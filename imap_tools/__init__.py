# Lib author: Vladimir Kaukin <KaukinVK@ya.ru>
# Project home page: https://github.com/ikvk/imap_tools
# Mirror: https://gitflic.ru/project/ikvk/imap-tools
# License: Apache-2.0

from .consts import MailBoxFolderStatusOptions, MailMessageFlags, SortCriteria
from .errors import (
    FilterConditionError,
    FilterError,
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
    MaterialFetchError,
    UnexpectedCommandStatusError,
)
from .filter import (
    And,
    AndNode,
    Condition,
    Decision,
    FetchResult,
    ImapMaterialFetcher,
    LocalVerifier,
    Material,
    MaterialFetcher,
    MaterialStore,
    Not,
    NotNode,
    Or,
    OrNode,
    ServerCapabilities,
    VerificationResult,
    attachment_content,
    attachment_name,
    bcc,
    body_text,
    build_candidate_query,
    cc,
    date_gte,
    date_lt,
    date_on,
    filter_uids,
    flag,
    from_,
    header,
    message_text,
    size_gt,
    size_lt,
    split_candidates,
    subject,
    to,
)
from .folder import FolderInfo, MailBoxFolderManager
from .mailbox import BaseMailBox, MailBox, MailBoxStartTls, MailBoxUnencrypted
from .message import LazyHeaders, MailAttachment, MailMessage
from .query import AND, NOT, OR, A, H, Header, N, O, U, UidRange
from .utils import EmailAddress

__version__ = '1.16.0'

__all__ = [
    'A', 'AND', 'And', 'AndNode', 'BaseMailBox', 'Condition', 'Decision', 'EmailAddress', 'FetchResult',
    'FilterConditionError', 'FilterError', 'FolderInfo', 'H', 'Header', 'ImapMaterialFetcher', 'ImapToolsError',
    'LocalVerifier', 'MailAttachment', 'MailBox', 'MailBoxFolderManager', 'MailBoxFolderStatusOptions',
    'MailBoxStartTls', 'MailBoxUnencrypted', 'MailMessage', 'MailMessageFlags', 'MailboxAppendError',
    'MailboxCopyError', 'MailboxDeleteError', 'MailboxExpungeError', 'MailboxFetchError', 'MailboxFlagError',
    'MailboxFolderCreateError', 'MailboxFolderDeleteError', 'MailboxFolderRenameError',
    'MailboxFolderSelectError', 'MailboxFolderStatusError', 'MailboxFolderStatusValueError',
    'MailboxFolderSubscribeError', 'MailboxLoginError', 'MailboxLogoutError', 'MailboxMoveError',
    'MailboxNumbersError', 'MailboxStarttlsError', 'MailboxTaggedResponseError', 'MailboxUidsError', 'Material',
    'MaterialFetchError', 'MaterialFetcher', 'MaterialStore', 'N', 'NOT', 'Not', 'NotNode', 'O', 'OR', 'Or',
    'OrNode', 'ServerCapabilities', 'SortCriteria', 'U', 'UidRange', 'UnexpectedCommandStatusError',
    'VerificationResult', '__version__', 'LazyHeaders', 'attachment_content', 'attachment_name', 'body_text',
    'build_candidate_query', 'cc', 'bcc', 'date_gte', 'date_lt', 'date_on', 'filter_uids', 'flag', 'from_',
    'header', 'message_text', 'size_gt', 'size_lt', 'split_candidates', 'subject', 'to',
]
