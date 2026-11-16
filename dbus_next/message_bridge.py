"""Compatibility module for the message bridge.

The implementation lives in :mod:`dbus_next.bridge`.
"""

from .bridge import (Bridge, BridgeError, InvalidBridgeBodyError, MessageBridge, MessageRouter,
                     MessageTranslator, MissingMappingError, MissingUnixFdError,
                     RecordedMessageBridge, bridge_message, bridge_messages,
                     bridge_recorded_message, bridge_recorded_messages,
                     recorded_message_to_message, remap_message, translate_message)

__all__ = [
    'Bridge',
    'BridgeError',
    'InvalidBridgeBodyError',
    'MessageBridge',
    'MessageRouter',
    'MessageTranslator',
    'MissingMappingError',
    'MissingUnixFdError',
    'RecordedMessageBridge',
    'bridge_message',
    'bridge_messages',
    'bridge_recorded_message',
    'bridge_recorded_messages',
    'recorded_message_to_message',
    'remap_message',
    'translate_message',
]