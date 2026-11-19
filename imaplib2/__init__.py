from __future__ import absolute_import

from .imaplib2 import *
from .imaplib2 import __version__, __doc__
from . import mailbox
from .mailbox import (
    MODIFIED_UTF7,
    UTF8,
    MailboxCatalog,
    MailboxCatalogError,
    MailboxConflictError,
    MailboxDecodeState,
    MailboxEntry,
    MailboxName,
    MailboxNameError,
    MailboxNotAddressableError,
    MailboxNotFound,
    MailboxRecord,
    Namespace,
    NamespaceSet,
    decode_mailbox_name,
    decode_modified_utf7,
    encode_mailbox_name,
    encode_modified_utf7,
    parse_list_response,
    parse_namespace_response,
    restore_catalog,
)
