import datetime
import imaplib
import re
from collections import UserString
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, TypeVar, Union

from .body_structure import extract_bodystructure, parse_section_literals, section_sort_key
from .consts import MOVE_RESULT_TAG, PYTHON_VERSION_MINOR, UID_PATTERN
from .errors import (
    MailboxAppendError,
    MailboxCopyError,
    MailboxDeleteError,
    MailboxExpungeError,
    MailboxFetchError,
    MailboxFlagError,
    MailboxLoginError,
    MailboxLogoutError,
    MailboxMoveError,
    MailboxNumbersError,
    MailboxStarttlsError,
    MailboxTaggedResponseError,
    MailboxUidsError,
    MessageFolderMismatchError,
    MessageGenerationError,
    MessageMaterialError,
    MessageStructureError,
)
from .folder import MailBoxFolderManager
from .idle import IdleManager
from .message import MailMessage, MaterialRef, MessagePartState, MessageRef
from .utils import (
    StrOrBytes,
    check_command_status,
    check_timeout_arg_support,
    chunked_crop,
    clean_flags,
    clean_uids,
    encode_folder,
    replace_html_ct_charset,
)

# Maximal line length when calling readline(). This is to prevent reading arbitrary length lines.
# 20Mb is enough for search response with about 2 000 000 message numbers
imaplib._MAXLINE = 20 * 1024 * 1024  # 20Mb

Criteria = Union[StrOrBytes, UserString]
Self = TypeVar("Self", bound="BaseMailBox")


def _create_imap4_client(host: str, port: int, timeout: Optional[float]) -> imaplib.IMAP4:
    if PYTHON_VERSION_MINOR < 9:
        return imaplib.IMAP4(host, port)
    elif PYTHON_VERSION_MINOR < 12:
        return imaplib.IMAP4(host, port, timeout)
    else:
        return imaplib.IMAP4(host, port, timeout=timeout)


def _create_imap4_ssl_client(
        host: str, port: int, keyfile, certfile, ssl_context, timeout: Optional[float]) -> imaplib.IMAP4_SSL:
    if PYTHON_VERSION_MINOR < 9:
        return imaplib.IMAP4_SSL(host, port, keyfile, certfile, ssl_context)  # noqa
    elif PYTHON_VERSION_MINOR < 12:
        return imaplib.IMAP4_SSL(host, port, keyfile, certfile, ssl_context, timeout)  # noqa
    else:
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl_context, timeout=timeout)


def split_bulk_fetch_items(fetch_data: list) -> Iterator[list]:
    """Split a bulk FETCH response into per-message item groups.

    A message may produce several literal tuples (HEADER, BODYSTRUCTURE, BODY[..]),
    so pairing adjacent items by two is unsafe. A new group starts when a prefix
    carries `UID <n>` together with the message sequence number.
    """
    current = []
    for item in fetch_data:
        if isinstance(item, tuple) and re.search(rb'^\d+ \(', item[0] or b''):
            if current:
                yield current
            current = [item]
        elif current:
            current.append(item)
        else:
            current = [item]
    if current:
        yield current


def _reverse_groups(groups, reverse: bool):
    """Reverse message groups (not items inside a group, which carry UID/section)."""
    groups = list(groups)
    return reversed(groups) if reverse else groups


class BaseMailBox:
    """Working with the email box"""

    email_message_class = MailMessage
    folder_manager_class = MailBoxFolderManager
    idle_manager_class = IdleManager

    def __init__(self) -> None:
        self.client = self._get_mailbox_client()
        self.folder = self.folder_manager_class(self)
        self.idle = self.idle_manager_class(self)
        self.login_result = None

    def __enter__(self: Self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        self.logout()

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        raise NotImplementedError

    def consume_until_tagged_response(self, tag: bytes):
        """Waiting for tagged response"""
        tagged_commands = self.client.tagged_commands
        response_set = []
        while True:
            # response example: b'IJDH3 OK IDLE Terminated'
            response: bytes = self.client._get_response()  # noqa
            if tagged_commands[tag]:
                break
            response_set.append(response)
        result = tagged_commands.pop(tag)
        check_command_status(result, MailboxTaggedResponseError)
        return result, response_set

    def login(self: Self, username: str, password: str, initial_folder: Optional[str] = 'INBOX') -> Self:
        """Authenticate to account"""
        login_result = (
            self.client._simple_command('LOGIN', self.client._quote(username), self.client._quote(password)))  # noqa
        check_command_status(login_result, MailboxLoginError)
        self.client.state = 'AUTH'  # logic from self.client.login
        if initial_folder is not None:
            self.folder.set(initial_folder)
        self.login_result = login_result
        return self  # return self in favor of context manager

    def login_utf8(self: Self, username: str, password: str, initial_folder: Optional[str] = 'INBOX') -> Self:
        """Authenticate to an account with a UTF-8 username and/or password"""
        # rfc2595 section 6 - PLAIN SASL mechanism
        encoded = (b"\0" + username.encode("utf8") + b"\0" + password.encode("utf8"))
        # Assumption is the server supports AUTH=PLAIN capability
        login_result = self.client.authenticate("PLAIN", lambda x: encoded)
        check_command_status(login_result, MailboxLoginError)
        if initial_folder is not None:
            self.folder.set(initial_folder)
        self.login_result = login_result
        return self

    def xoauth2(self: Self, username: str, access_token: str, initial_folder: Optional[str] = 'INBOX') -> Self:
        """Authenticate to account using OAuth 2.0 mechanism"""
        auth_string = f'user={username}\1auth=Bearer {access_token}\1\1'
        result = self.client.authenticate('XOAUTH2', lambda x: auth_string)  # noqa
        check_command_status(result, MailboxLoginError)
        if initial_folder is not None:
            self.folder.set(initial_folder)
        self.login_result = result
        return self

    def logout(self) -> tuple:
        """Informs the server that the client is done with the connection"""
        result = self.client.logout()
        check_command_status(result, MailboxLogoutError, expected='BYE')
        return result

    def numbers(self, criteria: Criteria = 'ALL', charset: Optional[str] = 'US-ASCII') -> List[str]:
        """
        Search mailbox for matching message numbers in current folder (this is not uids)
        Message Sequence Number Message Attribute - to accessing messages by relative position in the mailbox,
        it also can be used in mathematical calculations, see rfc3501.
        :param criteria: message search criteria (see examples at ./doc/imap_search_criteria.txt)
        :param charset: IANA charset, indicates charset of the strings that appear in the search criteria. See rfc2978
                        Some servers reject an explicit CHARSET, use None for them
        :return email message numbers
        """
        encoded_criteria = criteria if type(criteria) is bytes else str(criteria).encode(charset or 'us-ascii')
        # self.client.search can proc charset=None
        search_result = self.client.search(charset, encoded_criteria)
        check_command_status(search_result, MailboxNumbersError)
        return search_result[1][0].decode().split() if search_result[1][0] else []

    def numbers_to_uids(self, numbers: List[str]) -> List[str]:
        """Get message uids by message numbers"""
        if not numbers:
            return []
        fetch_result = self.client.fetch(','.join(numbers), "(UID)")
        check_command_status(fetch_result, MailboxFetchError)
        result = []
        for raw_uid_item in fetch_result[1]:
            uid_match = re.search(UID_PATTERN, (raw_uid_item or b'').decode())
            if uid_match:
                result.append(uid_match.group('uid'))
        return result

    def uids(self, criteria: Criteria = 'ALL', charset: Optional[str] = 'US-ASCII',
             sort: Optional[Union[str, Iterable[str]]] = None) -> List[str]:
        """
        Search mailbox for matching message uids in current folder
        :param criteria: message search criteria (see examples at ./doc/imap_search_criteria.txt)
        :param charset: IANA charset, indicates charset of the strings that appear in the search criteria. See rfc2978
                        Some servers reject an explicit CHARSET, use None for them
        :param sort: criteria for sort messages on server, use SortCriteria constants. Charset arg is important for sort
        :return: email message uids
        """
        encoded_criteria = criteria if type(criteria) is bytes else str(criteria).encode(charset or 'us-ascii')
        if sort:
            sort = (sort,) if isinstance(sort, str) else sort
            sort_cmd = f'({" ".join(sort)})'
            if charset is None:
                uid_result = self.client.uid('SORT', sort_cmd, encoded_criteria)
            else:
                uid_result = self.client.uid('SORT', sort_cmd, charset, encoded_criteria)
        else:
            if charset is None:
                uid_result = self.client.uid('SEARCH', encoded_criteria)
            else:
                uid_result = self.client.uid('SEARCH', 'CHARSET', charset, encoded_criteria)
        check_command_status(uid_result, MailboxUidsError)
        return uid_result[1][0].decode().split() if uid_result[1][0] else []

    def _fetch_by_one(self, uid_list: Sequence[str], message_parts: str) -> Iterator[list]:
        for uid in uid_list:
            fetch_result = self.client.uid('fetch', uid, message_parts)
            check_command_status(fetch_result, MailboxFetchError)
            if not fetch_result[1] or fetch_result[1][0] is None:
                continue
            yield fetch_result[1]

    def _fetch_in_bulk(self, uid_list: Sequence[str], message_parts: str, reverse: bool, bulk: int) \
            -> Iterator[list]:
        if not uid_list:
            return

        if isinstance(bulk, int) and bulk >= 2:
            uid_list_seq = chunked_crop(uid_list, bulk)
        elif isinstance(bulk, bool):
            uid_list_seq = (uid_list,)
        else:
            raise ValueError('bulk arg may be bool or int >= 2')

        for uid_list_i in uid_list_seq:
            fetch_result = self.client.uid('fetch', ','.join(uid_list_i), message_parts)
            check_command_status(fetch_result, MailboxFetchError)
            if not fetch_result[1] or fetch_result[1][0] is None:
                return
            for built_fetch_item in _reverse_groups(split_bulk_fetch_items(fetch_result[1]), reverse):
                yield built_fetch_item

    def fetch(self,
              criteria: Criteria = 'ALL',
              charset: Optional[str] = 'US-ASCII',
              *,
              limit: Optional[Union[int, slice]] = None,
              mark_seen=True,
              reverse=False,
              headers_only=False,
              bulk: Union[bool, int] = False,
              sort: Optional[Union[str, Iterable[str]]] = None,
              uid_list: Union[str, Iterable[str]] = None,
              structure: Optional[bool] = None) \
            -> Iterator[MailMessage]:
        """
        Mail message generator in current folder by search criteria
        :param criteria: message search criteria (see examples at ./doc/imap_search_criteria.txt)
        :param charset: IANA charset, indicates charset of the strings that appear in the search criteria. See rfc2978
                        Some servers reject an explicit CHARSET, use None for them
        :param limit: int | slice - limit number of read emails | slice emails range for read
                      useful for actions with a large number of messages, like "move" | paging
        :param mark_seen: mark emails as seen on fetch
        :param reverse: in order from the larger date to the smaller, works at client side
        :param headers_only: get only email headers (without text, html, attachments)
        :param bulk:
            False - fetch each message separately per N commands - low memory consumption, slow
            True  - fetch all messages per 1 command - high memory consumption, fast. Fails on big bulk at server
            int - fetch messages by bulks of the specified size
        :param sort: criteria for sort messages on server, use SortCriteria constants. Charset arg is important for sort
        :param uid_list: UIDs for fetch. If set: (criteria, charset, sort) will be ignored, SEARCH will not be used.
        :param structure: request BODYSTRUCTURE for material refs. Defaults to True with headers_only.
        :return generator: MailMessage
        """
        if structure is None:
            structure = bool(headers_only)
        parts = [f"BODY{'' if mark_seen else '.PEEK'}[{'HEADER' if headers_only else ''}]", 'UID', 'FLAGS',
                 'RFC822.SIZE']
        if structure:
            parts.append('BODYSTRUCTURE')
        message_parts = f"({' '.join(parts)})"
        limit_range = slice(0, limit) if type(limit) is int else limit or slice(None)
        if not isinstance(limit_range, slice):
            raise TypeError(f"slice expected, but received: {type(limit_range).__name__}")
        if uid_list:
            uids = clean_uids(uid_list)
        else:
            uids = self.uids(criteria, charset, sort)
        uids = tuple((reversed if reverse else iter)(uids))[limit_range]
        folder = self.folder.get()
        uidvalidity = self.folder.uidvalidity
        if bulk:
            message_generator = self._fetch_in_bulk(uids, message_parts, reverse, bulk)
        else:
            message_generator = self._fetch_by_one(uids, message_parts)
        for fetch_item in message_generator:
            raw_structure = extract_bodystructure(fetch_item) if structure else ''
            message = self.email_message_class(fetch_item)
            message.set_fetch_context(
                MessageRef(folder, uidvalidity, message.uid), headers_only, raw_structure)
            yield message

    def fetch_status(self, uid_list: Union[str, Iterable[str]], bulk: Union[bool, int] = False) \
            -> Iterator[MailMessage]:
        """Fetch message headers without bodies, keeping independent material refs.

        Same as fetch(uid_list=..., headers_only=True, mark_seen=False).
        """
        return self.fetch(uid_list=uid_list, headers_only=True, mark_seen=False, bulk=bulk)

    def fetch_body(self, messages: Iterable[MailMessage], bulk: Union[bool, int] = False) -> List[MailMessage]:
        """Backfill body materials for messages previously fetched with headers_only.

        UIDVALIDITY of each message is compared with the currently selected folder
        generation. Stale messages are rejected, not silently merged.
        """
        messages = list(messages)
        if not messages:
            return []
        self._check_message_generation(messages)
        needs_structure = [msg for msg in messages if not msg.part_refs]
        if needs_structure:
            self._attach_structures(needs_structure)
        to_fetch = [msg for msg in messages if msg.body_refs]
        self._fetch_materials(to_fetch, lambda msg: msg.body_refs, bulk)
        for msg in messages:
            msg._refresh_body_state()
            msg._invalidate_parsed_content()
            self._apply_text_materials(msg)
        return messages

    def fetch_attachments(self, messages: Iterable[MailMessage], selectors=None,
                          bulk: Union[bool, int] = False) -> List[MailMessage]:
        """Fetch selected attachment materials, leaving unselected refs pending.

        selectors: None for all attachments, or a single/iterable of section ids,
        filenames, content-ids, or attachment indexes. Missing refs are marked,
        but the already complete header scope is left untouched.
        """
        messages = list(messages)
        if not messages:
            return []
        self._check_message_generation(messages)
        needs_structure = [msg for msg in messages if not msg.part_refs]
        if needs_structure:
            self._attach_structures(needs_structure)
        if selectors is None:
            for msg in messages:
                for item in msg.attachment_refs:
                    item.requested = True
            self._fetch_materials([msg for msg in messages if msg.attachment_refs],
                                  lambda msg: msg.attachment_refs, bulk)
        else:
            selector_list = [selectors] if isinstance(selectors, (str, int)) else list(selectors)
            for msg in messages:
                selected = []
                unresolved = []
                for selector in selector_list:
                    item = msg.attachment_material(selector)
                    if item is None:
                        unresolved.append(selector)
                    else:
                        selected.append(item)
                if unresolved:
                    raise MessageMaterialError(
                        f'attachment material(s) not found: {unresolved}', sections=tuple(unresolved))
                for item in selected:
                    item.requested = True
                if selected:
                    self._fetch_materials([msg], lambda msg, refs=tuple(selected): refs, bulk)
        for msg in messages:
            msg._refresh_attachments_state()
        return messages

    def _check_message_generation(self, messages: List[MailMessage]) -> None:
        current_folder = self.folder.get()
        # Prefer a fresh STATUS: the cached value from SELECT can go stale if the
        # folder is recreated by another client.
        current_uidvalidity = None
        try:
            current_uidvalidity = self.folder.status(options=('UIDVALIDITY',)).get('UIDVALIDITY')
        except Exception:  # noqa
            current_uidvalidity = None
        if current_uidvalidity is None:
            current_uidvalidity = self.folder.uidvalidity
        if current_folder is None or current_uidvalidity is None:
            raise MessageGenerationError(
                'current folder generation is unknown, cannot verify UID ownership for backfill')
        for msg in messages:
            if msg.ref is None or not msg.ref.complete:
                raise MessageGenerationError(
                    'message has no folder/UIDVALIDITY/UID reference, backfill is unsafe')
            if msg.ref.folder != current_folder:
                raise MessageFolderMismatchError(
                    f'message ref folder {msg.ref.folder!r} does not match selected folder {current_folder!r}')
            if msg.ref.uidvalidity != current_uidvalidity:
                raise MessageGenerationError(
                    f'message UIDVALIDITY {msg.ref.uidvalidity!r} does not match folder generation '
                    f'{current_uidvalidity!r}, refetch headers from the current folder')
        if len({(msg.folder, msg.uidvalidity) for msg in messages}) > 1:
            raise MessageGenerationError('messages belong to different folder generations')

    def _attach_structures(self, messages: List[MailMessage]) -> None:
        by_uid = {msg.uid: msg for msg in messages if msg.uid}
        if not by_uid:
            raise MessageStructureError('messages have no UIDs to request BODYSTRUCTURE for')
        fetch_result = self.client.uid('fetch', ','.join(by_uid), '(UID BODYSTRUCTURE)')
        check_command_status(fetch_result, MailboxFetchError)
        found = 0
        for group in split_bulk_fetch_items(fetch_result[1] or ()):
            uid = ''
            for item in group:
                if isinstance(item, tuple):
                    uid_match = re.search(UID_PATTERN, (item[0] or b'').decode('utf-8', 'replace'))
                else:
                    uid_match = re.search(UID_PATTERN, (item or b'').decode('utf-8', 'replace'))
                if uid_match:
                    uid = uid_match.group('uid')
                    break
            msg = by_uid.get(uid)
            if msg is None:
                continue
            structure = extract_bodystructure(group)
            if structure:
                msg.apply_structure(structure)
                found += 1
        if not found and messages:
            raise MessageStructureError('server returned no BODYSTRUCTURE for requested messages')

    def _fetch_materials(self, messages: List[MailMessage], refs_getter, bulk: Union[bool, int]) -> None:
        if not messages:
            return
        pending = []
        for msg in messages:
            for item in refs_getter(msg):
                if not item.complete:
                    item.state = MessagePartState.PENDING
                    pending.append((msg, item))
        if not pending:
            return
        section_map: Dict[str, List[Tuple[MailMessage, MaterialRef]]] = {}
        for msg, item in pending:
            if item.section:
                section_map.setdefault(item.section, []).append((msg, item))
        sections = sorted(section_map, key=section_sort_key)
        if not sections:
            return
        if bulk is False:
            chunks = [[section] for section in sections]
        elif bulk is True:
            chunks = [sections]
        elif isinstance(bulk, int) and bulk >= 2:
            chunks = [sections[i:i + bulk] for i in range(0, len(sections), bulk)]
        else:
            raise ValueError('bulk arg may be bool or int >= 2')
        uid_list = ','.join(sorted({msg.uid for msg, _ in pending}))
        for section_chunk in chunks:
            parts = ' '.join(f'BODY.PEEK[{section}]' for section in section_chunk)
            fetch_result = self.client.uid('fetch', uid_list, f'(UID {parts})')
            check_command_status(fetch_result, MailboxFetchError)
            returned = parse_section_literals(fetch_result[1])
            for uid, section_items in returned.items():
                for msg, item in pending:
                    if msg.uid != uid:
                        continue
                    if item.section in section_items:
                        item.set_payload(section_items[item.section])
            for section in section_chunk:
                for _, item in section_map[section]:
                    if item.state == MessagePartState.PENDING:
                        item.mark_missing()

    @staticmethod
    def _apply_text_materials(msg: MailMessage) -> None:
        """Make backfilled body materials visible through msg.text / msg.html.

        This path is used for header-fetched messages whose `obj` contains no
        body, so text always comes from material bytes (never from declared sizes).
        """
        texts = []
        htmls = []
        for item in msg.body_refs:
            if not item.complete:
                continue
            text = item.decoded_text
            if item.content_type == 'text/html':
                htmls.append(replace_html_ct_charset(text, 'utf-8'))
            else:
                texts.append(text)
        msg._body_text_override = ''.join(texts)
        msg._body_html_override = ''.join(htmls)

    def expunge(self) -> tuple:
        result = self.client.expunge()
        check_command_status(result, MailboxExpungeError)
        return result

    def delete(self, uid_list: Union[str, Iterable[str]], chunks: Optional[int] = None) \
            -> Optional[List[Tuple[tuple, tuple]]]:
        """
        Delete email messages
        Do nothing on empty uid_list
        :param uid_list: UIDs for delete
        :param chunks: Number of UIDs to process at once, to avoid server errors on large set. Proc all at once if None.
        :return: None on empty uid_list, command results otherwise
        """
        cleaned_uid_list = clean_uids(uid_list)
        if not cleaned_uid_list:
            return None
        results = []
        for cleaned_uid_list_i in chunked_crop(cleaned_uid_list, chunks):
            store_result = self.client.uid('STORE', ','.join(cleaned_uid_list_i), '+FLAGS', r'(\Deleted)')
            check_command_status(store_result, MailboxDeleteError)
            expunge_result = self.expunge()
            results.append((store_result, expunge_result))
        return results

    def copy(self, uid_list: Union[str, Iterable[str]], destination_folder: StrOrBytes, chunks: Optional[int] = None) \
            -> Optional[List[tuple]]:
        """
        Copy email messages into the specified folder.
        Do nothing on empty uid_list.
        :param uid_list: UIDs for copy
        :param destination_folder: Folder for email copies
        :param chunks: Number of UIDs to process at once, to avoid server errors on large set. Proc all at once if None.
        :return: None on empty uid_list, command results otherwise
        """
        cleaned_uid_list = clean_uids(uid_list)
        if not cleaned_uid_list:
            return None
        results = []
        for cleaned_uid_list_i in chunked_crop(cleaned_uid_list, chunks):
            copy_result = self.client.uid(
                'COPY', ','.join(cleaned_uid_list_i), encode_folder(destination_folder))  # noqa
            check_command_status(copy_result, MailboxCopyError)
            results.append(copy_result)
        return results

    def move(self, uid_list: Union[str, Iterable[str]], destination_folder: StrOrBytes, chunks: Optional[int] = None) \
            -> Optional[List[Tuple[tuple, tuple]]]:
        """
        Move email messages into the specified folder.
        Do nothing on empty uid_list.
        :param uid_list: UIDs for move
        :param destination_folder: Folder for move to
        :param chunks: Number of UIDs to process at once, to avoid server errors on large set. Proc all at once if None.
        :return: None on empty uid_list, command results otherwise
        """
        cleaned_uid_list = clean_uids(uid_list)
        if not cleaned_uid_list:
            return None
        if 'MOVE' in self.client.capabilities:
            # server side move
            results = []
            for cleaned_uid_list_i in chunked_crop(cleaned_uid_list, chunks):
                move_result = self.client.uid(
                    'MOVE', ','.join(cleaned_uid_list_i), encode_folder(destination_folder))  # noqa
                check_command_status(move_result, MailboxMoveError)
                results.append((move_result, MOVE_RESULT_TAG))
            return results
        else:
            # client side move
            results = []
            for cleaned_uid_list_i in chunked_crop(cleaned_uid_list, chunks):
                copy_result = self.copy(cleaned_uid_list_i, destination_folder)
                delete_result = self.delete(cleaned_uid_list_i)
                results.append((copy_result, delete_result))
            return results

    def flag(self, uid_list: Union[str, Iterable[str]], flag_set: Union[str, Iterable[str]], value: bool,
             chunks: Optional[int] = None) -> Optional[List[Tuple[tuple, tuple]]]:
        """
        Set/unset email flags.
        Do nothing on empty uid_list.
        System flags contains in consts.MailMessageFlags.all
        :param uid_list: UIDs for set flag
        :param flag_set: Flags for operate
        :param value: Should the flags be set: True - yes, False - no
        :param chunks: Number of UIDs to process at once, to avoid server errors on large set. Proc all at once if None.
        :return: None on empty uid_list, command results otherwise
        """
        cleaned_uid_list = clean_uids(uid_list)
        if not cleaned_uid_list:
            return None
        cleaned_flags = clean_flags(flag_set)
        results = []
        for cleaned_uid_list_i in chunked_crop(cleaned_uid_list, chunks):
            store_result = self.client.uid(
                'STORE',
                ','.join(cleaned_uid_list_i),
                ('+' if value else '-') + 'FLAGS',
                f'({" ".join(cleaned_flags)})'
            )
            check_command_status(store_result, MailboxFlagError)
            expunge_result = self.expunge()
            results.append((store_result, expunge_result))
        return results

    def append(self, message: Union[MailMessage, bytes],
               folder: StrOrBytes = 'INBOX',
               dt: Optional[datetime.datetime] = None,
               flag_set: Optional[Union[str, Iterable[str]]] = None) -> tuple:
        """
        Append email messages to server
        :param message: MailMessage object or bytes
        :param folder: destination folder, INBOX by default
        :param dt: email message datetime with tzinfo, now by default, imaplib.Time2Internaldate types supported
        :param flag_set: email message flags, no flags by default. System flags at consts.MailMessageFlags.all
        :return: command results
        """
        timezone = datetime.datetime.now().astimezone().tzinfo  # system timezone
        cleaned_flags = clean_flags(flag_set or [])
        typ, dat = self.client.append(
            encode_folder(folder),  # noqa
            f'({" ".join(cleaned_flags)})' if cleaned_flags else None,
            dt or datetime.datetime.now(timezone),  # noqa
            message if type(message) is bytes else message.obj.as_bytes()
        )
        append_result = (typ, dat)
        check_command_status(append_result, MailboxAppendError)
        return append_result


class MailBox(BaseMailBox):
    """Working with the email box through IMAP4 over SSL/TLS connection with imaplib.IMAP4_SSL"""

    def __init__(self, host='', port=993, timeout=None, keyfile=None, certfile=None, ssl_context=None) -> None:
        """
        :param host: host's name (default: localhost)
        :param port: port number
        :param timeout: timeout in seconds for the connection attempt, since python 3.9
        :param keyfile: PEM formatted file that contains your private key (deprecated)
        :param certfile: PEM formatted certificate chain file (deprecated)
        :param ssl_context: SSLContext object that contains your certificate chain and private key
        Since Python 3.9 timeout argument added
        Since Python 3.12 keyfile and certfile arguments are deprecated, ssl_context and timeout must be keyword args
        """
        check_timeout_arg_support(timeout)
        self._host = host
        self._port = port
        self._timeout = timeout
        self._keyfile = keyfile
        self._certfile = certfile
        self._ssl_context = ssl_context
        super().__init__()

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        return _create_imap4_ssl_client(
            self._host, self._port, self._keyfile, self._certfile, self._ssl_context, self._timeout)


class MailBoxUnencrypted(BaseMailBox):
    """Working with the email box through IMAP4 without encryption. Do NOT use it on the public internet!"""

    def __init__(self, host='', port=143, timeout=None) -> None:
        """
        :param host: host's name (default: localhost)
        :param port: port number
        :param timeout: timeout in seconds for the connection attempt, since python 3.9
        """
        check_timeout_arg_support(timeout)
        self._host = host
        self._port = port
        self._timeout = timeout
        super().__init__()

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        return _create_imap4_client(self._host, self._port, self._timeout)


class MailBoxStartTls(BaseMailBox):
    """Working with the email box through IMAP4 with imaplib.IMAP4 + STARTTLS"""

    def __init__(self, host='', port=143, timeout=None, ssl_context=None) -> None:
        """
        :param host: host's name (default: localhost)
        :param port: port number
        :param timeout: timeout in seconds for the connection attempt, since python 3.9
        :param ssl_context: SSLContext object that contains your certificate chain and private key
        """
        check_timeout_arg_support(timeout)
        self._host = host
        self._port = port
        self._timeout = timeout
        self._ssl_context = ssl_context
        super().__init__()

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        if self._port == 993:
            raise ValueError("Port 993 requires IMAP4_SSL. Use MailBox class for SSL/TLS connection.")
        client = _create_imap4_client(self._host, self._port, self._timeout)
        result = client.starttls(self._ssl_context)
        check_command_status(result, MailboxStarttlsError)
        return client
