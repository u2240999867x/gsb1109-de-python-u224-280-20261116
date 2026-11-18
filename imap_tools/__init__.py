# Lib author: Vladimir Kaukin <KaukinVK@ya.ru>
# Project home page: https://github.com/ikvk/imap_tools
# Mirror: https://gitflic.ru/project/ikvk/imap-tools
# License: Apache-2.0

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
from .unsubscribe import (
    ConsentRecord,
    HeaderOccurrence,
    HeaderReference,
    OneClickRequest,
    SignatureVerification,
    SubscriptionCatalog,
    TargetKind,
    UnsubscribeDeclaration,
    UnsubscribeReport,
    UnsubscribeTarget,
    analyze_headers,
    build_subscription_catalog,
    build_unsubscribe_report,
    header_occurrences,
)
from .utils import EmailAddress

__version__ = '1.16.0'

__all__ = [
    'A', 'AND', 'BaseMailBox', 'ConsentRecord', 'EmailAddress', 'FolderInfo', 'H', 'Header', 'HeaderOccurrence',
    'HeaderReference', 'ImapToolsError', 'MailAttachment', 'MailBox', 'MailBoxFolderManager',
    'MailBoxFolderStatusOptions', 'MailBoxStartTls', 'MailBoxUnencrypted', 'MailMessage', 'MailMessageFlags',
    'MailboxAppendError', 'MailboxCopyError', 'MailboxDeleteError', 'MailboxExpungeError', 'MailboxFetchError',
    'MailboxFlagError', 'MailboxFolderCreateError', 'MailboxFolderDeleteError', 'MailboxFolderRenameError',
    'MailboxFolderSelectError', 'MailboxFolderStatusError', 'MailboxFolderStatusValueError',
    'MailboxFolderSubscribeError', 'MailboxLoginError', 'MailboxLogoutError', 'MailboxMoveError',
    'MailboxNumbersError', 'MailboxStarttlsError', 'MailboxTaggedResponseError', 'MailboxUidsError', 'N', 'NOT',
    'O', 'OR', 'OneClickRequest', 'SignatureVerification', 'SortCriteria', 'SubscriptionCatalog', 'TargetKind',
    'U', 'UidRange', 'UnexpectedCommandStatusError', 'UnsubscribeDeclaration', 'UnsubscribeReport',
    'UnsubscribeTarget', '__version__', 'analyze_headers', 'build_subscription_catalog',
    'build_unsubscribe_report', 'header_occurrences', 'LazyHeaders',
]
