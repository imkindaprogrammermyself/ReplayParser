# Notes for Claude (project context; README.md describes the project itself)

World of Warships replay parser + 2D/3D viewer + web app. Python (wowsreplay/), the viewer page (viewer/viewer.html),
and a Rust port of the parser compiled to WebAssembly (rust/) for in-browser parsing on /app.

## Ways of working
- **The game is installed on a different PC (Windows).** Nothing can be extracted here. Use
  `python tools/update.py plan` (or `full`) to write a .bat for the game PC, the user runs it there with wowsunpack.exe
  (ModSDK) and brings back a zip, then `python tools/update.py import <zip>`. Don't ask for a game install path.
  The .bat files must call `%SystemRoot%\System32\tar.exe` (Git's GNU tar can't write zips).
- **Two parsers must stay identical.** Library pages and `python -m wowsreplay` use the Python parser; /app uses the
  Rust one. Any parser / analytics change goes into BOTH, then `python tools/build_wasm.py` (rebuilds
  viewer/vendor/wowsreplay.wasm, committed, and the native CLI) and `python -m unittest discover tests` -
  tests/test_rust.py requires byte-for-byte equal output on every replay. Python round() == Rust format-then-parse.
- Untrusted input: replays are parsed with a restricted unpickler (no plain pickle, ever); upload / stream size caps.
- Keep Wargaming assets private (pages embed minimaps / models); the server has no login, local use by default.
  Exception, the user's call (2026-11-20): docs/index.html (GitHub Pages demo, tools/make_demo.py) embeds them;
  players stay anonymised there.
- Commit messages end with the Co-Authored-By line; commit only when asked.

## Moving to a new machine (Fedora from 2026-11-17)
- `pip install -r requirements.txt` (pycryptodomex, numpy). Rust: `sudo dnf install rustup gcc && rustup-init -y`,
  `rustup target add wasm32-unknown-unknown`.
- Not in git, copy them over or re-import: wowsreplay/data/{maps,models,gui}/ (~370 MB, or re-import wows_full.zip
  with tools/update.py), the *.wowsreplay files the tests use (project root), server_data/ (the web library).
- Headless browser checks used Playwright's Chromium driven over the DevTools protocol by small stdlib scripts
  (a WebSocket client, no playwright package); rewrite them if needed.

## State (2026-11-17)
Done: parser + timeline, 2D/3D viewer (fly camera, aim, shells, torps, planes, smoke, fires/floods, spotting, pings),
web app (library, lazy pages, /trends), battle analytics (ship report, momentum, fights, highlights, aircraft),
heatmap layers, /app with the Rust/WASM parser (~0.5 s per replay, nothing from a CDN).
Open / offered, not done: retire the Python parser (server via the Rust CLI or PyO3); production hardening
(reverse proxy, public /app-only mode, auth, rate limits, storage quota); Operations (Building entities, tasks);
local weather; pings / spotting in 3D. Video export: wowsreplay/video.py (headless Chrome over CDP, stdlib
WebSocket, viewer #video=1 + window.VIDEO.frame, ffmpeg) - Python pages only, not in /app. Unconfirmed: Vermont's 'auxTorpBooster' label ('Torpedo boost').

## Findings worth keeping
- burningFlags bits 0-3 = fire nodes (bow -> stern, GameParams hull fire1..fire4), bits 4-5 flood nodes.
- hitType: 0x61 ricochet, 0x62 citadel, 0x63 penetration, 0x64 overpenetration, 0x60 mixed / shatter; 0x40 terrain,
  0x20 / 0x22 water, 0 expired.
- Hit-location module types: 1 main, 2 secondary, 5 engine, 9 torpedo, 12 AA, 14 steering, 15 depth charges,
  21 sonar; flags 2 = out until the next event, 3 = destroyed (AA / secondaries return ~150 s later).
- Shell real flight = 0.363 x SHOT.serverTimeLeft; 1 world unit = 30 m; aim (targetLocalPos) steps 1.4 deg / ~178 m.
- visibilityFlags: 1 ships, 2 + 256 aircraft, 16 radar, 64 unknown. Sonar pings: speed / 0.363; mark 25 s, 65 s twice.
- Squadron waypoints run 1.3-2.3 s ahead of the minimap; the parser fits the lag per squadron.
- InteractiveZone.type 9 capture, 6 Arms Race buff (BattleLogic state.drop -> data/drops.json), 12 patrol fighters.
- Damage rows arrive ~0.5-0.7 s after the shell lands; ~55-70 % of own shells report where they ended (hits almost
  always), so accuracy = hits / shells fired. Arms Race HP pickups push health above maxHealth.
- Heading = (sin yaw, cos yaw), +z north.
- Packet 0x18: 52 B, one per CAMERA (0x25), constant 10 x 0.0 + 3 x -1.0 all battle (2 replays, 15.10) - ignore.
  0x1D: one i32 per camera frame, changes in +-256 steps (two packed bytes?), not own targetLocalPos - unknown.
  `python -m wowsreplay REPLAY --raw 0x18` adds a Raw panel to the page for looking at any packet type.
