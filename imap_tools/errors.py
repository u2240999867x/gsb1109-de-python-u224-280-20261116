from typing import Any


class ImapToolsError(Exception):
    """Base lib error"""


class MailboxFolderStatusValueError(ImapToolsError):
    """Wrong folder status value error"""


class UnexpectedCommandStatusError(ImapToolsError):
    """Unexpected status in IMAP command response"""

    def __init__(self, command_result: tuple, expected: Any) -> None:
        """
        :param command_result: imap command result
        :param expected: expected command status
        """
        self.command_result = command_result
        self.expected = expected

    def __str__(self):
        return (f'Response status "{self.expected}" expected, '
                f'but "{self.command_result[0]}" received. Data: {str(self.command_result[1])}')


class MailboxFolderSelectError(UnexpectedCommandStatusError):
    pass


class MailboxFolderUnselectError(UnexpectedCommandStatusError):
    pass


class MailboxFolderCreateError(UnexpectedCommandStatusError):
    pass


class MailboxFolderRenameError(UnexpectedCommandStatusError):
    pass


class MailboxFolderDeleteError(UnexpectedCommandStatusError):
    pass


class MailboxFolderStatusError(UnexpectedCommandStatusError):
    pass


class MailboxFolderSubscribeError(UnexpectedCommandStatusError):
    pass


class MailboxLoginError(UnexpectedCommandStatusError):
    pass


class MailboxLogoutError(UnexpectedCommandStatusError):
    pass


class MailboxNumbersError(UnexpectedCommandStatusError):
    pass


class MailboxUidsError(UnexpectedCommandStatusError):
    pass


class MailboxStarttlsError(UnexpectedCommandStatusError):
    pass


class MailboxFetchError(UnexpectedCommandStatusError):
    pass


class MailboxExpungeError(UnexpectedCommandStatusError):
    pass


class MailboxDeleteError(UnexpectedCommandStatusError):
    pass


class MailboxCopyError(UnexpectedCommandStatusError):
    pass


class MailboxMoveError(UnexpectedCommandStatusError):
    pass


class MailboxFlagError(UnexpectedCommandStatusError):
    pass


class MailboxAppendError(UnexpectedCommandStatusError):
    pass


class MailboxTaggedResponseError(UnexpectedCommandStatusError):
    pass


class MessagePartialError(ImapToolsError):
    """Message operation requires a part that has not been fetched"""


class MessageGenerationError(ImapToolsError):
    """Message UID refers to another folder or another UIDVALIDITY generation"""


class MessageFolderMismatchError(MessageGenerationError):
    """Backfill requested from a folder other than the message source folder"""


class MessageStructureError(MessagePartialError):
    """Server did not return BODYSTRUCTURE needed to address message materials"""


class MessageMaterialError(MessagePartialError):
    """Selected material is unknown or missing at the server"""

    def __init__(self, message: str, sections: tuple = ()) -> None:
        super().__init__(message)
        self.sections = sections
