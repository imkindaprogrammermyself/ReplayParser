"""Import a game version from files unpacked by wowsunpack.exe (see unpack_gamedata.bat).

    python tools/import_gamedata.py wows_gamedata.zip 15_10_0 [--constants-from 15_9_0] [--update-ships 15_9_0 ...]

Writes wowsreplay/data/<version>/:
  scripts/        entities.xml + entity_defs, straight from the client
  ships.json      every Ship in content/GameParams.data, with hull size, main gun range, concealment
  constants.json  copied from --constants-from (the arena-state key maps and death reasons are not in
                  these files; they come from replays_unpack, so check a replay of this version decodes)

--update-ships adds the new per-ship fields to older versions' ships.json (ids are stable).

GameParams.data is zlib data stored byte-reversed, holding a Python 2 pickle. It is read with
wowsreplay.safe_pickle, which only lets GameParams.GPData / TypeInfo through as inert stand-ins.
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from wowsreplay.safe_pickle import Opaque, loads  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_unpack_models import model_dirs  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), '..', 'wowsreplay', 'data')


class Source:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path) if zipfile.is_zipfile(path) else None
        self.root = path
        if self.zip:
            self.names = {n.replace('\\', '/'): n for n in self.zip.namelist()}

    def find(self, suffix):
        """Archive paths ending in suffix (zip) or the matching path on disk."""
        if self.zip:
            return [k for k in self.names if k == suffix or k.endswith('/' + suffix)]
        p = os.path.join(self.root, suffix)
        return [suffix] if os.path.exists(p) else []

    def read(self, rel):
        if self.zip:
            return self.zip.read(self.names[rel])
        with open(os.path.join(self.root, rel), 'rb') as f:
            return f.read()

    def copy_tree(self, rel_dir, dst):
        if self.zip:
            prefix = rel_dir.rstrip('/') + '/'
            for k, n in self.names.items():
                if k.startswith(prefix) and not k.endswith('/'):
                    out = os.path.join(dst, *k[len(prefix):].split('/'))
                    if '..' in k.split('/'):
                        raise ValueError(f'bad path in zip: {k}')
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, 'wb') as f:
                        f.write(self.zip.read(n))
        else:
            shutil.copytree(os.path.join(self.root, rel_dir), dst)


def state(o):
    return o.state if isinstance(o, Opaque) else o


def ship_entries(gp_bytes):
    root = loads(zlib.decompress(gp_bytes[::-1]), encoding='latin1')['']
    ships = {}
    for key, o in root.items():
        p = state(o)
        if not isinstance(p, dict):
            continue
        info = state(p.get('typeinfo')) or {}
        if info.get('type') != 'Ship':
            continue
        hulls = [state(p[k]) for k in sorted(p) if re.fullmatch(r'(?:[A-Z0-9]+_Hull(?:_\d+)?|HullDefault)', k)]
        arts = [state(v) for k, v in p.items() if re.fullmatch(r'(?:[A-Z0-9]+_Artillery|ArtilleryDefault)', k)]
        hull = hulls[-1] if hulls else {}      # last in name order = top hull
        size = hull.get('size') or (None, None, None)
        ranges = [a.get('maxDist') for a in arts if isinstance(a, dict) and a.get('maxDist')]
        ships[str(p['id'])] = {
            'index': p.get('name') or key, 'species': info.get('species'), 'nation': info.get('nation'),
            'level': p.get('level'),
            'length': size[0], 'beam': size[1],                         # metres
            'mainRange': max(ranges) if ranges else None,              # metres, top guns, no upgrades
            'concealment': hull.get('visibilityFactor'),               # km, by ship, no upgrades
            'maxSpeed': hull.get('maxSpeed'),                          # knots
            'model': hull.get('model'),
            'consumables': ship_consumables(root, p),
        }
    return ships


def ship_consumables(root, ship):
    """consumableType -> {range (world units, ships), work (s)} for every consumable the ship can fit.
    A slot can offer alternatives; the replay says which was used (onConsumableUsed type id)."""
    out = {}
    for slot in (state(ship.get('ShipAbilities')) or {}).values():
        for name, variant in (state(slot) or {}).get('abils') or ():
            v = state((state(root.get(name)) or {}).get(variant))
            if not isinstance(v, dict) or not v.get('consumableType'):
                continue
            logic = state(v.get('logic')) or {}
            e = {'work': v.get('workTime')}
            if logic.get('distShip'):
                e['range'] = round(float(logic['distShip']), 2)
            if logic.get('radius') and v['consumableType'] == 'smokeGenerator':
                e['smokeRadius'], e['smokeLife'] = logic['radius'], logic.get('lifeTime')
            out[v['consumableType']] = e
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('source', help='wows_gamedata.zip or the unpacked folder')
    ap.add_argument('version', help='e.g. 15_10_0')
    ap.add_argument('--constants-from', default=None, help='version dir to copy constants.json from')
    ap.add_argument('--update-ships', nargs='*', default=[], help='older versions to enrich with ship fields')
    a = ap.parse_args(argv)
    if not re.fullmatch(r'\d+_\d+_\d+', a.version):
        sys.exit('version must look like 15_10_0')

    src = Source(a.source)
    out = os.path.join(DATA, a.version)
    ents = src.find('scripts/entities.xml')
    if not ents:
        sys.exit('no scripts/entities.xml in source')
    scripts_dir = ents[0][:-len('entities.xml')]
    scripts_out = os.path.join(out, 'scripts')
    if os.path.exists(scripts_out):
        shutil.rmtree(scripts_out)
    os.makedirs(scripts_out)
    with open(os.path.join(scripts_out, 'entities.xml'), 'wb') as f:
        f.write(src.read(ents[0]))
    src.copy_tree(scripts_dir + 'entity_defs', os.path.join(scripts_out, 'entity_defs'))

    gp = src.find('content/GameParams.data')
    if not gp:
        sys.exit('no content/GameParams.data in source')
    gp_bytes = src.read(gp[0])
    ships = ship_entries(gp_bytes)
    with open(os.path.join(out, 'ships.json'), 'w', encoding='utf-8') as f:
        json.dump(ships, f, separators=(',', ':'))
    # ship key -> every model folder it uses (hull, guns, directors...), for tools/update.py plan
    root = loads(zlib.decompress(gp_bytes[::-1]), encoding='latin1')['']
    index = {v['index']: sorted(model_dirs(root, v['index'])) for v in ships.values() if v['index'] in root}
    with open(os.path.join(out, 'model_dirs.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, separators=(',', ':'))

    if a.constants_from:
        shutil.copy(os.path.join(DATA, a.constants_from, 'constants.json'), os.path.join(out, 'constants.json'))
    elif not os.path.exists(os.path.join(out, 'constants.json')):
        print('warning: no constants.json; pass --constants-from <older version>')
    print(f'{a.version}: defs + {len(ships)} ships -> {os.path.normpath(out)}')

    for v in a.update_ships:
        p = os.path.join(DATA, v, 'ships.json')
        with open(p, encoding='utf-8') as f:
            old = json.load(f)
        n = 0
        for k, e in old.items():
            if k in ships:
                e.update({x: ships[k][x] for x in ('length', 'beam', 'mainRange', 'concealment', 'maxSpeed', 'model', 'consumables')})
                n += 1
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(old, f, separators=(',', ':'))
        print(f'{v}: ship fields added to {n} of {len(old)} ships')


if __name__ == '__main__':
    main(sys.argv[1:])
