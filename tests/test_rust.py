"""The Rust parser (rust/, the WebAssembly module /app uses) must give exactly what the Python one gives: timeline,
analytics and asset links, on every replay. Needs the native build: python tools/build_wasm.py

    python -m unittest tests.test_rust
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wowsreplay import server  # noqa: E402
from wowsreplay.__main__ import build_data  # noqa: E402
from wowsreplay.bundle import data_bundle  # noqa: E402
from wowsreplay.container import read_header  # noqa: E402

CLI = os.path.join(ROOT, 'rust', 'target', 'release', 'wowsreplay-cli' + ('.exe' if os.name == 'nt' else ''))
REPLAYS = sorted(glob.glob(os.path.join(ROOT, '*.wowsreplay')), key=os.path.getsize)


def differences(a, b, path='', out=None):
    """Where two JSON values differ (numbers compared as numbers: 3 == 3.0)."""
    out = [] if out is None else out
    if len(out) > 20:
        return out
    if isinstance(a, bool) or isinstance(b, bool):
        if a is not b:
            out.append((path, a, b))
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a != b and abs(a - b) > 1e-9 * max(1, abs(a)):
            out.append((path, a, b))
    elif isinstance(a, dict) and isinstance(b, dict):
        for k in a.keys() | b.keys():
            if k not in a or k not in b:
                out.append((f'{path}/{k}', a.get(k, '<missing>'), b.get(k, '<missing>')))
            else:
                differences(a[k], b[k], f'{path}/{k}', out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((f'{path} (length)', len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            differences(x, y, f'{path}[{i}]', out)
    elif a != b:
        out.append((path, a, b))
    return out


@unittest.skipUnless(os.path.isfile(CLI) and REPLAYS, 'no native Rust build (python tools/build_wasm.py) or no replays')
class RustParityTests(unittest.TestCase):
    def test_same_page_data(self):
        index = json.loads(server.asset_index())
        with tempfile.TemporaryDirectory() as tmp:
            for rep in REPLAYS:
                with self.subTest(replay=os.path.basename(rep)):
                    ver = '_'.join(read_header(rep)['clientVersionFromXml'].replace(' ', '').split(',')[:3])
                    bundle = dict(data_bundle(ver), index=index, assetBase='/assets/')
                    path = os.path.join(tmp, ver + '.json')
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(bundle, f)
                    run = subprocess.run([CLI, rep, path], capture_output=True, timeout=300)
                    self.assertEqual(run.returncode, 0, run.stderr.decode(errors='replace'))
                    rust = json.loads(run.stdout)
                    python = json.loads(json.dumps(build_data(rep, asset_base='/assets/')))
                    self.assertEqual(differences(python, rust), [])

    def test_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk, bundle = os.path.join(tmp, 'x.wowsreplay'), os.path.join(tmp, 'b.json')
            with open(junk, 'wb') as f:
                f.write(b'\x12\x32\x34\x11' + b'\xff' * 64)
            with open(bundle, 'w') as f:
                json.dump(data_bundle(sorted(d for d in os.listdir(os.path.join(ROOT, 'wowsreplay', 'data')) if d[:1].isdigit())[-1]), f)
            run = subprocess.run([CLI, junk, bundle], capture_output=True, timeout=60)
            self.assertEqual(run.returncode, 1)
            self.assertIn(b'error', run.stderr)


if __name__ == '__main__':
    unittest.main()
