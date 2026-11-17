import io
import struct

from smb import smb2_constants as sc
from smb.base import SMB


class InMemorySMB(SMB):
    """
    SMB client with an in-memory transport.

    Outgoing SMB2 messages are captured in ``sent``. Tests can then feed
    crafted responses through the real ``_updateState_SMB2`` dispatcher.
    """

    def __init__(self, initial_credits=64):
        SMB.__init__(self, 'user', 'password', 'CLIENT', 'SERVER')
        self.sent = []
        self.messages = []
        self.last_message = None
        self._setupSMB2Methods()
        self.is_using_smb2 = True
        self.session_id = 0x1122334455667788
        self.max_read_size = 64 * 1024
        self.max_write_size = 64 * 1024
        self.has_authenticated = True
        self.request_ledger.initial_credits = initial_credits
        self.request_ledger.total_credits = initial_credits
        self.results = []
        self.errors = []

    def sendNMBMessage(self, data):
        self.sent.append(data)

    def _sendSMBMessage_SMB2(self, smb_message):
        # Keep the logical message so tests can inspect its MID/credit charge
        # after encoding, without reimplementing the wire format.
        self.last_message = smb_message
        SMB._sendSMBMessage_SMB2(self, smb_message)
        self.messages.append(smb_message)

    def onAuthOK(self):
        pass


class InMemoryReadResponse:
    """Minimal stand-in for a decoded SMB2 READ response."""

    def __init__(self, mid, data, status=0, async_id=0,
                 session_id=0x1122334455667788):
        self.command = sc.SMB2_COM_READ
        self.mid = mid
        self.status = status
        self.async_id = async_id
        self.session_id = session_id
        self.flags = sc.SMB2_FLAGS_SERVER_TO_REDIR
        if async_id:
            self.flags |= sc.SMB2_FLAGS_ASYNC_COMMAND
        self.credit_charge = 1
        self.credit_re = 1
        self.payload = _ReadPayload(data)

    @property
    def isAsync(self):
        return bool(self.flags & sc.SMB2_FLAGS_ASYNC_COMMAND)

    @property
    def isReply(self):
        return bool(self.flags & sc.SMB2_FLAGS_SERVER_TO_REDIR)


class _ReadPayload:
    def __init__(self, data):
        self.data = data
        self.data_length = len(data)


def _mount_file(smb, file_data, max_read_size=64 * 1024):
    smb.max_read_size = max_read_size
    smb.connected_trees['share'] = 1
    smb.session_id = 0x1122334455667788

    def on_done(result):
        smb.results.append(result)
        smb.is_busy = False

    def on_error(error):
        smb.errors.append(error)
        smb.is_busy = False

    out = io.BytesIO()
    smb.is_busy = True
    smb._retrieveFileFromOffset('share', 'big.bin', out, on_done, on_error, 0, -1)
    return out


def _message_by_command(smb, command):
    for message in reversed(smb.messages):
        if message.command == command:
            return message
    raise AssertionError('no message for command 0x%04x' % command)


def _respond(smb, message, status=0, async_id=0):
    smb._updateState_SMB2(_SyntheticResponse(message, status=status, async_id=async_id))


class _SyntheticResponse:
    """Response built from a captured SMB2Message, with selected payload fields."""

    def __init__(self, message, status=0, async_id=0, data=b''):
        self.command = message.command
        self.mid = message.mid
        self.status = status
        self.async_id = async_id
        self.session_id = message.session_id
        self.flags = sc.SMB2_FLAGS_SERVER_TO_REDIR
        if async_id:
            self.flags |= sc.SMB2_FLAGS_ASYNC_COMMAND
        self.credit_charge = 1
        self.credit_re = 1
        self.payload = _Payload(message.command, data, status)

    @property
    def isAsync(self):
        return bool(self.flags & sc.SMB2_FLAGS_ASYNC_COMMAND)

    @property
    def isReply(self):
        return bool(self.flags & sc.SMB2_FLAGS_SERVER_TO_REDIR)


class _Payload:
    def __init__(self, command, data, status):
        self.data = data
        self.data_length = len(data)
        self.fid = b'\xAA' * 16
        self.file_attributes = 0x80
        self.count = len(data)
        if command == sc.SMB2_COM_QUERY_INFO and status == 0:
            # FileStandardInformation: AllocationSize then EndOfFile at offset 8
            self.data = struct.pack('<QQ', len(data), len(data)) if len(data) == 16 else \
                struct.pack('<QQ', 0, 0)
            self.data_length = len(self.data)
