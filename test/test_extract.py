"""
Tests for dcmstack.extract
"""
import sys, warnings
from os import path

import pytest
from nibabel.nicom import csareader
try:
    import pydicom
except ImportError:
    import dicom as pydicom

from . import test_dir, src_dir

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    from dcmstack import extract

class TestCsa(object):
    def setup_method(self, method):
        data_fn = path.join(test_dir, 'data', 'extract', 'csa_test.dcm')
        self.data = pydicom.dcmread(data_fn)

    def teardown_method(self, method):
        del self.data

    def test_simplify(self):
        assert extract.simplify_csa_dict(None) == None
        csa_dict = csareader.read(self.data[pydicom.tag.Tag(0x29, 0x1010)].value)
        simp_dict = extract.simplify_csa_dict(csa_dict)
        for tag in csa_dict['tags']:
            items = csa_dict['tags'][tag]['items']
            if len(items) == 0:
                assert(not tag in simp_dict)
            elif len(items) == 1:
                assert simp_dict[tag] == items[0]
            else:
                assert simp_dict[tag] == items

    def test_csa_image_trans(self):
        csa_dict = extract.csa_series_trans_func(self.data[(0x29, 0x1010)])
        assert csa_dict["EchoLinePosition"] == 64

    def test_parse_phx_line(self):
        assert(extract._parse_phoenix_line("") is None)
        assert(extract._parse_phoenix_line("#test = 2") is None)

        assert extract._parse_phoenix_line('test = "" 2 ""') == ('test', ' 2 ')
        assert extract._parse_phoenix_line('test = "" #2 ""'), ('test', ' #2 ')
        assert extract._parse_phoenix_line('test = "" 2 ""#=') == ('test', ' 2 ')
        assert extract._parse_phoenix_line("test = 2#") == ('test', 2)
        assert extract._parse_phoenix_line("test = 0x2") == ('test', 2)
        assert extract._parse_phoenix_line("test = 2.") == ('test', 2.0)

        
        with pytest.raises(extract.PhoenixParseError):
            extract._parse_phoenix_line('test = blah')
        with pytest.raises(extract.PhoenixParseError):
            extract._parse_phoenix_line('==')
        with pytest.raises(extract.PhoenixParseError):
            extract._parse_phoenix_line('test')
        with pytest.raises(extract.PhoenixParseError):
            extract._parse_phoenix_line('test = "" 2 ""3')

    def test_csa_series_trans(self):
        csa_dict = extract.csa_series_trans_func(self.data[(0x29, 0x1020)])
        assert csa_dict['MrPhoenixProtocol.sEFISPEC.bEFIDataValid'] == 1

class TestMetaExtractor(object):
    def setup_method(self, method):
        data_fn = path.join(test_dir, 'data', 'extract', 'csa_test.dcm')
        self.data = pydicom.dcmread(data_fn)

    def teardown_method(self,method):
        del self.data

    def test_get_elem_key(self):
        ignore_rules = (extract.ignore_pixel_data,)
        extractor = extract.MetaExtractor(ignore_rules=ignore_rules)
        for key in extractor(self.data):
            assert(key.strip() != '')
            assert(key[0].isalpha())

    def test_get_elem_value(self):
        ignore_rules = (extract.ignore_pixel_data,)
        extractor = extract.MetaExtractor(ignore_rules=ignore_rules)
        for elem in self.data:
            value = extractor._get_elem_value(elem)
            if elem.VM > 1:
                assert(isinstance(value, list))
            if elem.VR in list(extract.unpack_vr_map) + ['DS', 'IS']:
                if elem.VM == 1:
                    assert(not isinstance(value, str))
                else:
                    assert(not any(isinstance(val, str) for val in value))

    def test_dup_trans(self):
        translators = [extract.csa_image_trans, extract.csa_image_trans]
        extractor = extract.MetaExtractor(translators=translators)
        with pytest.raises(ValueError):
            extractor(self.data)

    def test_reloc_private(self):
        extractor = extract.MetaExtractor()
        elem = self.data[(0x29, 0x10)]
        elem.tag = pydicom.tag.Tag((0x29, 0x20))
        self.data[(0x29, 0x20)] = elem
        elem = self.data[(0x29, 0x1010)]
        elem.tag = pydicom.tag.Tag((0x29, 0x2010))
        self.data[(0x29, 0x2010)] = elem
        elem = self.data[(0x29, 0x1020)]
        elem.tag = pydicom.tag.Tag((0x29, 0x2020))
        self.data[(0x29, 0x2020)] = elem
        del self.data[(0x29, 0x10)]
        del self.data[(0x29, 0x1010)]
        del self.data[(0x29, 0x1020)]
        meta_dict = extractor(self.data)
        assert meta_dict["CsaImage.EchoLinePosition"] == 64
        assert meta_dict['CsaSeries.MrPhoenixProtocol.sEFISPEC.bEFIDataValid'] == 1

    def test_non_reloc_private(self):
        extractor = extract.MetaExtractor()
        meta_dict = extractor(self.data)
        assert meta_dict["CsaImage.EchoLinePosition"] == 64
        assert meta_dict['CsaSeries.MrPhoenixProtocol.sEFISPEC.bEFIDataValid'] == 1

    def test_none_vals(self):
        extractor = extract.MetaExtractor()
        self.data.PercentSampling = None
        meta_data = extractor(self.data)
        assert "PercentSampling" not in meta_data


def _deep_contains(val, needle):
    if isinstance(val, str):
        return needle in val
    if isinstance(val, (list, tuple)):
        return any(_deep_contains(item, needle) for item in val)
    if isinstance(val, dict):
        return any(_deep_contains(item, needle) for item in val.values())
    return False


class TestSourcePolicy(object):
    def setup_method(self, method):
        data_fn = path.join(test_dir, 'data', 'extract', 'csa_test.dcm')
        self.data = pydicom.dcmread(data_fn)
        self.extractor = extract.MetaExtractor()

    def teardown_method(self, method):
        del self.data
        del self.extractor

    def test_restrict_identity_field(self):
        policy = extract.SourcePolicy('v1', restricted_sources=['PatientID'])
        proj = self.extractor.project(self.data, policy)
        assert 'PatientID' not in proj.meta
        assert 'PatientID' in proj.removed
        # Independent measurements are still kept
        assert proj['StationName'] == 'MRC35139'
        assert proj.meta['CsaImage.EchoLinePosition'] == 64

    def test_restricted_value_does_not_flow_back(self):
        self.data.PatientName = 'Secret^Name'
        policy = extract.SourcePolicy('v1', restricted_sources=['PatientName'])
        proj = self.extractor.project(self.data, policy)
        assert 'PatientName' not in proj.meta
        assert not any(_deep_contains(val, 'Secret') for val in proj.meta.values())
        # The audit record holds key names and sources, never values
        assert not any(_deep_contains(val, 'Secret') for val in proj.removed.values())
        assert not _deep_contains(proj.to_dict().get('absent'), 'Secret')

    def test_translator_source_restriction(self):
        # Restricting the CSA series element removes all keys derived from it,
        # even though the derived key names look unrelated
        policy = extract.SourcePolicy('v1', restricted_sources=[(0x29, 0x1020)])
        proj = self.extractor.project(self.data, policy)
        assert not any(key.startswith('CsaSeries.') for key in proj.meta)
        assert any(key.startswith('CsaSeries.') for key in proj.removed)
        # The independent CSA image header is unaffected
        assert proj.meta['CsaImage.EchoLinePosition'] == 64

    def test_private_creator_restriction(self):
        policy = extract.SourcePolicy('v1',
                                      restricted_sources=['SIEMENS CSA HEADER'])
        proj = self.extractor.project(self.data, policy)
        assert not any(key.startswith(('CsaImage.', 'CsaSeries.'))
                       for key in proj.meta)

    def test_derivation_rule_allows_key(self):
        allow_key = 'CsaSeries.MrPhoenixProtocol.sEFISPEC.bEFIDataValid'
        policy = extract.SourcePolicy('v1',
                                      restricted_sources=[(0x29, 0x1020)],
                                      derivation_rules=[allow_key])
        proj = self.extractor.project(self.data, policy)
        assert proj.meta[allow_key] == 1
        assert allow_key not in proj.removed
        # Other derived keys from the same source are still removed
        assert any(key.startswith('CsaSeries.') for key in proj.removed)

    def test_mixed_deps_not_upgraded_by_translation(self):
        # A translator declared to depend on both allowed and restricted
        # sources has its outputs removed; being "translated" grants nothing
        policy = extract.SourcePolicy(
            'v1',
            restricted_sources=['PatientID'],
            translator_deps={'CsaImage': [(0x29, 0x1010), 'PatientID']},
        )
        proj = self.extractor.project(self.data, policy)
        assert not any(key.startswith('CsaImage.') for key in proj.meta)
        assert any(key.startswith('CsaImage.') for key in proj.removed)
        # Unless a derivation rule explicitly allows a specific output
        policy = extract.SourcePolicy(
            'v1',
            restricted_sources=['PatientID'],
            translator_deps={'CsaImage': [(0x29, 0x1010), 'PatientID']},
            derivation_rules=['CsaImage.EchoLinePosition'],
        )
        proj = self.extractor.project(self.data, policy)
        assert proj.meta['CsaImage.EchoLinePosition'] == 64

    def test_missing_vs_removed(self):
        policy = extract.SourcePolicy(
            'v1', restricted_sources=['PatientID', 'IssuerOfPatientID']
        )
        proj = self.extractor.project(self.data, policy)
        # Observed and suppressed
        assert 'PatientID' in proj.removed
        assert proj.source_dispositions['PatientID'] == 'removed'
        # Never observed: reported as absent, not as removed
        assert proj.source_dispositions['IssuerOfPatientID'] == 'absent'
        assert 'IssuerOfPatientID' in proj.absent
        assert 'IssuerOfPatientID' not in proj.removed

    def test_error_summary_sanitized(self):
        def bad_trans(elem):
            raise ValueError('secret-value-xyz')
        trans = extract.Translator('BadTrans',
                                   pydicom.tag.Tag(0x29, 0x1010),
                                   'SIEMENS CSA HEADER',
                                   bad_trans)
        policy = extract.SourcePolicy('v1', restricted_sources=[(0x29, 0x1010)])
        extractor = extract.MetaExtractor(translators=[trans], policy=policy)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            extractor(self.data)
        messages = [str(w.message) for w in caught]
        assert any('BadTrans' in msg for msg in messages)
        assert not any('secret-value-xyz' in msg for msg in messages)
        # Without a policy the details are still reported
        extractor = extract.MetaExtractor(translators=[trans])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            extractor(self.data)
        assert any('secret-value-xyz' in str(w.message) for w in caught)

    def test_policy_versioning(self):
        pol_v1 = extract.SourcePolicy('v1', restricted_sources=['PatientID'])
        pol_v2 = extract.SourcePolicy('v2', restricted_sources=['PatientID'])
        proj = self.extractor.project(self.data, pol_v1)
        assert proj.is_current(pol_v1)
        assert not proj.is_current(pol_v2)
        assert extract.verify_projection(proj, pol_v1)
        assert extract.verify_projection(proj.to_dict(), pol_v1)
        # Artifacts from the old policy cannot pass as results of the new one
        assert not extract.verify_projection(proj.to_dict(), pol_v2)
        # Same version but different rules also fails verification
        pol_v1_wide = extract.SourcePolicy(
            'v1', restricted_sources=['PatientID', 'PatientName']
        )
        assert not extract.verify_projection(proj.to_dict(), pol_v1_wide)

    def test_project_requires_policy(self):
        with pytest.raises(ValueError):
            self.extractor.project(self.data)

    def test_attached_policy_used_by_default(self):
        policy = extract.SourcePolicy('v1', restricted_sources=['PatientID'])
        extractor = extract.MetaExtractor(policy=policy)
        proj = extractor.project(self.data)
        assert 'PatientID' not in proj.meta
        assert proj.policy_version == 'v1'
