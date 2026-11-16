import pytest

from smb.asset_library import (AssetLibrary, AssetFile, StreamRecord,
                               DEFAULT_STREAM_NAME, DATA_STREAM_TYPE,
                               StreamNameError, DuplicateFileError,
                               UnknownAssetError, parse_smb_path,
                               ensure_directory_path, validate_stream_name)


def _library_with_file(path='share\\report.docx', file_id='file-1'):
    library = AssetLibrary()
    asset_file = library.register_file(path, file_id)
    return library, asset_file


def test_default_stream_is_registered_with_name_size_and_identity():
    library, asset_file = _library_with_file()
    record = library.register_stream(asset_file.path, DEFAULT_STREAM_NAME, 4096)

    assert record.is_default
    assert record.stream_name == ''
    assert record.size == 4096
    assert record.file_id == 'file-1'
    assert record.is_readable
    assert asset_file.default_stream is record
    assert asset_file.get_stream('').identity == ('file-1', '')


def test_named_stream_is_registered_alongside_default_stream():
    library, asset_file = _library_with_file()
    default = library.register_stream(asset_file.path, '', 100)
    named = library.register_stream(asset_file.path, 'Zone.Identifier', 26)

    assert named.is_named
    assert named.stream_name == 'Zone.Identifier'
    assert named.size == 26
    assert named.file_id == asset_file.file_id
    assert [record.stream_name for record in asset_file.streams()] == ['', 'Zone.Identifier']
    assert asset_file.named_streams() == [named]
    assert default in asset_file.streams()
    assert len(library.all_streams()) == 2


def test_same_named_stream_on_different_files_has_distinct_identity():
    library = AssetLibrary()
    first = library.register_file('share\\a.txt', 'id-a')
    second = library.register_file('share\\b.txt', 'id-b')

    stream_a = library.register_stream(first.path, 'metadata', 10)
    stream_b = library.register_stream(second.path, 'metadata', 20)

    assert stream_a.stream_name == stream_b.stream_name
    assert stream_a.identity != stream_b.identity
    assert stream_a.file_id != stream_b.file_id
    assert stream_a != stream_b
    assert len({stream_a, stream_b}) == 2
    assert library.resolve_stream('share\\a.txt:metadata').size == 10
    assert library.resolve_stream('share\\b.txt:metadata').size == 20


def test_unreadable_stream_does_not_affect_file_or_other_streams():
    library, asset_file = _library_with_file()
    default = library.register_stream(asset_file.path, '', 512)
    broken = library.register_stream(asset_file.path, 'broken', 64, is_readable=False)

    assert broken.is_readable is False
    assert default.is_readable is True
    assert library.find_by_path(asset_file.path) is asset_file
    assert asset_file.get_stream('').is_readable

    asset_file.mark_stream_unreadable('broken')
    assert asset_file.get_stream('broken').is_readable is False
    assert asset_file.get_stream('broken').size == 64
    assert asset_file.default_stream.is_readable is True


def test_moving_file_migrates_all_stream_relations_via_identity():
    library, asset_file = _library_with_file()
    default = library.register_stream(asset_file.path, '', 100)
    named = library.register_stream(asset_file.path, 'summary', 12)

    moved = library.move_file('share\\report.docx', 'archive\\2026\\report.docx')

    assert moved is asset_file
    assert moved.file_id == 'file-1'
    assert moved.path == 'archive\\2026\\report.docx'
    assert moved.get_stream('') is default
    assert moved.get_stream('summary') is named
    assert moved.stream_path('summary') == 'archive\\2026\\report.docx:summary'

    with pytest.raises(UnknownAssetError):
        library.find_by_path('share\\report.docx')
    assert library.resolve_stream('archive\\2026\\report.docx:summary') is named
    assert library.resolve_stream('archive\\2026\\report.docx') is default
    assert len(library) == 1


def test_colon_text_is_never_treated_as_path_directory():
    file_path, stream_name = parse_smb_path('archive\\report.docx:Zone.Identifier')
    assert file_path == 'archive\\report.docx'
    assert stream_name == 'Zone.Identifier'

    with pytest.raises(StreamNameError):
        ensure_directory_path('archive\\report.docx:Zone.Identifier')

    library, _ = _library_with_file()
    with pytest.raises(StreamNameError):
        library.register_file('dir\\file.txt:stream')
    with pytest.raises(StreamNameError):
        library.move_file('share\\report.docx', 'new\\file.txt:stream')


def test_parse_smb_path_variants():
    assert parse_smb_path('report.docx') == ('report.docx', '')
    assert parse_smb_path('dir/report.docx:ads') == ('dir\\report.docx', 'ads')
    assert parse_smb_path('report.docx:stream:$DATA') == ('report.docx', 'stream')
    assert parse_smb_path('report.docx::$DATA') == ('report.docx', '')

    with pytest.raises(StreamNameError):
        parse_smb_path(':stream')
    with pytest.raises(StreamNameError):
        parse_smb_path('report.docx:a:b')
    with pytest.raises(StreamNameError):
        parse_smb_path('report.docx:stream:$INDEX_ALLOCATION')


def test_register_smb_entry_creates_file_and_streams():
    library = AssetLibrary()
    default = library.register_smb_entry('share\\photo.jpg', 2048)
    named = library.register_smb_entry('share\\photo.jpg:preview', 128)

    assert default.is_default
    assert named.stream_name == 'preview'
    asset_file = library.find_by_path('share\\photo.jpg')
    assert asset_file.file_id == default.file_id == named.file_id
    assert len(asset_file.streams()) == 2


def test_reregistering_stream_keeps_identity_and_refreshes_size():
    library, asset_file = _library_with_file()
    first = library.register_stream(asset_file.path, 'log', 5)
    second = library.register_stream(asset_file.path, 'log', 99)

    assert first is second
    assert second.size == 99
    assert second.identity == ('file-1', 'log')
    assert len(asset_file.named_streams()) == 1


def test_stream_lookups_are_case_insensitive_but_preserve_name():
    library, asset_file = _library_with_file()
    record = library.register_stream(asset_file.path, 'Zone.Identifier', 7)

    assert asset_file.get_stream('ZONE.IDENTIFIER') is record
    assert record.stream_name == 'Zone.Identifier'
    assert asset_file.has_stream('zone.identifier')


def test_duplicate_registration_is_rejected():
    library = AssetLibrary()
    library.register_file('share\\a.txt', 'id-1')
    with pytest.raises(DuplicateFileError):
        library.register_file('share\\b.txt', 'id-1')
    with pytest.raises(DuplicateFileError):
        library.register_file('share\\a.txt', 'id-2')


def test_move_rejects_target_owned_by_another_file():
    library = AssetLibrary()
    library.register_file('share\\a.txt', 'id-a')
    library.register_file('share\\b.txt', 'id-b')

    with pytest.raises(DuplicateFileError):
        library.move_file('share\\a.txt', 'share\\b.txt')


def test_unknown_assets_raise():
    library, asset_file = _library_with_file()
    with pytest.raises(UnknownAssetError):
        library.find_by_path('missing.txt')
    with pytest.raises(UnknownAssetError):
        library.get_file('nope')
    with pytest.raises(UnknownAssetError):
        asset_file.get_stream('missing')
    with pytest.raises(UnknownAssetError):
        library.resolve_stream('share\\report.docx:missing')


def test_stream_name_validation():
    assert validate_stream_name('') == ''
    assert validate_stream_name(None) == ''
    assert validate_stream_name('valid.name-1') == 'valid.name-1'

    for bad_name in ('a:b', 'a\\b', 'a/b', 'bad ', 'bad.', 'a\tb'):
        with pytest.raises(StreamNameError):
            validate_stream_name(bad_name)


def test_stream_record_requires_non_negative_size():
    with pytest.raises(ValueError):
        StreamRecord('file-1', '', -1)
    with pytest.raises(ValueError):
        StreamRecord('file-1', '', 1.5)


def test_stream_path_notation():
    library, asset_file = _library_with_file()
    library.register_stream(asset_file.path, '', 1)
    library.register_stream(asset_file.path, 'ads', 2)

    assert asset_file.stream_path() == 'share\\report.docx'
    assert asset_file.stream_path('ads') == 'share\\report.docx:ads'
