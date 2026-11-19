def test_import_normally():
    from imaplib2 import IMAP4_SSL
    assert IMAP4_SSL


def test_import_hack():
    from imaplib2.imaplib2 import IMAP4_SSL
    assert IMAP4_SSL


def test_collector_is_exported():
    from imaplib2 import IdleCollector, collect_mailbox

    assert IdleCollector
    assert collect_mailbox
