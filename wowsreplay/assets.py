"""content/assets.bin (b'BDWB' prototype database, 15.x) - just enough to assemble ship models.

Pointers are relative offsets; what they are relative to was worked out per field (see _Header).
  strings    hashmap u32 id -> offset into a NUL-terminated string blob (node / render-set names)
  resources  hashmap u64 path id -> u32 (record << 8 | database << 2)
  paths      [u64 id, u64 parent id, {u64 len incl. NUL, u64 relptr}] - a tree of path components
  databases  10 x {u32 magic, u32 checksum, u64 size, u64 relptr}; each {u64 count, u64 relptr} + records
             db0 materials (.mfm), db1 visuals (.visual), db2 skeleton extensions, db3 models

Visual record (112 B, pointers relative to the record):
  u64 n_nodes, ptr name_ids[u32 n], ptr node_index[u16 n] (name table -> node), ptr name_hash[u32 n],
  ptr matrices[16 f32 n] (local, row-vector convention: translation in [12:15]), ptr parents[i16 n],
  u64 geometry path id, u8, u8, u16 n_render_sets, u16 n_lods, u16, f32[4] bbox min, f32[4] bbox max,
  ptr render_sets, ptr lods
Render set (40 B): u32 name id, u32, u32 vertex mapping id, u32 index mapping id, u64 material path id,
  u8, u8 n_nodes, 6 pad, ptr node name ids
LOD (16 B): f32 extent, u8 casts shadow, u8, u16 n_render_sets, u32 ptr render-set name ids (u32 each)
"""
import struct
from typing import Dict, List, NamedTuple, Optional

import numpy as np


class RenderSet(NamedTuple):
    name: int
    vertex_mapping: int
    index_mapping: int
    material: int
    nodes: List[int]


class Lod(NamedTuple):
    extent: float
    render_sets: List[int]


class Visual(NamedTuple):
    nodes: List[str]                 # by node index
    parents: List[int]
    local: List[np.ndarray]          # 4x4, row-vector convention
    geometry: int                    # path id of the .geometry
    render_sets: List[RenderSet]
    lods: List[Lod]

    def world(self, i):
        m = self.local[i]
        p = self.parents[i]
        while p >= 0:
            m = m @ self.local[p]
            p = self.parents[p]
        return m

    def node(self, name) -> Optional[int]:
        try:
            return self.nodes.index(name)
        except ValueError:
            return None


class Assets:
    def __init__(self, data: bytes):
        d = self.d = data
        if d[:4] != b'BDWB':
            raise ValueError('not an assets.bin')
        u32 = lambda o: struct.unpack_from('<I', d, o)[0]
        u64 = lambda o: struct.unpack_from('<Q', d, o)[0]
        # strings section at 0x10 (pointers relative to 0x10)
        self.s_cap, self.s_buckets, self.s_values, self.s_data = u32(0x10), 0x10 + u64(0x18), 0x10 + u64(0x20), 0x10 + u64(0x30)
        # resources section at 0x38 (hashmap pointers relative to 0x38); paths {count, ptr} at 0x50
        self.r_cap, self.r_buckets, self.r_values = u32(0x38), 0x38 + u64(0x40), 0x38 + u64(0x48)
        n_paths, paths = u64(0x50), 0x50 + u64(0x58)
        # databases {count, ptr} at 0x60, array pointer relative to 0x10
        n_db, dbs = u64(0x60), 0x10 + u64(0x68)
        self.db = []
        for k in range(n_db):
            o = dbs + 24 * k
            _m, _c, _size, rp = struct.unpack_from('<IIQQ', d, o)
            base = o + rp
            count, recs = struct.unpack_from('<QQ', d, base)
            self.db.append((base + recs, count))
        self._paths = {}
        for k in range(n_paths):
            o = paths + 32 * k
            sid, pid, ln, rp = struct.unpack_from('<QQQQ', d, o)
            self._paths[sid] = (pid, o + 16 + rp, ln - 1)
        self._by_path = None

    # ---------------------------------------------------------------- lookups

    def string(self, h: int) -> Optional[str]:
        i = h % self.s_cap
        for _ in range(self.s_cap):
            k, f = struct.unpack_from('<II', self.d, self.s_buckets + 8 * i)
            if f == 0:
                return None
            if k == h:
                off = struct.unpack_from('<I', self.d, self.s_values + 4 * i)[0]
                start = self.s_data + off
                return self.d[start:self.d.index(b'\0', start)].decode('utf-8', 'replace')
            i = (i + 1) % self.s_cap
        return None

    def path(self, pid: int) -> str:
        parts = []
        while pid in self._paths:
            parent, o, n = self._paths[pid]
            parts.append(self.d[o:o + n].decode('utf-8', 'replace'))
            pid = parent
        return '/'.join(reversed(parts))

    def path_id(self, path: str) -> Optional[int]:
        if self._by_path is None:
            self._by_path = {self.path(p): p for p in self._paths}
        return self._by_path.get(path)

    def resource(self, pid: int):
        """(database, record) for a path id, or None."""
        i = pid % self.r_cap
        for _ in range(self.r_cap):
            k, f = struct.unpack_from('<QQ', self.d, self.r_buckets + 16 * i)
            if k == 0 and f == 0:
                return None
            if k == pid:
                v = struct.unpack_from('<I', self.d, self.r_values + 4 * i)[0]
                return (v & 0xFF) >> 2, v >> 8
            i = (i + 1) % self.r_cap
        return None

    # ---------------------------------------------------------------- visuals

    def visual(self, path: str) -> Visual:
        pid = self.path_id(path)
        res = pid is not None and self.resource(pid)
        if not res or res[0] != 1:
            raise KeyError(f'no visual for {path}')
        base, count = self.db[1]
        r = base + 0x70 * res[1]
        d = self.d
        n, pn, pidx, _ph, pm, pp = struct.unpack_from('<6Q', d, r)
        geo, = struct.unpack_from('<Q', d, r + 0x30)
        n_rs, n_lod = struct.unpack_from('<HH', d, r + 0x3A)
        prs, plod = struct.unpack_from('<QQ', d, r + 0x60)
        name_ids = struct.unpack_from(f'<{n}I', d, r + pn)
        node_of = struct.unpack_from(f'<{n}H', d, r + pidx)
        nodes = [''] * n
        for j in range(n):
            nodes[node_of[j]] = self.string(name_ids[j]) or f'#{name_ids[j]:08x}'
        local = [np.array(struct.unpack_from('<16f', d, r + pm + 64 * i), np.float64).reshape(4, 4) for i in range(n)]
        parents = list(struct.unpack_from(f'<{n}h', d, r + pp))
        rsets = []
        for k in range(n_rs):
            o = r + prs + 40 * k
            name, _x, vm, im, mat = struct.unpack_from('<IIIIQ', d, o)
            nn = d[o + 25]
            np_, = struct.unpack_from('<Q', d, o + 32)
            rsets.append(RenderSet(name, vm, im, mat, list(struct.unpack_from(f'<{nn}I', d, o + np_))))
        lods = []
        for k in range(n_lod):
            o = r + plod + 16 * k
            ext, _sh, _b, cnt, lp = struct.unpack_from('<fBBHI', d, o)
            lods.append(Lod(ext, list(struct.unpack_from(f'<{cnt}I', d, o + lp))))
        return Visual(nodes, parents, local, geo, rsets, lods)
