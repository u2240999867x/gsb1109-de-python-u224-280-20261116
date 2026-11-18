"""Reusable fixtures for tests driven by :mod:`pycomm3.testing.cip_peer`.

The peer itself ships with the library; this module keeps the tag catalog and
small factory helpers that make a scenario read like the sequence of requests
the driver will actually send.
"""

import json
import os
from typing import Any, Dict

from pycomm3 import Array, DataTypes
from pycomm3.testing.cip_peer import (
    CIPPeer,
    ForwardOpen,
    Generic,
    ListIdentity,
    RegisterSession,
    Reply,
    Scenario,
    Unconnected,
    string,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "offline")
TAGS_FILE = os.path.join(FIXTURE_DIR, "controller_tags.json")

with open(TAGS_FILE) as _tags_file:
    RAW_TAGS: Dict[str, Dict[str, Any]] = json.load(_tags_file)


def tag_info(name: str) -> Dict[str, Any]:
    """Return a tag definition with the ``type_class`` the driver expects."""

    raw = dict(RAW_TAGS[name])
    data_type = raw["data_type_name"]
    type_class = DataTypes.get(data_type)
    if raw["dim"]:
        type_class = Array(raw["dimensions"][0], type_class)
    raw["type_class"] = type_class
    return raw


def tag_infos(*names: str) -> Dict[str, Dict[str, Any]]:
    return {name: tag_info(name) for name in names}


def open_sequence(connection_id: int = 0x0000_0022, revision=(20, 11)):
    """The frames ``LogixDriver(init_tags=False).open()`` sends."""

    return [
        RegisterSession(),
        ListIdentity(
            reply=Reply.list_identity(
                revision=revision, product_name="1756-L61", product_code=89
            )
        ),
        Unconnected(
            0x01,
            class_id=0x01,
            instance=1,
            reply=Reply.identity(revision=revision, product_name="1756-L61"),
        ),
        ForwardOpen(reply=Reply.forward_open(connection_id=connection_id)),
        # get_plc_name is sent on the new connection; a STRING reply body
        Generic(
            0x01,
            class_id=0x64,
            instance=1,
            reply=Reply.success(string("PROGRAM").encode()),
        ),
    ]


def make_peer(*steps, session_id: int = 0x1234_5678) -> CIPPeer:
    return CIPPeer(Scenario(steps), session_id=session_id)


__all__ = [
    "RAW_TAGS",
    "tag_info",
    "tag_infos",
    "open_sequence",
    "make_peer",
    "string",
]
