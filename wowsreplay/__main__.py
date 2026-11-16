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


def load_class_icons(base=None):
    """The game's minimap class icons (gui/fla/minimap/ship_icons*/minimap_*.svg) as SVG text, by file stem.
    White parts are tinted with the team colour in the viewer, like the game does."""
    out = {}
    for folder in ('ship_icons', 'ship_icons_self'):
        d = os.path.join(GUI, 'fla', 'minimap', folder)
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith('.svg'):
                    if base is not None:   # lazy: URL, the viewer fetches the SVG text to tint it
                        out[name[:-4]] = _ref(os.path.join(d, name), '', base)
                        continue
                    with open(os.path.join(d, name), encoding='utf-8') as f:
                        out[name[:-4]] = f.read()
    return out


PLANE_ICONS = os.path.join(GUI, 'battle_hud', 'markers_minimap', 'plane')


def load_plane_icons(planes, base=None):
    """Tag each squadron with its minimap icon (data/aircraft.json) and embed those icons, per side."""
    try:
        with open(os.path.join(os.path.dirname(__file__), 'data', 'aircraft.json'), encoding='utf-8') as f:
            kinds = json.load(f)
    except FileNotFoundError:
        return {}
    out = {}
    for sq in planes:
        icon = kinds.get(str(sq.get('params')))
        if not icon:
            continue
        for side in ('ally', 'enemy', 'own'):
            p = os.path.join(PLANE_ICONS, *f'{icon}_{side}.png'.split('/'))
            if f'{icon}_{side}' not in out and os.path.isfile(p):
                out[f'{icon}_{side}'] = _ref(p, 'image/png', base)
        if f'{icon}_ally' in out:
            sq['icon'] = icon
    return out


def load_icons(ships, base=None):
    """The game's side icons per ship type (gui/ship_icons/<PXXX123>.png, + ship_dead_icons), as data URIs."""
    out = {}
    for key in sorted({s['ship'] for s in ships if s.get('ship')}):
        icons = {}
        for kind, folder in (('alive', 'ship_icons'), ('dead', 'ship_dead_icons')):
            p = os.path.join(GUI, folder, key.split('_')[0] + '.png')
            if os.path.isfile(p):
                icons[kind] = _ref(p, 'image/png', base)
        if icons:
            out[key] = icons
    return out


DATA = os.path.join(os.path.dirname(__file__), 'data')


def _ref(path, mime, base):
    """A file for the page: embedded as a data: URI (base None), or a URL under the server's asset path."""
    if base is not None:
        return base + os.path.relpath(path, DATA).replace(os.sep, '/')
    with open(path, 'rb') as f:
        return f'data:{mime};base64,' + base64.b64encode(f.read()).decode()


def model_urls(ships, base):
    """Lazy pages: where each imported ship model is (fetched by the viewer when 3D opens)."""
    keys = sorted({s['ship'] for s in ships if s.get('ship')})
    return {k: _ref(os.path.join(MODELS, k + '.json'), '', base) for k in keys if os.path.isfile(os.path.join(MODELS, k + '.json'))}


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


def load_map(name, base=None):
    """Minimap image + heightmap from tools/import_spaces.py, or None if that map wasn't imported."""
    d = os.path.join(MAPS, name or '')
    if not name or not os.path.isfile(os.path.join(d, 'map.json')):
        return None
    with open(os.path.join(d, 'map.json'), encoding='utf-8') as f:
        m = json.load(f)
    out = {'name': name, 'minimapBounds': m.get('minimap'), 'terrain': None, 'minimap': None}
    png = os.path.join(d, 'minimap.png')
    if m.get('minimap') and os.path.isfile(png):
        out['minimap'] = _ref(png, 'image/png', base)
    ter = os.path.join(d, 'terrain.i16')
    if m.get('terrain') and os.path.isfile(ter):
        if base is not None:   # lazy: the viewer fetches the heights when 3D opens
            out['terrain'] = dict(m['terrain'], url=_ref(ter, '', base))
        else:
            with open(ter, 'rb') as f:
                out['terrain'] = dict(m['terrain'], data=base64.b64encode(f.read()).decode())
    return out


def build_data(replay_path, embed=True, asset_base=None) -> dict:
    """Timeline + the imported map, models and icons this battle needs (embed=False: timeline only).

    asset_base (e.g. '/assets/'): a lazy page - files are referenced by URL under that path instead of embedded,
    and the viewer fetches them (models / terrain only when 3D opens), so the browser caches them across replays."""
    data = build_timeline(replay_path)
    lazy = asset_base is not None
    data['map'] = load_map(data['meta']['map'], asset_base) if embed or lazy else None
    data['models'] = load_models(data['ships']) if embed and not lazy else {}
    if lazy:
        data['modelUrls'] = model_urls(data['ships'], asset_base)
    data['icons'] = load_icons(data['ships'], asset_base) if embed or lazy else {}
    data['classIcons'] = load_class_icons(asset_base) if embed or lazy else {}
    if lazy:
        data['lazyClassIcons'] = True       # classIcons hold URLs; the viewer fetches the SVG text
    data['planeIcons'] = load_plane_icons(data['planes'], asset_base) if embed or lazy else {}
    if data['map'] and data['map']['minimapBounds']:
        data['meta']['bounds'] = data['map']['minimapBounds']   # show the whole map, like the game's minimap
    return data


def render_page(data: dict, three_url=None) -> str:
    """The viewer page (viewer.html + three.js + the data) as HTML text; three_url: load three.js from there
    (a cached script) instead of inlining it."""
    payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    with open(VIEWER, encoding='utf-8') as f:
        template = f.read()
    if PLACEHOLDER not in template or THREE_PLACEHOLDER not in template:
        raise RuntimeError('viewer.html is missing a placeholder')
    if three_url:   # a cached script instead of 640 KB inlined in every page
        page = template.replace(f'<script>{THREE_PLACEHOLDER}</script>', f'<script src="{three_url}"></script>', 1)
    else:
        with open(THREE_JS, encoding='utf-8') as f:
            three = f.read()
        # keep "</script>" inside embedded text from closing the script tag
        page = template.replace(THREE_PLACEHOLDER, three.replace('</script', '<\\/script'), 1)
    return page.replace(PLACEHOLDER, payload.replace('</', '<\\/'), 1)


def summary(data: dict) -> str:
    m = data['meta']
    return (f"{m['map']} | {m['gameType']} | {m['version']} | {len(data['ships'])} ships | "
            f"{m['end']:.0f}s | {len(data['deaths'])} deaths | {len(data['shots'])} shells | "
            f"map data: {'yes' if data['map'] else 'no (run tools/import_spaces.py)'} | "
            f"ship models: {len(data['models'])} of {len({s['ship'] for s in data['ships']})} types")


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m wowsreplay', description='WoWS replay -> minimap timeline')
    ap.add_argument('replay', help='.wowsreplay file')
    ap.add_argument('-o', '--html', help='write a self-contained viewer page (default: out/<replay>.html)')
    ap.add_argument('--json', help='also write the timeline JSON here')
    ap.add_argument('--no-map', action='store_true', help="don't embed the imported minimap/terrain/ship models")
    args = ap.parse_args(argv)

    data = build_data(args.replay, embed=not args.no_map)
    if args.json:
        payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        with open(args.json, 'w', encoding='utf-8') as f:
            f.write(payload)
        print(f'json: {args.json} ({len(payload) / 1024:.0f} KB)')

    html_path = args.html or os.path.join('out', os.path.splitext(os.path.basename(args.replay))[0] + '.html')
    try:
        page = render_page(data)
    except RuntimeError as e:
        sys.exit(str(e))
    os.makedirs(os.path.dirname(html_path) or '.', exist_ok=True)
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(page)
    print(f'html: {html_path}')
    print(summary(data))
    if data['parseErrors']:
        print('packets skipped:', data['parseErrors'], file=sys.stderr)


if __name__ == '__main__':
    main()
