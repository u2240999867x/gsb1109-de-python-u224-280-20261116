"""Unit tests for the offline recording audit."""

from io import BytesIO

from pydicom.dataset import Dataset
from pydicom.uid import (
    UID,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
)

from pynetdicom.dimse_messages import C_STORE_RQ
from pynetdicom.dimse_primitives import C_STORE
from pynetdicom.dsutils import encode
from pynetdicom.pdu import A_ASSOCIATE_AC, A_ASSOCIATE_RJ, A_ASSOCIATE_RQ, P_DATA_TF
from pynetdicom.pdu_primitives import (
    A_ASSOCIATE,
    P_DATA,
    SCP_SCU_RoleSelectionNegotiation,
)
from pynetdicom.presentation import PresentationContext
from pynetdicom.recording import (
    Direction,
    IssueCode,
    MessageStatus,
    Role,
    inspect_recording,
)


CT = "1.2.840.10008.5.1.4.1.1.2"
MR = "1.2.840.10008.5.1.4.1.1.4"
VERIFICATION = "1.2.840.10008.1.1"


def context(context_id, abstract_syntax=CT, transfer_syntax=None, result=None):
    """Return a PresentationContext."""
    cx = PresentationContext()
    cx.context_id = context_id
    cx.abstract_syntax = abstract_syntax
    if isinstance(transfer_syntax, (list, tuple)):
        cx.transfer_syntax = list(transfer_syntax)
    else:
        cx.transfer_syntax = [transfer_syntax or ImplicitVRLittleEndian]
    if result is not None:
        cx.result = result

    return cx


def role(sop_class, scu_role, scp_role):
    """Return an SCP/SCU Role Selection Negotiation item."""
    item = SCP_SCU_RoleSelectionNegotiation()
    item.sop_class_uid = sop_class
    item.scu_role = scu_role
    item.scp_role = scp_role

    return item


def association_request(contexts, roles=None, calling="SCU", called="SCP"):
    """Return an encoded A-ASSOCIATE-RQ PDU."""
    primitive = A_ASSOCIATE()
    primitive.calling_ae_title = calling
    primitive.called_ae_title = called
    primitive.application_context_name = "1.2.840.10008.3.1.1.1"
    primitive.presentation_context_definition_list = contexts
    primitive.user_information = list(roles or [])

    return A_ASSOCIATE_RQ(primitive).encode()


def association_response(contexts, roles=None, calling="SCU", called="SCP"):
    """Return an encoded A-ASSOCIATE-AC PDU."""
    primitive = A_ASSOCIATE()
    primitive.calling_ae_title = calling
    primitive.called_ae_title = called
    primitive.application_context_name = "1.2.840.10008.3.1.1.1"
    primitive.presentation_context_definition_results_list = contexts
    primitive.user_information = list(roles or [])

    return A_ASSOCIATE_AC(primitive).encode()


def association_reject():
    """Return an encoded A-ASSOCIATE-RJ PDU."""
    primitive = A_ASSOCIATE()
    primitive.result = 1
    primitive.result_source = 1
    primitive.diagnostic = 7

    return A_ASSOCIATE_RJ(primitive).encode()


def c_store(context_id, transfer_syntax=ImplicitVRLittleEndian, **kwargs):
    """Return the encoded P-DATA PDUs for a C-STORE request."""
    sop_class = kwargs.get("sop_class", CT)
    patient_id = kwargs.get("patient_id", "Test1101")

    primitive = C_STORE()
    primitive.MessageID = kwargs.get("message_id", 1)
    primitive.AffectedSOPClassUID = sop_class
    primitive.AffectedSOPInstanceUID = kwargs.get("sop_instance", "1.2.3.4")
    primitive.Priority = 2

    dataset = Dataset()
    dataset.SOPClassUID = sop_class
    dataset.SOPInstanceUID = kwargs.get("data_sop_instance", "1.2.3.4")
    dataset.PatientID = patient_id
    data = encode(
        dataset,
        transfer_syntax.is_implicit_VR,
        transfer_syntax.is_little_endian,
    )
    primitive.DataSet = BytesIO(data)

    message = C_STORE_RQ()
    message.primitive_to_message(primitive)

    return [P_DATA_TF(pdata).encode() for pdata in message.encode_msg(context_id, 0)]


def c_store_without_data(context_id, **kwargs):
    """Return a command set that declares a dataset which is not sent."""
    primitive = C_STORE()
    primitive.MessageID = 1
    primitive.AffectedSOPClassUID = CT
    primitive.AffectedSOPInstanceUID = "1.2.3.4"
    primitive.Priority = 2
    primitive.DataSet = BytesIO(b"\x10\x00\x10\x00\x02\x00\x00\x00A^")

    message = C_STORE_RQ()
    message.primitive_to_message(primitive)
    message.data_set = None
    message.command_set.CommandDataSetType = 0x0001

    return [P_DATA_TF(pdata).encode() for pdata in message.encode_msg(context_id, 0)]


class TestContextView:
    """Tests for the per-association context view."""

    def test_multiple_contexts_same_sop_class(self):
        """Each context ID keeps its own accepted transfer syntax."""
        recording = inspect_recording(
            association_request(
                [
                    context(1, CT, ImplicitVRLittleEndian),
                    context(3, CT, ExplicitVRLittleEndian),
                    context(5, CT, ExplicitVRBigEndian),
                ]
            )
            + association_response(
                [
                    context(1, CT, ImplicitVRLittleEndian, 0x00),
                    context(3, CT, ExplicitVRLittleEndian, 0x00),
                    context(5, CT, ExplicitVRBigEndian, 0x04),
                ]
            )
        )

        view = recording[0].contexts
        assert [cx.context_id for cx in view.contexts] == [1, 3]
        assert view.by_id(1).transfer_syntax == ImplicitVRLittleEndian
        assert view.by_id(3).transfer_syntax == ExplicitVRLittleEndian
        assert view.by_id(5) is None
        assert view.is_rejected(5)
        assert [cx.context_id for cx in view.rejected] == [5]

    def test_accepted_alternative_is_not_all_accepted(self):
        """Accepting one transfer syntax doesn't accept the alternatives."""
        recording = inspect_recording(
            association_request(
                [
                    context(
                        1,
                        CT,
                        [ImplicitVRLittleEndian, ExplicitVRLittleEndian],
                    )
                ]
            )
            + association_response([context(1, CT, ExplicitVRLittleEndian, 0x00)])
        )

        view = recording[0].contexts
        assert view.by_id(1).transfer_syntax == ExplicitVRLittleEndian
        assert view.proposed_syntaxes(CT) == (
            ImplicitVRLittleEndian,
            ExplicitVRLittleEndian,
        )

    def test_contexts_are_not_shared_between_associations(self):
        """A context from another association isn't used to fill gaps."""
        first = association_request([context(1, CT, ImplicitVRLittleEndian)])
        first += association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
        first += b"".join(c_store(1, ImplicitVRLittleEndian))

        second = association_request([context(3, CT, ExplicitVRLittleEndian)])
        second += association_response([context(3, CT, ExplicitVRLittleEndian, 0x00)])
        second += b"".join(c_store(1, ImplicitVRLittleEndian))

        recording = inspect_recording(first + second)
        assert len(recording) == 2
        assert recording[0].messages[0].status is MessageStatus.EXPLAINABLE
        message = recording[1].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert message.issue_codes == (IssueCode.UNKNOWN_CONTEXT,)


class TestMessageAudit:
    """Tests for the per-message audit statuses."""

    def test_explainable(self):
        """A data set encoded as the accepted transfer syntax is explainable."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store(1, ImplicitVRLittleEndian))
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.EXPLAINABLE
        assert message.issue_codes == ()
        assert message.data_set.PatientID == "Test1101"

    def test_unaccepted_context(self):
        """Messages sent under a rejected context are reported."""
        recording = inspect_recording(
            association_request(
                [
                    context(1, CT, ImplicitVRLittleEndian),
                    context(3, CT, ExplicitVRLittleEndian),
                ]
            )
            + association_response(
                [
                    context(1, CT, ImplicitVRLittleEndian, 0x00),
                    context(3, CT, ExplicitVRLittleEndian, 0x04),
                ]
            )
            + b"".join(c_store(3, ExplicitVRLittleEndian))
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert message.issue_codes == (IssueCode.UNACCEPTED_CONTEXT,)

    def test_transfer_syntax_conflict(self):
        """A data set encoded differently to the accepted syntax is reported."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store(1, ExplicitVRLittleEndian))
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.ENCODING_CONFLICT
        assert IssueCode.DATA_SET_ENCODING_CONFLICT in message.issue_codes

    def test_conflict_not_hidden_by_matching_fields(self):
        """Matching field values do not hide a transfer syntax conflict."""
        dataset = Dataset()
        dataset.PatientID = "Test1101"
        data = encode(dataset, False, True)

        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        primitive.DataSet = BytesIO(data)

        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        pdus = [P_DATA_TF(pdata).encode() for pdata in message.encode_msg(1, 0)]

        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(pdus)
        )

        record = recording[0].messages[0]
        assert record.status is MessageStatus.ENCODING_CONFLICT
        assert IssueCode.DATA_SET_ENCODING_CONFLICT in record.issue_codes

    def test_missing_data_set_end(self):
        """A truncated data set is reported as insufficient evidence."""
        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        dataset = Dataset()
        dataset.SOPClassUID = CT
        dataset.SOPInstanceUID = "1.2.3.4"
        dataset.PatientID = "Test1101"
        primitive.DataSet = BytesIO(encode(dataset, True, True))
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        pdus = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 10)]
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(pdus[:-1])
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert IssueCode.MISSING_DATA_SET_END in message.issue_codes

    def test_missing_data_set(self):
        """A declared but absent data set is reported."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store_without_data(1))
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert IssueCode.MISSING_DATA_SET in message.issue_codes

    def test_sop_class_conflict(self):
        """A command set SOP Class that differs from the context is flagged."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store(1, ImplicitVRLittleEndian, sop_class=MR))
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.ENCODING_CONFLICT
        assert IssueCode.SOP_CLASS_CONFLICT in message.issue_codes

    def test_sop_instance_conflict(self):
        """Command and data set SOP Instance UIDs must agree."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(
                c_store(
                    1,
                    ImplicitVRLittleEndian,
                    sop_instance="1.2.3.4",
                    data_sop_instance="1.2.3.5",
                )
            )
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.ENCODING_CONFLICT
        assert IssueCode.SOP_INSTANCE_CONFLICT in message.issue_codes

    def test_other_messages_remain_deliverable(self):
        """One bad message doesn't invalidate the rest of the recording."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store(1, ImplicitVRLittleEndian, message_id=1))
            + b"".join(c_store(1, ExplicitVRLittleEndian, message_id=2))
        )

        assert len(recording.deliverables) == 1
        assert len(recording.problems) == 1
        assert recording.problems[0].status is MessageStatus.ENCODING_CONFLICT


class TestRoles:
    """Tests for role selection and message direction."""

    def test_role_not_allowed(self):
        """Messages sent in an unnegotiated role are reported."""
        request_roles = [role(CT, scu_role=False, scp_role=True)]
        response_roles = [role(CT, scu_role=False, scp_role=True)]
        recording = inspect_recording(
            association_request(
                [context(1, CT, ImplicitVRLittleEndian)], roles=request_roles
            )
            + association_response(
                [context(1, CT, ImplicitVRLittleEndian, 0x00)],
                roles=response_roles,
            )
            + b"".join(c_store(1, ImplicitVRLittleEndian))
        )

        # Tag the PDUs as requestor-sent so the role is not inferred
        from pynetdicom.recording import read_recording

        data = (
            association_request(
                [context(1, CT, ImplicitVRLittleEndian)], roles=request_roles
            )
            + association_response(
                [context(1, CT, ImplicitVRLittleEndian, 0x00)],
                roles=response_roles,
            )
            + b"".join(c_store(1, ImplicitVRLittleEndian))
        )
        pdus = read_recording(data)
        tagged = []
        for pdu in pdus:
            direction = (
                Direction.ACCEPTOR
                if isinstance(pdu, A_ASSOCIATE_AC)
                else Direction.REQUESTOR
            )
            tagged.append((direction, pdu))

        recording = inspect_recording(tagged)
        message = recording[0].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert IssueCode.ROLE_NOT_ALLOWED in message.issue_codes

    def test_role_is_recorded(self):
        """The negotiated roles are exposed by the context view."""
        request_roles = [role(CT, scu_role=False, scp_role=True)]
        response_roles = [role(CT, scu_role=False, scp_role=True)]
        recording = inspect_recording(
            association_request(
                [context(1, CT, ImplicitVRLittleEndian)], roles=request_roles
            )
            + association_response(
                [context(1, CT, ImplicitVRLittleEndian, 0x00)],
                roles=response_roles,
            )
        )

        view = recording[0].contexts
        assert view.by_id(1).requestor_roles == (Role.SCP,)
        assert view.by_id(1).acceptor_roles == (Role.SCU,)

    def test_default_roles(self):
        """Without role selection the requestor is SCU and acceptor is SCP."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
        )

        view = recording[0].contexts
        assert view.by_id(1).requestor_roles == (Role.SCU,)
        assert view.by_id(1).acceptor_roles == (Role.SCP,)


class TestRecordingSource:
    """Tests for the accepted recording inputs."""

    def test_bytes(self):
        """A bytestring recording is accepted."""
        data = association_request([context(1, CT, ImplicitVRLittleEndian)])
        recording = inspect_recording(data)
        assert len(recording) == 1

    def test_pdu_list(self):
        """A list of decoded PDUs is accepted."""
        from pynetdicom.recording import read_recording

        data = association_request([context(1, CT, ImplicitVRLittleEndian)])
        pdus = read_recording(data)
        recording = inspect_recording(pdus)
        assert len(recording) == 1

    def test_direction_tagged_pdus(self):
        """Direction-tagged PDUs are used to determine the message sender."""
        from pynetdicom.recording import read_recording

        data = association_request([context(1, CT, ImplicitVRLittleEndian)])
        data += association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
        data += b"".join(c_store(1, ImplicitVRLittleEndian))
        pdus = read_recording(data)
        tagged = []
        for pdu in pdus:
            direction = (
                Direction.REQUESTOR
                if isinstance(pdu, A_ASSOCIATE_RQ)
                else Direction.REQUESTOR
            )
            tagged.append((direction, pdu))

        recording = inspect_recording(tagged)
        message = recording[0].messages[0]
        assert message.sender is Direction.REQUESTOR
        assert message.sender_role is Role.SCU

    def test_rejected_association(self):
        """A rejected association has no accepted contexts."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_reject()
        )

        assert recording[0].is_rejected
        assert recording[0].contexts.contexts == ()
        assert recording[0].contexts.is_rejected(1)

    def test_json(self):
        """The recording can be serialised to JSON."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store(1, ImplicitVRLittleEndian))
        )

        data = recording.to_dict()
        assert data["associations"][0]["messages"][0]["status"] == "explainable"


class TestAssembledMessages:
    """Tests for message assembly from PDVs."""

    def test_multiple_messages_in_one_pdu(self):
        """Multiple messages in a single P-DATA PDU are kept separate."""
        first = c_store(1, ImplicitVRLittleEndian, message_id=1)
        second = c_store(1, ImplicitVRLittleEndian, message_id=2)

        # Rebuild into a single P-DATA PDU
        values = []
        for pdu in first + second:
            decoded = P_DATA_TF()
            decoded.decode(pdu)
            values.extend(decoded.to_primitive().presentation_data_value_list)

        combined = P_DATA()
        combined.presentation_data_value_list = [list(v) for v in values]
        data = association_request([context(1, CT, ImplicitVRLittleEndian)])
        data += association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
        data += P_DATA_TF(combined).encode()

        recording = inspect_recording(data)
        assert len(recording[0].messages) == 2
        assert all(m.status is MessageStatus.EXPLAINABLE for m in recording[0].messages)

    def test_split_message_across_pdus(self):
        """A message split across several P-DATA PDUs is reassembled."""
        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        dataset = Dataset()
        dataset.SOPClassUID = CT
        dataset.SOPInstanceUID = "1.2.3.4"
        dataset.PatientID = "Test1101"
        primitive.DataSet = BytesIO(encode(dataset, True, True))
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        split = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 10)]

        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(split)
        )

        record = recording[0].messages[0]
        assert record.status is MessageStatus.EXPLAINABLE
        assert record.data_set.PatientID == "Test1101"


class TestIssueCodes:
    """Tests for the issue code enum."""

    def test_aliases(self):
        """The issue codes normalise common spellings."""
        assert IssueCode("unaccepted-context") is IssueCode.UNACCEPTED_CONTEXT
        assert IssueCode("MISSING DATA SET") is IssueCode.MISSING_DATA_SET
        assert (
            MessageStatus("encoding-contradiction") is MessageStatus.ENCODING_CONFLICT
        )


class TestDelivery:
    """Tests for delivering only verified materials."""

    def test_deliver_excludes_conflicts(self):
        """Conflicting messages are not delivered, valid ones are."""
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store(1, ImplicitVRLittleEndian, message_id=1))
            + b"".join(c_store(1, ExplicitVRLittleEndian, message_id=2))
        )

        delivered = recording.deliver()
        assert len(delivered) == 1
        assert delivered[0].status is MessageStatus.EXPLAINABLE

        datasets = recording.deliver(datasets=True)
        assert len(datasets) == 1
        assert datasets[0].PatientID == "Test1101"

    def test_deliver_same_values_different_encoding(self):
        """A plausible-looking conflicting data set is not delivered."""
        dataset = Dataset()
        dataset.PatientID = "Test1101"
        data = encode(dataset, False, True)

        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        primitive.DataSet = BytesIO(data)
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        pdus = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 0)]

        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(pdus)
        )

        assert recording.deliver() == []
        assert len(recording.problems) == 1


class TestMismatchDescription:
    """Tests for describing an encoding mismatch."""

    def test_names_the_proposed_syntax(self):
        """A conflicting data set is matched against proposed alternatives."""
        recording = inspect_recording(
            association_request(
                [context(1, CT, [ImplicitVRLittleEndian, ExplicitVRLittleEndian])]
            )
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(c_store(1, ExplicitVRLittleEndian))
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.ENCODING_CONFLICT
        assert "Explicit VR Little Endian" in message.issues[0]
        assert "not accepted" in message.issues[0]


class TestFileMeta:
    """Tests for data sets that include File Meta Information."""

    def test_media_storage_sop_class_conflict(self):
        """The File Meta SOP Class must also match the context."""
        from pydicom.dataset import FileMetaDataset

        dataset = Dataset()
        dataset.SOPClassUID = MR
        dataset.SOPInstanceUID = "1.2.3.4"
        dataset.PatientID = "Test1101"
        data = encode(dataset, True, True)

        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = MR
        file_meta.MediaStorageSOPInstanceUID = "1.2.3.4"
        file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
        preamble = b"\x00" * 128 + b"DICM"

        from pynetdicom.dsutils import encode_file_meta

        raw = preamble + encode_file_meta(file_meta) + data

        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        primitive.DataSet = BytesIO(raw)
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        pdus = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 0)]

        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(pdus)
        )

        record = recording[0].messages[0]
        assert record.status is MessageStatus.ENCODING_CONFLICT
        assert IssueCode.FILE_META_ENCODING_CONFLICT in record.issue_codes

    def test_declared_transfer_syntax_conflict(self):
        """The File Meta transfer syntax must match the accepted syntax."""
        from pydicom.dataset import FileMetaDataset

        from pynetdicom.dsutils import encode_file_meta

        dataset = Dataset()
        dataset.SOPClassUID = CT
        dataset.SOPInstanceUID = "1.2.3.4"
        dataset.PatientID = "Test1101"
        data = encode(dataset, False, True)

        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = CT
        file_meta.MediaStorageSOPInstanceUID = "1.2.3.4"
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        raw = b"\x00" * 128 + b"DICM" + encode_file_meta(file_meta) + data

        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        primitive.DataSet = BytesIO(raw)
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        pdus = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 0)]

        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(pdus)
        )

        record = recording[0].messages[0]
        assert record.status is MessageStatus.ENCODING_CONFLICT
        assert IssueCode.TRANSFER_SYNTAX_CONFLICT in record.issue_codes
        assert record.declared_transfer_syntax == ExplicitVRLittleEndian


class TestTruncatedCommands:
    """Tests for recordings that end mid message."""

    def test_missing_command_set_end(self):
        """A recording ending mid command set is insufficient evidence."""
        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        primitive.DataSet = BytesIO(encode(Dataset(), True, True))
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        pdus = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 10)]
        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(pdus[:1])
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert IssueCode.MISSING_COMMAND_SET_END in message.issue_codes

    def test_interrupted_message_does_not_affect_next(self):
        """A truncated message doesn't corrupt the message that follows it."""
        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        dataset = Dataset()
        dataset.SOPClassUID = CT
        dataset.SOPInstanceUID = "1.2.3.4"
        dataset.PatientID = "Test1101"
        primitive.DataSet = BytesIO(encode(dataset, True, True))
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        split = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 10)]

        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + b"".join(split[:-1])
            + b"".join(c_store(1, ImplicitVRLittleEndian, message_id=2))
        )

        assert len(recording[0].messages) == 2
        assert recording[0].messages[0].status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert IssueCode.MISSING_DATA_SET_END in recording[0].messages[0].issue_codes
        assert recording[0].messages[1].status is MessageStatus.EXPLAINABLE


class TestIssueNormalisation:
    """Tests for status and issue code spellings."""

    def test_status_spellings(self):
        """Common spellings map to the correct status."""
        assert MessageStatus("encoding conflict") is MessageStatus.ENCODING_CONFLICT
        assert (
            MessageStatus("INSUFFICIENT-EVIDENCE")
            is MessageStatus.INSUFFICIENT_EVIDENCE
        )
        assert MessageStatus("explainable") is MessageStatus.EXPLAINABLE

    def test_issue_spellings(self):
        """Common spellings map to the correct issue code."""
        assert IssueCode("unaccepted context") is IssueCode.UNACCEPTED_CONTEXT
        assert IssueCode("role-not-allowed") is IssueCode.ROLE_NOT_ALLOWED
        assert IssueCode("missing data set end") is IssueCode.MISSING_DATA_SET_END


class TestContextMismatch:
    """Tests for messages split across contexts."""

    def test_command_and_data_use_different_contexts(self):
        """A data set sent under another context is reported."""
        request = association_request(
            [
                context(1, CT, ImplicitVRLittleEndian),
                context(3, CT, ExplicitVRLittleEndian),
            ]
        )
        response = association_response(
            [
                context(1, CT, ImplicitVRLittleEndian, 0x00),
                context(3, CT, ExplicitVRLittleEndian, 0x00),
            ]
        )

        primitive = C_STORE()
        primitive.MessageID = 1
        primitive.AffectedSOPClassUID = CT
        primitive.AffectedSOPInstanceUID = "1.2.3.4"
        primitive.Priority = 2
        dataset = Dataset()
        dataset.SOPClassUID = CT
        dataset.SOPInstanceUID = "1.2.3.4"
        primitive.DataSet = BytesIO(encode(dataset, True, True))
        message = C_STORE_RQ()
        message.primitive_to_message(primitive)
        pdus = [P_DATA_TF(pdu).encode() for pdu in message.encode_msg(1, 0)]
        command, data = pdus

        # Re-home the data set PDV onto context ID 3
        decoded = P_DATA_TF()
        decoded.decode(data)
        pdv = decoded.presentation_data_value_items[0]
        pdv.presentation_context_id = 3

        recording = inspect_recording(request + response + command + decoded.encode())

        record = recording[0].messages[0]
        assert IssueCode.CONTEXT_MISMATCH in record.issue_codes
        assert record.status is MessageStatus.ENCODING_CONFLICT


class TestUnknownSyntax:
    """Tests for accepted transfer syntaxes with unknown properties."""

    def test_private_transfer_syntax(self):
        """A private accepted syntax yields insufficient evidence, not a crash."""
        private = UID("1.2.3.4.5")
        recording = inspect_recording(
            association_request([context(1, CT, private)])
            + association_response([context(1, CT, private, 0x00)])
            + b"".join(c_store(1, ImplicitVRLittleEndian))
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert IssueCode.UNKNOWN_TRANSFER_SYNTAX in message.issue_codes


class TestMalformedPdv:
    """Tests for malformed presentation data values."""

    def test_reserved_control_bits(self):
        """Reserved bits in the PDV control header are reported."""
        pdus = c_store(1, ImplicitVRLittleEndian)
        decoded = P_DATA_TF()
        decoded.decode(pdus[0])
        item = decoded.presentation_data_value_items[0]
        item.presentation_data_value = b"\xff" + item.presentation_data_value[1:]

        recording = inspect_recording(
            association_request([context(1, CT, ImplicitVRLittleEndian)])
            + association_response([context(1, CT, ImplicitVRLittleEndian, 0x00)])
            + decoded.encode()
        )

        message = recording[0].messages[0]
        assert message.status is MessageStatus.INSUFFICIENT_EVIDENCE
        assert IssueCode.INVALID_MESSAGE in message.issue_codes
