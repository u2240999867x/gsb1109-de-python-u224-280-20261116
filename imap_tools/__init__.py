# Lib author: Vladimir Kaukin <KaukinVK@ya.ru>
# Project home page: https://github.com/ikvk/imap_tools
# Mirror: https://gitflic.ru/project/ikvk/imap-tools
# License: Apache-2.0

from .catalog import (
    Catalog,
    CatalogConflict,
    CatalogError,
    CopyUid,
    CopyUidMapping,
    FolderObservation,
    FolderRecord,
    MessageOccurrence,
    MoveEvidence,
    MoveEvidenceError,
    MoveReport,
    ReconcileReport,
    RenameEvent,
    ServerIdentity,
    capture_observations,
    expand_uid_set,
    parse_copyuid,
)
from .consts import MailBoxFolderStatusOptions, MailMessageFlags, SortCriteria
from .errors import (
    ImapToolsError,
    MailboxAppendError,
    MailboxCopyError,
    MailboxDeleteError,
    MailboxExpungeError,
    MailboxFetchError,
    MailboxFlagError,
    MailboxFolderCreateError,
    MailboxFolderDeleteError,
    MailboxFolderRenameError,
    MailboxFolderSelectError,
    MailboxFolderStatusError,
    MailboxFolderStatusValueError,
    MailboxFolderSubscribeError,
    MailboxLoginError,
    MailboxLogoutError,
    MailboxMoveError,
    MailboxNumbersError,
    MailboxStarttlsError,
    MailboxTaggedResponseError,
    MailboxUidsError,
    UnexpectedCommandStatusError,
)
from .folder import FolderInfo, MailBoxFolderManager
from .mailbox import BaseMailBox, MailBox, MailBoxStartTls, MailBoxUnencrypted
from .message import LazyHeaders, MailAttachment, MailMessage
from .query import AND, NOT, OR, A, H, Header, N, O, U, UidRange
from .utils import EmailAddress

__version__ = '1.17.0'

__all__ = [
    'A', 'AND', 'BaseMailBox', 'Catalog', 'CatalogConflict', 'CatalogError', 'CopyUid', 'CopyUidMapping',
    'EmailAddress', 'FolderInfo', 'FolderObservation', 'FolderRecord', 'H', 'Header', 'ImapToolsError',
    'MailAttachment', 'MailBox', 'MailBoxFolderManager', 'MailBoxFolderStatusOptions', 'MailBoxStartTls',
    'MailBoxUnencrypted', 'MailMessage', 'MailMessageFlags', 'MailboxAppendError', 'MailboxCopyError',
    'MailboxDeleteError', 'MailboxExpungeError', 'MailboxFetchError', 'MailboxFlagError', 'MailboxFolderCreateError',
    'MailboxFolderDeleteError', 'MailboxFolderRenameError', 'MailboxFolderSelectError', 'MailboxFolderStatusError',
    'MailboxFolderStatusValueError', 'MailboxFolderSubscribeError', 'MailboxLoginError', 'MailboxLogoutError',
    'MailboxMoveError', 'MailboxNumbersError', 'MailboxStarttlsError', 'MailboxTaggedResponseError', 'MailboxUidsError',
    'MessageOccurrence', 'MoveEvidence', 'MoveEvidenceError', 'MoveReport',
    'N', 'NOT', 'O', 'OR', 'ReconcileReport', 'RenameEvent', 'ServerIdentity', 'SortCriteria', 'U', 'UidRange',
    'UnexpectedCommandStatusError', '__version__', 'capture_observations', 'expand_uid_set', 'LazyHeaders',
    'parse_copyuid',
]
