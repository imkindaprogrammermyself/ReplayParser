"""WoWS .geometry files (15.x): merged vertex/index buffers, meshoptimizer-compressed.

Layout (little endian, offsets inside a struct are relative to that struct's start):
  header: u32 n_vertex_buffers, n_index_buffers, n_vertex_mappings, n_index_mappings,
          n_collision_models, n_armor_models;
          u64 ptr vertex_mappings, index_mappings, vertex_buffers, index_buffers, collision, armor
  mapping (16 B): u32 id, u16 buffer index, u16 texel density (f16), u32 first item, u32 item count
  vertex buffer (32 B): u64 data ptr, {u64 format-name length, u64 format-name ptr (relative to
                        this inner struct)}, u32 data size, u16 stride, u8, u8
  index buffer (16 B):  u64 data ptr, u32 data size, u16 ?, u16 index size
  buffer data: b'ENCD', u32 item count, then a meshoptimizer vertex (0xA0) / index (0xE0) stream

Mapping ids are hashes; which mapping is which mesh/LOD comes from content/assets.bin.
"""
import struct
from typing import List, NamedTuple

import numpy as np


# ------------------------------------------------------------------ meshoptimizer codecs (format v0)

def _decode_bytes(data, pos, n):
    """n (multiple of 16) bytes of one vertex channel; returns (array, new pos)."""
    header_size = (n // 16 + 3) // 4
    header = data[pos:pos + header_size]
    pos += header_size
    out = bytearray(n)
    for g in range(n // 16):
        bits = (header[g // 4] >> ((g % 4) * 2)) & 3
        o = g * 16
        if bits == 0:
            continue
        if bits == 3:
            out[o:o + 16] = data[pos:pos + 16]
            pos += 16
            continue
        width = 1 << bits                    # 2 or 4 bits per value
        per = 8 // width
        packed = data[pos:pos + 16 // per]
        extra = pos + 16 // per
        sentinel = (1 << width) - 1
        k = 0
        for byte in packed:
            for s in range(8 - width, -1, -width):
                v = (byte >> s) & sentinel
                if v == sentinel:
                    v = data[extra]
                    extra += 1
                out[o + k] = v
                k += 1
        pos = extra
    return out, pos


def decode_vertex_buffer(buf: bytes, count: int, stride: int) -> np.ndarray:
    if buf[0] & 0xF0 != 0xA0 or buf[0] & 0x0F != 0:
        raise ValueError(f'unsupported vertex codec header {buf[0]:#x}')
    end = len(buf)
    last = np.frombuffer(buf, np.uint8, stride, end - stride).copy()
    block = min(256, (8192 // stride) & ~15)
    out = np.empty((count, stride), np.uint8)
    pos = 1
    for start in range(0, count, block):
        n = min(block, count - start)
        aligned = (n + 15) & ~15
        for k in range(stride):
            raw, pos = _decode_bytes(buf, pos, aligned)
            d = np.frombuffer(bytes(raw[:n]), np.uint8).astype(np.int32)
            delta = (d >> 1) ^ -(d & 1)                                  # unzigzag
            out[start:start + n, k] = (np.cumsum(delta) + last[k]) & 0xFF
        last = out[start + n - 1].copy()
    if end - pos != max(stride, 32):
        raise ValueError(f'vertex stream: {end - pos} bytes left, expected tail of {max(stride, 32)}')
    return out


def _vbyte(data, pos):
    v = shift = 0
    while True:
        b = data[pos]
        pos += 1
        v |= (b & 0x7F) << shift
        if b < 0x80:
            return v, pos
        shift += 7


def decode_index_buffer(buf: bytes, count: int) -> np.ndarray:
    if buf[0] & 0xF0 != 0xE0 or buf[0] & 0x0F > 1:
        raise ValueError(f'unsupported index codec header {buf[0]:#x}')
    version = buf[0] & 0x0F
    fecmax = 13 if version >= 1 else 15
    edge = [[0xFFFFFFFF, 0xFFFFFFFF] for _ in range(16)]
    vert = [0xFFFFFFFF] * 16
    eo = vo = 0
    nxt = last = 0
    code = 1
    data = 1 + count // 3
    safe_end = len(buf) - 16
    aux = buf[safe_end:]
    out = np.empty(count, np.uint32)

    def idx(data, last):
        v, data = _vbyte(buf, data)
        return (last + ((v >> 1) ^ -(v & 1))) & 0xFFFFFFFF, data

    for i in range(0, count, 3):
        t = buf[code]
        code += 1
        if t < 0xF0:
            a, b = edge[(eo - 1 - (t >> 4)) & 15]
            fec = t & 15
            if fec < fecmax:
                c = nxt if fec == 0 else vert[(vo - 1 - fec) & 15]
                push_c = fec == 0
                nxt += push_c
            else:
                if fec != 15:
                    c = (last + (fec - (fec ^ 3))) & 0xFFFFFFFF
                else:
                    c, data = idx(data, last)
                last = c
                push_c = True
            out[i:i + 3] = (a, b, c)
            vert[vo] = c
            vo = (vo + push_c) & 15
            edge[eo] = [c, b]; eo = (eo + 1) & 15
            edge[eo] = [a, c]; eo = (eo + 1) & 15
        else:
            if t < 0xFE:
                ca = aux[t & 15]
                feb, fec = ca >> 4, ca & 15
                a = nxt; nxt += 1
                b = nxt if feb == 0 else vert[(vo - feb) & 15]
                nxt += feb == 0
                c = nxt if fec == 0 else vert[(vo - fec) & 15]
                nxt += fec == 0
                pb, pc = feb == 0, fec == 0
            else:
                ca = buf[data]
                data += 1
                fea = 0 if t == 0xFE else 15
                feb, fec = ca >> 4, ca & 15
                if ca == 0:
                    nxt = 0
                if fea == 0:
                    a = nxt; nxt += 1
                else:
                    a = 0
                if feb == 0:
                    b = nxt; nxt += 1
                else:
                    b = vert[(vo - feb) & 15]
                if fec == 0:
                    c = nxt; nxt += 1
                else:
                    c = vert[(vo - fec) & 15]
                if fea == 15:
                    a, data = idx(data, last); last = a
                if feb == 15:
                    b, data = idx(data, last); last = b
                if fec == 15:
                    c, data = idx(data, last); last = c
                pb, pc = feb in (0, 15), fec in (0, 15)
            out[i:i + 3] = (a, b, c)
            vert[vo] = a; vo = (vo + 1) & 15
            vert[vo] = b; vo = (vo + pb) & 15
            vert[vo] = c; vo = (vo + pc) & 15
            edge[eo] = [b, a]; eo = (eo + 1) & 15
            edge[eo] = [c, b]; eo = (eo + 1) & 15
            edge[eo] = [a, c]; eo = (eo + 1) & 15
    if data != safe_end:
        raise ValueError(f'index stream: ended at {data}, expected {safe_end}')
    return out


# ------------------------------------------------------------------ file structure

class Mapping(NamedTuple):
    id: int
    buffer: int
    first: int
    count: int


class VertexBuffer(NamedTuple):
    format: str
    stride: int
    data: np.ndarray          # (count, stride) uint8


class Geometry(NamedTuple):
    vertex_mappings: List[Mapping]
    index_mappings: List[Mapping]
    vertex_buffers: List[VertexBuffer]
    index_buffers: List[np.ndarray]
    armor_ptr: int
    n_armor: int


def _encd(d, pos):
    if d[pos:pos + 4] != b'ENCD':
        raise ValueError(f'no ENCD block at {pos:#x}')
    return struct.unpack_from('<I', d, pos + 4)[0], pos + 8


def read_geometry(d: bytes, decode=True) -> Geometry:
    nvb, nib, nvm, nim, ncol, narm = struct.unpack_from('<6I', d, 0)
    pvm, pim, pvb, pib, pcol, parm = struct.unpack_from('<6Q', d, 24)

    def mappings(p, n):
        return [Mapping(*(lambda m: (m[0], m[1], m[3], m[4]))(struct.unpack_from('<IHHII', d, p + 16 * i)))
                for i in range(n)]

    vbs = []
    for i in range(nvb):
        s = pvb + 32 * i
        dp, nlen, np_, size, stride = struct.unpack_from('<QQQIH', d, s)
        fmt = d[s + 8 + np_:s + 8 + np_ + nlen].split(b'\0')[0].decode('ascii')   # {len, ptr} struct at +8
        count, start = _encd(d, s + dp)
        raw = d[start:s + dp + size]
        vbs.append(VertexBuffer(fmt, stride, decode_vertex_buffer(raw, count, stride) if decode else None))
    ibs = []
    for i in range(nib):
        s = pib + 16 * i
        dp, size, _, isz = struct.unpack_from('<QIHH', d, s)
        count, start = _encd(d, s + dp)
        ibs.append(decode_index_buffer(d[start:s + dp + size], count) if decode else count)
    return Geometry(mappings(pvm, nvm), mappings(pim, nim), vbs, ibs, parm, narm)
