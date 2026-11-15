"""Assemble ship 3D models for the viewer from files unpacked by unpack_models.bat.

    python tools/import_models.py wows_gamedata.zip wows_models.zip [SHIP_KEY ...] [--tris 40000]

For each ship (default: every ship whose hull model is in wows_models.zip) writes
wowsreplay/data/models/<SHIP_KEY>.json:
  meshes: {visual path: {pos: base64 f32 xyz, idx: base64 u32}}  - viewer space (x starboard, y up, -z bow)
  parts:  [{mesh, m: 4x4 column-major (three.js), kind, hp}]        - hull sections + every mounted model

Pieces: GameParams gives the hull .model and, per component (artillery, ATBA, AA, directors, finders,
catapults, torpedoes), HP_* mount names with their .model. assets.bin gives each .visual's nodes
(HP_* mount points with matrices), render sets and LODs; .geometry holds the meshes.
One LOD index is used for the whole ship: the most detailed one that fits in --tris triangles.
"""
import argparse
import base64
import json
import os
import re
import sys
import zipfile
import zlib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from wowsreplay.assets import Assets  # noqa: E402
from wowsreplay.geometry import read_geometry  # noqa: E402
from wowsreplay.safe_pickle import Opaque, loads  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), '..', 'wowsreplay', 'data', 'models')
KINDS = {'Artillery': 'main', 'ATBA': 'secondary', 'AirDefense': 'aa', 'Directors': 'director',
         'Finders': 'finder', 'AirArmament': 'catapult', 'Torpedoes': 'torpedo'}
FLIP = np.diag([1.0, 1.0, -1.0, 1.0])     # game model space is left-handed with the bow at +z


def S(o):
    return o.state if isinstance(o, Opaque) else o


def zip_index(z):
    """'content/...' path -> name in the zip (tolerates a top-level folder around content/)."""
    out = {}
    for n in z.namelist():
        p = n.replace('\\', '/')
        i = p.find('content/')
        out[p[i:] if i > 0 and p[i - 1] == '/' else p] = n
    return out


class Builder:
    def __init__(self, models_zip, assets):
        self.z = models_zip
        self.names = zip_index(models_zip)
        self.A = assets
        self.visuals = {}
        self.geoms = {}
        self.mixed_bones = 0      # skinned render sets whose bones disagree (approximated)

    def visual(self, path):
        if path not in self.visuals:
            try:
                self.visuals[path] = self.A.visual(path)
            except KeyError:
                self.visuals[path] = None
        return self.visuals[path]

    def geometry(self, gid):
        path = self.A.path(gid)
        if path not in self.geoms:
            n = self.names.get(path)
            self.geoms[path] = read_geometry(self.z.read(n)) if n else None
        return self.geoms[path]

    def mesh(self, vpath, lod):
        """One LOD of a visual, in viewer space: (pos, idx, barrel_idx | None, pivot | None).

        Skinned gun mounts are split: triangles whose vertices mostly follow a Rotate_X* / Roll_Back*
        bone are the barrels (barrel_idx); they elevate about the Rotate_X node (pivot)."""
        v = self.visual(vpath)
        if v is None or not v.lods:
            return None
        g = self.geometry(v.geometry)
        if g is None:
            return None
        lod = v.lods[min(lod, len(v.lods) - 1)]
        rs_by_name = {r.name: r for r in v.render_sets}
        vmap = {m.id: m for m in g.vertex_mappings}
        imap = {m.id: m for m in g.index_mappings}
        pos, idx, bidx, base = [], [], [], 0
        for name in lod.render_sets:
            rs = rs_by_name.get(name)
            if rs is None or rs.vertex_mapping not in vmap or rs.index_mapping not in imap:
                continue
            vm, im = vmap[rs.vertex_mapping], imap[rs.index_mapping]
            vb = g.vertex_buffers[vm.buffer]
            raw = vb.data[vm.first:vm.first + vm.count]
            xyz = np.ascontiguousarray(raw[:, :12]).view('<f4').reshape(-1, 3)
            barrel = None
            if 'iii' in vb.format and rs.nodes:
                # skinned: vertices sit in the bind frame of their *_BlendBone bones (a turret's can be
                # turned 180 degrees). All bones of a render set share that frame in the models seen so
                # far, so the first one's world matrix is applied to the whole set.
                names = [self.A.string(n) or '' for n in rs.nodes]
                bones = [v.node(n) for n in names]
                mats = [v.world(i) for i in bones if i is not None]
                if mats:
                    if any(not np.allclose(m[:3], mats[0][:3], atol=1e-3) for m in mats[1:]):
                        self.mixed_bones += 1
                    xyz = (np.c_[xyz, np.ones(len(xyz))] @ mats[0])[:, :3]
                if vb.format.startswith('set3/xyznuviiiww'):
                    # bytes 20-22: bone indices * 3; weights: byte 26 -> first, 25 -> second, rest -> third
                    bi = raw[:, 20:23].astype(int) // 3
                    w0, w1 = raw[:, 26].astype(int), raw[:, 25].astype(int)
                    w = np.stack([w0, w1, 255 - w0 - w1], 1)
                    dom = bi[np.arange(len(bi)), w.argmax(1)]
                    is_barrel = np.array([n.startswith(('Rotate_X', 'Roll_Back')) for n in names] + [False])
                    barrel = is_barrel[np.minimum(dom, len(names))]
            tri = g.index_buffers[im.buffer][im.first:im.first + im.count].astype(np.int64).reshape(-1, 3)
            tri = tri[:, [0, 2, 1]]                  # mirrored: reverse winding
            pos.append(xyz * [1, 1, -1])
            if barrel is not None and barrel.any():
                tb = barrel[tri].sum(1) >= 2
                bidx.append(tri[tb] + base)
                tri = tri[~tb]
            idx.append(tri + base)
            base += len(xyz)
        if not pos:
            return None
        pivot = None
        if bidx:
            i = v.node('Rotate_X')
            if i is None:
                idx, bidx = idx + bidx, []
            else:
                w = v.world(i)
                pivot = [round(float(w[3, 0]), 4), round(float(w[3, 1]), 4), round(float(-w[3, 2]), 4)]
        cat = lambda a: np.concatenate(a).astype('<u4').ravel() if a else np.zeros(0, '<u4')
        return np.concatenate(pos).astype('<f4'), cat(idx), (cat(bidx) if bidx else None), pivot

    def tris(self, vpath, lod):
        m = self.mesh(vpath, lod)
        return 0 if m is None else (len(m[1]) + (0 if m[2] is None else len(m[2]))) // 3


def pack_mesh(pos, idx, bidx=None, pivot=None):
    """pos -> int16 on the mesh's bounding box (x = lo + q * step), indices -> u16 when they fit."""
    lo, hi = pos.min(0), pos.max(0)
    step = np.maximum(hi - lo, 1e-6) / 65535
    q = np.round((pos - lo) / step - 32768).astype('<i2')
    wide = len(pos) > 0xFFFF
    enc = lambda a: base64.b64encode(a.astype('<u4' if wide else '<u2').tobytes()).decode()
    out = {'pos': base64.b64encode(q.tobytes()).decode(),
           'q': [round(float(v), 7) for v in (*lo, *step)],
           'idx': enc(idx), 'idx32': bool(wide)}
    if bidx is not None and len(bidx):
        out['barrels'] = enc(bidx)       # same vertices; elevate about pivot (local x axis)
        out['pivot'] = pivot
    return out


def to_three(m_row):
    """Row-vector game matrix -> column-major three.js matrix in viewer space."""
    return (FLIP @ m_row.T @ FLIP).T.ravel().round(5).tolist()    # .T.ravel() = column-major


def ship_layout(gp_ship, b: Builder):
    """[(visual path, row-vector world matrix, kind, hp)] for the hull sections and all mounts."""
    comps = {}
    for key in sorted(gp_ship):
        m = re.fullmatch(r'([A-Z0-9]+)_([A-Za-z]+)', key)
        if m and isinstance(S(gp_ship[key]), dict):
            comps[m.group(2)] = S(gp_ship[key])         # last in name order = top configuration
    hull = comps.get('Hull') or {}
    model = hull.get('model')
    if not model:
        return None, 'no hull model'
    main_vis = model[:-len('.model')] + '.visual'
    mv = b.visual(main_vis)
    if mv is None:
        return None, f'missing {main_vis}'

    parts, frames = [], []        # frames: (visual, world matrix) searched for HP nodes
    stem = main_vis[:-len('.visual')]
    for i, n in enumerate(mv.nodes):
        if n.startswith('HP_'):
            sec = f'{stem}_{n[3:]}.visual'
            if b.visual(sec) is not None:
                w = mv.world(i)
                parts.append((sec, w, 'hull', n))
                frames.append((sec, w))
    if not parts:                                   # single-piece hull
        parts.append((main_vis, np.eye(4), 'hull', None))
    frames.append((main_vis, np.eye(4)))

    def find(hp, where):
        for vis, w in where:
            v = b.visual(vis)
            i = v.node(hp) if v else None
            if i is not None:
                return v.world(i) @ w
        return None

    mounted = {}
    missing = []
    mounts = []
    sectors = {}    # hp -> traverse limits (degrees, relative to the mount's forward)
    for ctype, kind in KINDS.items():
        for hp, item in (comps.get(ctype) or {}).items():
            item = S(item)
            if hp.startswith('HP_') and isinstance(item, dict) and item.get('model'):
                mounts.append((hp, item['model'], kind))
                sector = item.get('horizSector')
                if sector and len(sector) == 2:
                    sectors[hp] = {'sector': [float(sector[0]), float(sector[1])],
                                   'dead': [list(map(float, z)) for z in (item.get('deadZone') or ())]}
    # direct mounts first, then the ones on top of another mount (HP_GGM_3_HP_GGA_1)
    for hp, mdl, kind in sorted(mounts, key=lambda x: x[0].count('HP_')):
        vis = mdl[:-len('.model')] + '.visual'
        nested = re.fullmatch(r'(HP_.+?)_(HP_.+)', hp)
        if nested and nested.group(1) in mounted:
            parent_vis, parent_w = mounted[nested.group(1)]
            w = find(nested.group(2), [(parent_vis, parent_w)])
        else:
            w = find(hp, frames)
        if w is None or b.visual(vis) is None:
            missing.append(hp)
            continue
        mounted[hp] = (vis, w)
        parts.append((vis, w, kind, hp))
    ship_layout.sectors = sectors
    ship_layout.fires = fire_points(hull, frames, b)
    return parts, missing


def fire_points(hull, frames, b: Builder):
    """Viewer-space [x, y, z] of the hull's 4 burn nodes (None where missing).

    GameParams hull effects 'fire1'..'fire4' list the effect points each burn node lights (first = the main
    flame); burningFlags bit i is fire i + 1 (checked: the shell that starts bit 0..3 lands bow -> stern).
    The points live in <section>_ep.skel_ext, relative to the section placed by the main visual's HP_<section>."""
    eps = {}
    for vis, w in frames:
        try:
            ext = b.A.skel_ext(vis[:-len('.visual')] + '_ep.skel_ext')
        except KeyError:
            continue
        for name, m in ext.items():
            eps.setdefault(name, (m @ w)[3, :3])
    effects = S(hull.get('effects')) or {}
    out = []
    for i in range(1, 5):
        names = [e[0] for e in (S(effects.get(f'fire{i}')) or ()) if e] or [f'EP_Fire_{i}']
        pos = next((eps[n] for n in names if n in eps), None)
        out.append(None if pos is None else [round(float(pos[0]), 3), round(float(pos[1]), 3), round(-float(pos[2]), 3)])
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('gamedata', help='wows_gamedata.zip (GameParams)')
    ap.add_argument('models', help='wows_models.zip (assets.bin + geometry)')
    ap.add_argument('ships', nargs='*', help='GameParams keys, e.g. PGSB310_Preussen (default: all available)')
    ap.add_argument('--tris', type=int, default=40000, help='triangle budget per ship')
    a = ap.parse_args(argv)

    gz = zipfile.ZipFile(a.gamedata)
    gname = next(n for n in gz.namelist() if n.replace('\\', '/').endswith('content/GameParams.data'))
    gp = loads(zlib.decompress(gz.read(gname)[::-1]), encoding='latin1')['']
    mz = zipfile.ZipFile(a.models)
    mnames = zip_index(mz)
    b = Builder(mz, Assets(mz.read(mnames['content/assets.bin'])))

    ships = a.ships
    if not ships:
        have = {n.rsplit('/', 1)[0] for n in mnames if n.endswith('.geometry')}
        for key, o in gp.items():
            p = S(o)
            if isinstance(p, dict) and (S(p.get('typeinfo')) or {}).get('type') == 'Ship':
                hulls = [S(p[k]) for k in sorted(p) if re.fullmatch(r'[A-Z0-9]+_Hull', k)]
                mdl = hulls[-1].get('model') if hulls and isinstance(hulls[-1], dict) else None
                if mdl and os.path.dirname(mdl) in have:
                    ships.append(key)

    os.makedirs(OUT, exist_ok=True)
    for key in ships:
        if key not in gp:
            print(f'{key}: not in GameParams')
            continue
        parts, missing = ship_layout(S(gp[key]), b)
        if parts is None:
            print(f'{key}: {missing}')
            continue
        lod = 0
        while lod < 4 and sum(b.tris(v, lod) for v, *_ in parts) > a.tris:
            lod += 1
        meshes = {}
        out_parts = []
        for vis, w, kind, hp in parts:
            if vis not in meshes:
                m = b.mesh(vis, lod)
                if m is None:
                    continue
                meshes[vis] = pack_mesh(*m)
            part = {'mesh': vis, 'm': to_three(w), 'kind': kind, 'hp': hp}
            nested = hp and re.fullmatch(r'(HP_.+?)_(HP_.+)', hp)
            if nested:
                part['parent'] = nested.group(1)        # turns with that mount (AA on a turret roof)
            elif kind in ('main', 'secondary', 'torpedo'):
                num = re.search(r'_(\d+)$', hp)
                if num:   # shells' gunBarrelID & 31 = this - 1 (checked on Preussen: 4 main, 14 ATBA)
                    part['gun'] = int(num.group(1)) - 1
            part.update(ship_layout.sectors.get(hp, {}))
            out_parts.append(part)
        total = sum(b.tris(v, lod) for v, *_ in parts)
        with open(os.path.join(OUT, key + '.json'), 'w') as f:
            json.dump({'ship': key, 'lod': lod, 'meshes': meshes, 'parts': out_parts, 'fires': ship_layout.fires},
                      f, separators=(',', ':'))
        print(f'{key}: {len(out_parts)} parts, {len(meshes)} meshes, LOD {lod}, {total} tris'
              + (f', unplaced: {missing}' if missing else ''))
    if b.mixed_bones:
        print(f'note: {b.mixed_bones} skinned render sets had bones with different bind frames (first one used)')


if __name__ == '__main__':
    main(sys.argv[1:])
