# Changelog

## Unreleased

* Match rules are compared semantically so consumers sharing one connection
  register each actual rule with the daemon once while still receiving only
  their own matching signals.  Quoting, escaping, key order, path namespace
  and argument conditions follow the bus semantics.
* Add and remove confirmations are serialized: a removal is never sent before
  a pending add is confirmed, new demand during a removal keeps the old
  registration valid, and a failed removal is not reported as gone.
* Subscriptions are reference counted per consumer; one consumer leaving does
  not affect the others.
* Connections are tracked by generation so confirmations from an old
  connection cannot modify registrations on a reconnect, where wanted rules
  are re-verified.
* Add `MessageBus.subscribe()`, `MatchRule`, and `MessageBus.get_match_rules()`
  to query the actual registrations and per-consumer status.  Duplicate copies
  of a signal are only dispatched once.

## Version 0.2.3

This version contains some bugfixes and new features.

* Include py.typed in Manifest.in (#79)
* Fix property validation error message (#81)
* Don't log errors if call failed after disconnect (#84)
* Support PEP563 annotations (#3)
* Service: support async properties (#86)
* Client: Support coroutines as signal handlers (#2)

## Version 0.2.2

This version contains some bugfixes and a new feature

* Add `connected` instance variable to the `MessageBus` (#74)
* Better handling of message bus errors on disconnect (de8ed30)
* Ensure futures are not done when settings results and exceptions (#73, 1213667)

## Version 0.2.1

This version adds performance optimizations, bugfixes, and new features.

* aio.MessageBus: Support passing unix fds. (#54)
* Unmarshaller optimizations for a significant performance increase in message reading. (#62, #64)
* Cache instances of `SignatureTree`. (ace5584)
* Fix socket creation on macos. (#63)
* Implement PEP 561 to indicate inline type hints. (#69)
* aio.MessageBus: Return a future from `send()`. (302511b)
* aio.MessageBus: Add `wait_for_disconnect()` to detect connection errors. (ab01ab1)

Notice: `aio.MessageBus.send()` will be changed to a coroutine function in the 1.0 version of this library.

## Version 0.1.4

This version adds some bugfixes and new features.

* Support tcp transport addresses (#57)
* Add support for the annonymous authentication protocol (#32)
* Add flags kwarg to aio high level client method call (#55)
* Allow subclassing of DBusError (#42)
* Fix exception in aio message handler loop on task cancellation (ff165aa)
* Improve error messages (#46, #59)
* Fix match rule memory leak bug (508edf8)
* Don't add match rules for high level client by default (615218f)
* Add empty properties interface to standard interfaces (#49)

## Version 0.1.3

This version adds some bugfixes and new features.

* Add the object manager interface to the service. (#14, #37)
* Allow coroutines in service methods. (#24, #27)
* Client: don't send method replies with `NO_REPLY_EXPECTED` message flag. (#22)
* Fix duplicate nodes in introspection. (#13)

## Version 0.1.2

This version adds some bugfixes.

* Allow exporting interface multiple times (#4)
* Fix super call in exceptions (#5)
* Add timeout support on `introspect` (#7)
* Add unix fd type 'h' to valid tokens (#9)
* Dont use future annotations (#10)
* Fix variant validator (d724fc2)

## Version 0.1.1

This version adds some major features and breaking changes.

* Remove the MessageBus convenience constructors (breaking).
* Complete documentation.
* Type annotation for all public methods.

## Version 0.0.1

This is the first release of python-dbus-next.
