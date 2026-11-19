"""A shareable demo page of one battle, e.g. for GitHub Pages:

    python tools/make_demo.py REPLAY [-o docs/index.html]

The self-contained viewer page (minimap, terrain, ship models and icons embedded, like python -m wowsreplay;
--no-wg-assets leaves the Wargaming files out: grid-only 2D map, placeholder hulls in 3D). Other players are
anonymised: names become 'Ally 3' / 'Enemy 5', clan tags and the chat (it carries account ids) are dropped. Your own
name stays (--hide-me replaces it with 'You'). The page is checked afterwards: it must not contain any original name,
clan tag or account id.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT]

from wowsreplay.__main__ import build_data, render_page  # noqa: E402


def anonymise(data, hide_me=False):
    """Returns the strings that must not appear in the page any more (original names, clan tags, account ids)."""
    secrets = set()
    counters = {'ally': 0, 'enemy': 0}
    for s in sorted(data['ships'], key=lambda s: (s['relation'] != 'self', s['relation'], s['id'])):
        if s.get('clan'):
            secrets.add(s['clan'])
        s['clan'] = ''
        if s['relation'] == 'self':
            if hide_me:
                secrets.add(s['name'])
                s['name'] = data['meta']['player'] = 'You'
            continue
        secrets.add(s['name'])
        side = 'ally' if s['relation'] == 'ally' else 'enemy'
        counters[side] += 1
        s['name'] = f'{side.capitalize()} {counters[side]}'
    for c in data['chat']:
        secrets.add(str(c[1]))
    data['chat'] = []
    return {x for x in secrets if x and len(x) > 2}


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python tools/make_demo.py', description=__doc__.split('\n\n')[0])
    ap.add_argument('replay', help='.wowsreplay file')
    ap.add_argument('-o', '--out', default=os.path.join(ROOT, 'docs', 'index.html'), help='default: docs/index.html')
    ap.add_argument('--hide-me', action='store_true', help="replace your own name with 'You' too")
    ap.add_argument('--no-wg-assets', action='store_true',
                    help='leave out the Wargaming files (minimap, terrain, ship models, icons)')
    a = ap.parse_args(argv)

    data = build_data(a.replay, embed=not a.no_wg_assets)
    if a.no_wg_assets:
        assert not (data['map'] or data['models'] or data['icons'] or data['classIcons'] or data['planeIcons'])
    secrets = anonymise(data, a.hide_me)
    page = render_page(data)                    # three.js (MIT) inlined, so 3D works offline too
    found = sorted(x for x in secrets if json.dumps(x)[1:-1] in page)
    if found:
        sys.exit(f'refusing to write: the page still contains {len(found)} hidden name(s) / id(s)')
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write(page)
    m = data['meta']
    print(f'{a.out}: {len(page) / 1048576:.1f} MB | {m["map"]} | {m["gameType"]} | {len(data["ships"])} ships, '
          f'{len(secrets)} names / ids removed | map: {"yes" if data["map"] else "no"}, ship models: '
          f'{len(data["models"])} of {len({s["ship"] for s in data["ships"]})} types')


if __name__ == '__main__':
    main()
