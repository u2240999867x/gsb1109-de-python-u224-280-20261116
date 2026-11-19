Replaying records after tag rule changes
========================================

When the semantics of CBOR tags change, previously decoded records must be
replayed without losing their history.  The :mod:`cbor2` replay support keeps
the original tag, the original payload bytes, the previously used rule and
the newly applied rule together for every affected field.

Rules
-----

A rule set is an immutable mapping of tag numbers to :class:`~cbor2.TagRule`
objects::

    from cbor2 import RuleSet, TagRule, builtin_rules

    old_rules = builtin_rules("2023.1")
    new_rules = RuleSet(
        "2024.1",
        {
            **old_rules.rules,
            0: TagRule(0, "datetime-string-v2", "raw"),
            100: TagRule(100, "epoch-date-v2", "raw", {0}),
        },
    )
    diff = old_rules.diff(new_rules)
    print(diff.behavioural_tags)

Each rule declares:

``semantic``
    The name of the handler that interprets the tag.  A change of name is a
    behavioural change, because it can produce a different value.

``input``
    ``"raw"`` when the handler receives the original payload bytes, or
    ``"interpreted"`` when it receives the decoded payload.  A raw handler
    cannot silently fall back to a decoded value; if the bytes are missing the
    field stays pending.

``dependencies``
    The nested tags that may influence the handler's result.  ``"all"`` means
    every nested tag.  This is also the only authority an outer tag has:
    nested vendor tags outside the declared scope are never passed to their
    handlers.

Descriptions are metadata only.  Editing a description does not schedule a
field for reinterpretation.

Replaying
---------

Use :class:`~cbor2.Replay` or :func:`~cbor2.replay_records`.  Values may be
annotated with :class:`~cbor2.TaggedValue`, which carries the original
payload bytes::

    from datetime import datetime, timezone

    from cbor2 import Replay, TaggedValue

    def read_datetime(ctx):
        return ctx.raw.decode()

    replay = Replay(old_rules, new_rules, {"datetime-string-v2": read_datetime})
    result = replay.replay(
        {
            "when": TaggedValue(
                0, "2024-01-02T03:04:05Z",
                raw=b"2024-01-02T03:04:05Z",
                previous=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            ),
            "count": 1700000000,
        }
    )

    print(result.new["when"])              # 2024-01-02T03:04:05Z
    print(result["when"].old_rule.semantic) # datetime-string
    print(result["when"].new_rule.semantic) # datetime-string-v2
    print(result["when"].rule_version)      # 2024.1

A plain integer is never treated as a timestamp merely because its value
matches an epoch timestamp; only tagged values are interpreted.

Pending fields
--------------

A field is *pending* when a complete interpretation path is missing: a rule
was removed, a handler is not registered, raw bytes are absent, or a nested
dependency is itself pending.  Pending fields keep the original value
object, including its tag, rather than degrading it to a string::

    for ticket in pending_tickets(result):
        print(ticket.path, ticket.tag, ticket.raw)

Nested tags
-----------

An outer tag only authorises the nested tags listed in its ``dependencies``.
An unknown outer tag therefore leaves an inner vendor tag completely opaque.
Conversely, when a nested tag inside the declared scope changes, every outer
handler that declared that dependency is replayed too, even though the outer
tag number did not change.
