# imaplib2: a threaded Python IMAP4 client

[![Jazzband](https://jazzband.co/static/img/badge.svg)](https://jazzband.co/)
[![codecov](https://codecov.io/gh/jazzband/imaplib2/branch/master/graph/badge.svg?token=DZZ3P6438E)](https://codecov.io/gh/jazzband/imaplib2)

Based on RFC 3501 and original imaplib module.

This is a version of imaplib that uses threads to allow full use of the
IMAP4 concurrency features, and to de-couple a user of imaplib from i/o
lags, except where explicitly allowed.

Documented in imaplib2.html

## Mailbox names for Unicode-only catalogues

`imaplib2.mailbox` keeps protocol mailbox names intact while exporting text
to a Unicode-only directory, and restores enough state to address the
original mailboxes again.

```python
from imaplib2 import IMAP4

M = IMAP4(host)
M.login(user, password)
catalog = M.mailbox_catalog()               # NAMESPACE + LIST, per connection

for entry in catalog:
    print(entry.directory_id, entry.display_name, entry.decode_state,
          entry.namespace_kind, entry.delimiter, entry.segments)

# Address by directory id even when the name cannot round-trip.
M.select_mailbox(catalog.entries()[0].directory_id, catalog=catalog)
```

Encoding follows the connection: modified UTF-7 by default, UTF-8 after
`ENABLE UTF8=ACCEPT`. The server's declared delimiter drives hierarchy
segmentation; a literal `/` in a `.`-delimited namespace is not a level
boundary. Different protocol names that decode or normalize to the same text
get distinct `directory_id` values and a shared conflict group, and lookup by
that text raises instead of picking one. Names that cannot round-trip remain
archivable by id but are never addressable by text.

`catalog.export_dicts()` yields JSON-safe records;
`imaplib2.restore_catalog(records)` rebuilds and verifies them.
