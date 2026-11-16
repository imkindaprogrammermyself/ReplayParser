"""Build the Rust parser (rust/): the WebAssembly module the in-browser viewer (/app) uses, and the native CLI the
tests compare with the Python parser.

    python tools/build_wasm.py          # needs Rust (rustup) with the wasm32-unknown-unknown target

Copies the module to viewer/vendor/wowsreplay.wasm (committed, so the server works without a Rust toolchain).
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRATE = os.path.join(ROOT, 'rust')
OUT = os.path.join(ROOT, 'viewer', 'vendor', 'wowsreplay.wasm')


def cargo():
    exe = shutil.which('cargo') or os.path.join(os.path.expanduser('~'), '.cargo', 'bin', 'cargo' + ('.exe' if os.name == 'nt' else ''))
    if not os.path.isfile(exe):
        sys.exit('cargo not found: install Rust from https://rustup.rs and run '
                 '`rustup target add wasm32-unknown-unknown`')
    return exe


def main():
    c = cargo()
    subprocess.run([c, 'build', '--release', '--target', 'wasm32-unknown-unknown', '--lib'], cwd=CRATE, check=True)
    subprocess.run([c, 'build', '--release', '--bin', 'wowsreplay-cli'], cwd=CRATE, check=True)
    shutil.copyfile(os.path.join(CRATE, 'target', 'wasm32-unknown-unknown', 'release', 'wowsreplay.wasm'), OUT)
    print(f'{os.path.relpath(OUT, ROOT)}: {os.path.getsize(OUT) / 1024:.0f} KB')


if __name__ == '__main__':
    main()
