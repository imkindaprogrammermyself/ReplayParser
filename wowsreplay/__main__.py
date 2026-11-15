"""CLI: python -m wowsreplay REPLAY [-o out.html] [--json out.json]"""
import argparse
import base64
import json
import os
import sys

from .timeline import build_timeline

VIEWER = os.path.join(os.path.dirname(__file__), '..', 'viewer', 'viewer.html')
THREE_JS = os.path.join(os.path.dirname(__file__), '..', 'viewer', 'vendor', 'three-0.158.0.min.js')
MAPS = os.path.join(os.path.dirname(__file__), 'data', 'maps')
MODELS = os.path.join(os.path.dirname(__file__), 'data', 'models')
GUI = os.path.join(os.path.dirname(__file__), 'data', 'gui')


def load_class_icons():
    """The game's minimap class icons (gui/fla/minimap/ship_icons*/minimap_*.svg) as SVG text, by file stem.
    White parts are tinted with the team colour in the viewer, like the game does."""
    out = {}
    for folder in ('ship_icons', 'ship_icons_self'):
        d = os.path.join(GUI, 'fla', 'minimap', folder)
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith('.svg'):
                    with open(os.path.join(d, name), encoding='utf-8') as f:
                        out[name[:-4]] = f.read()
    return out


def load_icons(ships):
    """The game's side icons per ship type (gui/ship_icons/<PXXX123>.png, + ship_dead_icons), as data URIs."""
    out = {}
    for key in sorted({s['ship'] for s in ships if s.get('ship')}):
        icons = {}
        for kind, folder in (('alive', 'ship_icons'), ('dead', 'ship_dead_icons')):
            p = os.path.join(GUI, folder, key.split('_')[0] + '.png')
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    icons[kind] = 'data:image/png;base64,' + base64.b64encode(f.read()).decode()
        if icons:
            out[key] = icons
    return out


def load_models(ships):
    """Ship models from tools/import_models.py for the ship types in this battle."""
    out = {}
    for key in sorted({s['ship'] for s in ships if s.get('ship')}):
        p = os.path.join(MODELS, key + '.json')
        if os.path.isfile(p):
            with open(p, encoding='utf-8') as f:
                out[key] = json.load(f)
    return out
PLACEHOLDER = '/*__REPLAY_DATA__*/null'
THREE_PLACEHOLDER = '/*__THREE_JS__*/'


def load_map(name):
    """Minimap image + heightmap from tools/import_spaces.py, or None if that map wasn't imported."""
    d = os.path.join(MAPS, name or '')
    if not name or not os.path.isfile(os.path.join(d, 'map.json')):
        return None
    with open(os.path.join(d, 'map.json'), encoding='utf-8') as f:
        m = json.load(f)
    out = {'name': name, 'minimapBounds': m.get('minimap'), 'terrain': None, 'minimap': None}
    png = os.path.join(d, 'minimap.png')
    if m.get('minimap') and os.path.isfile(png):
        with open(png, 'rb') as f:
            out['minimap'] = 'data:image/png;base64,' + base64.b64encode(f.read()).decode()
    ter = os.path.join(d, 'terrain.i16')
    if m.get('terrain') and os.path.isfile(ter):
        with open(ter, 'rb') as f:
            out['terrain'] = dict(m['terrain'], data=base64.b64encode(f.read()).decode())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m wowsreplay', description='WoWS replay -> minimap timeline')
    ap.add_argument('replay', help='.wowsreplay file')
    ap.add_argument('-o', '--html', help='write a self-contained viewer page (default: out/<replay>.html)')
    ap.add_argument('--json', help='also write the timeline JSON here')
    ap.add_argument('--no-map', action='store_true', help="don't embed the imported minimap/terrain/ship models")
    args = ap.parse_args(argv)

    data = build_timeline(args.replay)
    data['map'] = None if args.no_map else load_map(data['meta']['map'])
    data['models'] = {} if args.no_map else load_models(data['ships'])
    data['icons'] = {} if args.no_map else load_icons(data['ships'])
    data['classIcons'] = {} if args.no_map else load_class_icons()
    if data['map'] and data['map']['minimapBounds']:
        data['meta']['bounds'] = data['map']['minimapBounds']   # show the whole map, like the game's minimap
    payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            f.write(payload)
        print(f'json: {args.json} ({len(payload) / 1024:.0f} KB)')

    html_path = args.html or os.path.join('out', os.path.splitext(os.path.basename(args.replay))[0] + '.html')
    with open(VIEWER, encoding='utf-8') as f:
        template = f.read()
    if PLACEHOLDER not in template or THREE_PLACEHOLDER not in template:
        sys.exit('viewer.html is missing a placeholder')
    with open(THREE_JS, encoding='utf-8') as f:
        three = f.read()
    os.makedirs(os.path.dirname(html_path) or '.', exist_ok=True)
    with open(html_path, 'w', encoding='utf-8') as f:
        # keep "</script>" inside embedded text from closing the script tag
        page = template.replace(THREE_PLACEHOLDER, three.replace('</script', '<\\/script'), 1)
        f.write(page.replace(PLACEHOLDER, payload.replace('</', '<\\/'), 1))
    print(f'html: {html_path}')

    m = data['meta']
    print(f"{m['map']} | {m['gameType']} | {m['version']} | {len(data['ships'])} ships | "
          f"{m['end']:.0f}s | {len(data['deaths'])} deaths | {len(data['shots'])} shells | "
          f"map data: {'yes' if data['map'] else 'no (run tools/import_spaces.py)'} | "
          f"ship models: {len(data['models'])} of {len({s['ship'] for s in data['ships']})} types")
    if data['parseErrors']:
        print('packets skipped:', data['parseErrors'], file=sys.stderr)


if __name__ == '__main__':
    main()
