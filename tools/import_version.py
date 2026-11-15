"""Import one game version's data from a replays_unpack checkout.

    python tools/import_version.py <replays_unpack>/replay_unpack/clients/wows/versions/15_9_0

Copies scripts/entities.xml + scripts/entity_defs, and extracts the few
constants we need into constants.json / ships.json. The Python modules are
read with `ast.literal_eval`, never imported or executed.
"""
import ast
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'wowsreplay', 'data')
WANTED = ('id_property_map', 'id_property_map_bots', 'id_property_map_observer', 'DEATH_TYPES')


def literal_assignments(path, names):
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), path)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    found[target.id] = ast.literal_eval(node.value)
    return found


def main(src):
    version = os.path.basename(os.path.normpath(src))
    out = os.path.join(DATA, version)
    scripts_out = os.path.join(out, 'scripts')
    if os.path.exists(scripts_out):
        shutil.rmtree(scripts_out)
    os.makedirs(scripts_out)
    shutil.copy(os.path.join(src, 'scripts', 'entities.xml'), scripts_out)
    shutil.copytree(os.path.join(src, 'scripts', 'entity_defs'), os.path.join(scripts_out, 'entity_defs'))

    consts = literal_assignments(os.path.join(src, 'constants.py'), WANTED)
    missing = set(WANTED) - set(consts)
    if missing:
        sys.exit(f'constants.py lacks {sorted(missing)}')
    consts['DEATH_TYPES'] = {k: v['name'] for k, v in consts['DEATH_TYPES'].items()}
    with open(os.path.join(out, 'constants.json'), 'w', encoding='utf-8') as f:
        json.dump(consts, f, indent=1)

    ships = {}
    params = literal_assignments(os.path.join(src, 'game_params.py'), ('BY_ID',)).get('BY_ID', {})
    for pid, p in params.items():
        info = p.get('typeinfo', {})
        if info.get('type') == 'Ship':
            ships[pid] = {'index': p.get('index'), 'species': info.get('species'),
                          'nation': info.get('nation'), 'level': p.get('level')}
    with open(os.path.join(out, 'ships.json'), 'w', encoding='utf-8') as f:
        json.dump(ships, f, separators=(',', ':'))
    print(f'{version}: copied defs, {len(ships)} ships -> {os.path.normpath(out)}')


if __name__ == '__main__':
    main(sys.argv[1])
