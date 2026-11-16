"""The data the Rust / WebAssembly parser needs for one game version, as one JSON object.

    {'version', 'defs' (defs.export), 'constants', 'ships' (trimmed), 'consumables' (ids), 'drops', 'aircraft'}

The server sends it to the browser (/wasm/<version>.json) together with the asset index; tests/test_rust.py
feeds it to the native build to compare the two parsers.
"""
import json
import os

from .defs import Definitions, export

DATA = os.path.join(os.path.dirname(__file__), 'data')
SHIP_FIELDS = ('index', 'species', 'level', 'length', 'mainRange', 'concealment', 'consumables', 'modules')


def _json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def data_bundle(version):
    """None when that game version isn't imported."""
    vdir = os.path.join(DATA, version)
    if not os.path.isfile(os.path.join(vdir, 'scripts', 'entities.xml')):
        return None
    ships = _json(os.path.join(vdir, 'ships.json'), {})
    return {
        'version': version,
        'defs': export(Definitions(os.path.join(vdir, 'scripts'))),
        'constants': _json(os.path.join(vdir, 'constants.json'), {}),
        'ships': {k: {f: v[f] for f in SHIP_FIELDS if f in v} for k, v in ships.items()},
        'consumables': (_json(os.path.join(DATA, 'consumables.json'), {}) or {}).get('ids', {}),
        'drops': _json(os.path.join(DATA, 'drops.json'), {}),
        'aircraft': _json(os.path.join(DATA, 'aircraft.json'), {}),
    }
