"""Replay file container.

Layout (all integers little-endian):

    u32 magic = 12 32 34 11
    u32 block_count
    block_count x (u32 length, JSON bytes)    # block 0 = battle header
    rest of file = Blowfish-encrypted, zlib-compressed packet stream

The stream is decrypted with Blowfish-ECB, skipping the first 8-byte block;
each decrypted block is then XORed with the previous *plaintext* block.
"""
import json
import struct
import zlib
from dataclasses import dataclass

try:
    from Cryptodome.Cipher import Blowfish       # pycryptodomex
except ImportError:                             # pycryptodome - what Pyodide ships (in-browser parsing)
    from Crypto.Cipher import Blowfish

MAGIC = b'\x12\x32\x34\x11'
WOWS_KEY = bytes.fromhex('29B7C909383F8488FA98EC4E131979FB')


@dataclass
class Replay:
    header: dict          # block 0: map, players, client version, ...
    extra_blocks: list    # remaining JSON blocks (usually empty in modern replays)
    stream: bytes         # decrypted + decompressed packet stream

    @property
    def version(self) -> tuple:
        """Client version as ints, e.g. (15, 9, 0, 13357625)."""
        raw = self.header.get('clientVersionFromXml', '')
        return tuple(int(p) for p in raw.replace(' ', '').split(',') if p)


def _decrypt(data: bytes) -> bytes:
    cipher = Blowfish.new(WOWS_KEY, Blowfish.MODE_ECB)
    out = bytearray()
    prev = 0
    end = len(data) - len(data) % 8
    for i in range(8, end, 8):
        block = int.from_bytes(cipher.decrypt(data[i:i + 8]), 'little') ^ prev
        prev = block
        out += block.to_bytes(8, 'little')
    return bytes(out)


def read_header(path) -> dict:
    """Block 0 only (map, players, client version) - no decryption."""
    with open(path, 'rb') as f:
        if f.read(4) != MAGIC:
            raise ValueError(f'{path}: not a replay file (bad magic)')
        f.read(4)
        size, = struct.unpack('<I', f.read(4))
        return json.loads(f.read(size))


MAX_STREAM = 256 << 20   # decompressed packet stream cap; real replays are ~3x their file size (~10 MB)


def _inflate(data: bytes) -> bytes:
    """zlib.decompress with a size cap, so a crafted file can't expand into gigabytes."""
    d = zlib.decompressobj()
    out = d.decompress(data, MAX_STREAM)
    if d.unconsumed_tail:
        raise ValueError(f'packet stream larger than {MAX_STREAM >> 20} MB - not a normal replay')
    return out + d.flush()


def read_replay(path) -> Replay:
    with open(path, 'rb') as f:
        if f.read(4) != MAGIC:
            raise ValueError(f'{path}: not a replay file (bad magic)')
        count, = struct.unpack('<I', f.read(4))
        if not 1 <= count <= 16:
            raise ValueError(f'{path}: not a replay file ({count} header blocks)')
        blocks = []
        for _ in range(count):
            size, = struct.unpack('<I', f.read(4))
            raw = f.read(size)
            blocks.append(json.loads(raw) if raw else None)
        payload = f.read()
    return Replay(header=blocks[0], extra_blocks=blocks[1:], stream=_inflate(_decrypt(payload)))
