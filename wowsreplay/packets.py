"""Packet framing and the packet IDs used by WoWS >= 12.6.

Each packet: u32 payload_size, u32 type, f32 time (seconds since replay start), payload.
Layouts of the unmapped packets were worked out from a 15.9 replay; treat the
ones marked "likely" as best guesses.
"""
import struct
from typing import Iterator, NamedTuple

BASE_PLAYER_CREATE = 0x00
CELL_PLAYER_CREATE = 0x01
ENTITY_CONTROL = 0x02
ENTITY_ENTER = 0x03
ENTITY_LEAVE = 0x04
ENTITY_CREATE = 0x05
ENTITY_PROPERTY = 0x07
ENTITY_METHOD = 0x08
POSITION = 0x0A
SERVER_TICK = 0x0E           # f64 tick length (1/7 s)
VERSION = 0x16
BATTLE_STATS = 0x22          # u32 len + post-battle JSON
NESTED_PROPERTY = 0x23
CAMERA = 0x25
VIEW_MODE = 0x27             # likely: i32, 8 = normal, 5 = tactical map
MAP = 0x28
AVATAR_POSITION = 0x2A       # same layout as 0x2C: i32 avatarId, i32 0, vec3 pos, yaw/pitch/roll = 0.
                             # Avatar follows the camera; re-sent when it moved ~25 u. Duplicates CAMERA.
PLAYER_POSITION = 0x2C       # own Avatar / own Vehicle position (0x2B before 15.x)
TARGET_LOCK = 0x30           # likely: i32 0, i32 3, i32 targetVehicleId
STEERING = 0x32              # likely: i32 kind (0 throttle, 1 locked rudder), i32 level
END_OF_STREAM = 0xFFFFFFFF


class Packet(NamedTuple):
    time: float
    type: int
    body: bytes


def iter_packets(stream: bytes) -> Iterator[Packet]:
    pos, end = 0, len(stream)
    while pos < end:
        size, ptype, time = struct.unpack_from('<IIf', stream, pos)
        pos += 12
        yield Packet(time, ptype, stream[pos:pos + size])
        pos += size
