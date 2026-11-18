# Lib author: Vladimir Kaukin <KaukinVK@ya.ru>
# Project home page: https://github.com/ikvk/imap_tools
# Mirror: https://gitflic.ru/project/ikvk/imap-tools
# License: Apache-2.0

from .attachment_filename import (
    FilenameInfo,
    FilenameIssue,
    FilenameSegment,
    build_filename_info,
    safe_filename,
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

__version__ = '1.16.0'

__all__ = [
    'A', 'AND', 'BaseMailBox', 'EmailAddress', 'FilenameInfo', 'FilenameIssue', 'FilenameSegment',
    'FolderInfo', 'H', 'Header', 'ImapToolsError', 'MailAttachment',
    'MailBox', 'MailBoxFolderManager', 'MailBoxFolderStatusOptions', 'MailBoxStartTls', 'MailBoxUnencrypted',
    'MailMessage', 'MailMessageFlags', 'MailboxAppendError', 'MailboxCopyError', 'MailboxDeleteError',
    'MailboxExpungeError', 'MailboxFetchError', 'MailboxFlagError', 'MailboxFolderCreateError',
    'MailboxFolderDeleteError', 'MailboxFolderRenameError', 'MailboxFolderSelectError', 'MailboxFolderStatusError',
    'MailboxFolderStatusValueError', 'MailboxFolderSubscribeError', 'MailboxLoginError', 'MailboxLogoutError',
    'MailboxMoveError', 'MailboxNumbersError', 'MailboxStarttlsError', 'MailboxTaggedResponseError',
    'MailboxUidsError',
    'N', 'NOT', 'O', 'OR', 'SortCriteria', 'U', 'UidRange', 'UnexpectedCommandStatusError', '__version__',
    'LazyHeaders', 'build_filename_info', 'safe_filename',
]
