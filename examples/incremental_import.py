"""Incremental import: keep state per folder/UIDVALIDITY and stop on a failed message."""
from imap_tools import CoverageStatus, MailBox, MailboxImportState

STATE_PATH = '/tmp/imap_tools_incremental_state.json'
ARCHIVE_PATH = '/tmp/imap_tools_archive'

state = MailboxImportState.load_or_create(STATE_PATH)
with MailBox('imap.mail.com').login('test@mail.com', 'pwd') as mailbox:
    for folder in ('INBOX', 'Archive'):
        result = mailbox.incremental_import(
            state=state, folder=folder, archive=ARCHIVE_PATH, mark_seen=False)
        print(f'{folder}: UIDVALIDITY={result.uidvalidity} status={result.status.value} '
              f'fetched={result.fetched} cursor={result.cursor}')
        if result.status == CoverageStatus.INCOMPLETE:
            print(f'  stopped at UID {result.failed_uid}: {result.error}')
        elif result.status == CoverageStatus.UNKNOWN:
            print(f'  coverage unknown, retry later: {result.error}')
