"""
Two-stage filtering example: server-side candidate query + local verification.

The filter mixes conditions the IMAP server can evaluate (sender, subject,
flags, dates) with conditions that are decidable only after downloading the
body or attachments (attachment name, attachment payload content).

Stage 1 builds a *sound* server query: it never excludes a possible match.
Conditions the server cannot evaluate are relaxed away, and when nothing sound
remains the query becomes ALL, so every message stays a candidate.

Stage 2 verifies the candidates locally. Materials are fetched cheapest-first,
already downloaded materials are reused, and any fetch failure results in an
"inconclusive" verdict rather than a false exclusion.
"""
import datetime as dt

from imap_tools import (
    And,
    LocalVerifier,
    Material,
    MaterialStore,
    Not,
    Or,
    ServerCapabilities,
    ImapMaterialFetcher,
    attachment_content,
    attachment_name,
    build_candidate_query,
    date_gte,
    flag,
    from_,
    split_candidates,
    subject,
)

# Filter tree:
#   sender is the boss
#   AND (subject mentions invoice OR has a PDF attachment)
#   AND not marked Seen
#   AND sent in 2026 or later
#   AND no attachment contains the word "confidential"
filter_tree = And(
    from_('boss@corp.example'),
    Or(subject('invoice'), attachment_name('.pdf')),
    Not(flag('\\Seen')),
    date_gte(dt.date(2026, 1, 1)),
    Not(attachment_content('confidential')),
)

# What this particular server announces it can search. Use ServerCapabilities
# built from the server's CAPABILITY response / known behaviour in real code.
capabilities = ServerCapabilities.imap4rev1()

# Stage 1: conservative server-side candidate query.
# The attachment conditions have no IMAP search key, so they are relaxed away;
# the OR branch containing one becomes unrenderable and is dropped as a whole
# to stay sound. 'ALL' is returned when nothing sound can be expressed.
candidate_query = build_candidate_query(filter_tree, capabilities)
# '(FROM "boss@corp.example" NOT (KEYWORD \\Seen) SINCE 1-Jan-2026)'
print('server candidate query:', candidate_query)

# A server without NOT/OR keeps only the conjuncts it can evaluate soundly.
weak_caps = ServerCapabilities.minimal()
print('weak server candidate query:', build_candidate_query(filter_tree, weak_caps))
# '(FROM "boss@corp.example")'


def run(mailbox, cached_materials=None):
    # Candidate UIDs come from the server; fetch()/uids() criteria stay valid IMAP.
    uids = mailbox.uids(candidate_query)

    # Stage 2: local verification with a caching material store.
    store = MaterialStore(ImapMaterialFetcher(mailbox), cache=cached_materials)
    verifier = LocalVerifier(store)
    buckets = split_candidates(verifier.verify_all(uids, filter_tree))

    for result in buckets['matched']:
        print('MATCHED', result.uid, 'cost', result.spent_cost)
        for decision in result.decisive_decisions:
            print('  ', decision.condition, '->', decision.evidence)

    for result in buckets['excluded']:
        print('EXCLUDED', result.uid, 'by',
              [d.condition for d in result.decisive_decisions if d.excluded])

    # Inconclusive messages must not be treated as excluded: fetch these again
    # later or flag them for manual review.
    for result in buckets['inconclusive']:
        print('INCONCLUSIVE', result.uid, 'missing',
              [m.name for m in result.missing_materials], 'errors', result.fetch_errors)

    return buckets


# Cost control: Material carries relative default costs; a Condition can
# override them, and conditions sharing a material (BODY and attachment
# payloads both need the full RFC822 message) trigger a single download.
print('material costs:', {m.name: m.default_cost for m in Material})
