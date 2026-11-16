# wowsreplay

Parses World of Warships replays (15.x) and turns them into a self-contained HTML viewer:
a 2D minimap/timeline and a 3D view with the game's terrain and ship models.

```
pip install -r requirements.txt
python -m wowsreplay my.wowsreplay          # -> out/my.html   (--json t.json to dump the timeline)
python -m unittest discover tests           # smoke tests over the replays in this folder
```

Web app: upload replays in the browser and keep a library of viewer pages.

```
python -m wowsreplay.server                 # http://localhost:8000/  (--port, --data server_data, --workers 2)
python -m wowsreplay.server --host 0.0.0.0  # share on your network - there is no login, anyone who reaches it can upload
```

Uploads are checked (size <= 50 MB, replay signature), stored by SHA-256 so a re-upload is instant, processed in the
background and listed newest battle first. Library pages carry only the battle's timeline (~1-2 MB compressed);
the viewer fetches the map, icons and three.js from /assets/ and /static/ (ship models and terrain only when 3D
opens), and the browser caches them across replays. `python -m wowsreplay` still writes self-contained pages. A replay from a game
version that isn't imported is reported, not crashed on (see tools/update.py). Pages rebuild themselves when the game data or viewer
changes; each library row also has Rebuild and Delete, and lists what its page is missing (map, ship models).

In-browser parsing: `http://localhost:8000/app` is the viewer with no battle in it. Drop a `.wowsreplay` there and
the browser parses it itself with the Rust parser compiled to WebAssembly (`rust/`, run in a worker), so the replay
is never uploaded or stored. The server hands out the module (`/static/wowsreplay.wasm`, ~270 KB compressed), the
game data for the replay's version (`/wasm/15_10_0.json`: entity definitions, constants, ships, ~70 KB) and
`/assets/index.json` (which maps, models and icons it has); the rest loads from /assets/ like a library page. About
half a second from drop to battle on a first visit, nothing from outside the server. A progress bar follows the
parse, and the Log panel lists every step and every file fetched (size, time, server or cache).

The Rust parser is a port of the Python one and must match it exactly: `tests/test_rust.py` compares the two
(timeline, analytics, asset links) on every replay. After changing the parser, change both, then
`python tools/build_wasm.py` (needs Rust: https://rustup.rs, plus `rustup target add wasm32-unknown-unknown`) - it
rebuilds the module into viewer/vendor/ and the native CLI the test uses.

Game files, all in one (game installed on another PC): copy `tools/game_full.bat` and wowsunpack.exe (ModSDK) there,
run `game_full.bat "C:\path\to\World_of_Warships"` (needs ~10 GB free), bring back `wows_full.zip`, then
`python tools/update.py import wows_full.zip` - every map, ship model and icon, once per game patch
(regenerate the .bat with `python tools/update.py full`).

Page links can jump straight to a moment: `out/my.html#t=450&view=3d&cam=follow&dist=26&pitch=1.5`.

2D map: mouse wheel zooms, drag pans, double-click shows the whole map. Click a ship (on the map, in 3D or its
roster row) to follow it in both views; Esc goes back to your own ship.
The ship card (top right; 'card' layer) shows the selected ship's health, speed, fires/floods, active
consumables, module damage (turrets, torpedoes, secondaries, AA, engine, steering) and damage/hits.
Heatmaps (2D, bottom bar), for all ships / allies / enemies / the selected ship, since the start or over the last
1 / 2 / 5 minutes; hover the map for the value under the cursor:
- time spent; map control (ally minus enemy presence, green / grey / red); where spotted (the selected ship's or the
  allies' positions, violet = hidden ... orange = spotted); threat (live: how many hostile ships seen in the last 30 s
  have their main guns in range of each spot);
- damage dealt from (fire / flood ticks left out) and damage taken; main-battery shell landings (the aim point when the
  landing wasn't reported) and shell hits on ships;
- torpedo traffic, aircraft activity, planes shot down.
Aim ('aim' layer): where the selected ship's main battery points (Vehicle.targetLocalPos), drawn as the patch the
real aim lies in (one 1.4 deg bearing step by one ~178 m range step). Player camera mode in 3D adds the game-style
crosshair: range from own ship to the ship, land or water under it, held at the guns' reach.

Analysis (bottom bar), from wowsreplay/analytics.py - computed with the timeline, so library pages, `python -m wowsreplay`
pages and /app all have it:
- Ship report (any ship; the recording player's is complete, others as far as their client saw): damage dealt / taken
  by source (main battery, secondaries, torpedoes, aircraft, fire, flooding - each damage event matched to the shell,
  torpedo or squadron that landed just before it, or the fire / flood running; 1-2 % stays unmatched), who it damaged
  and was damaged by, accuracy (hits / shells fired: a lower bound, ~30-45 % of landings are never reported) by
  outcome and by range, reload and gun use, torpedo hits, shells aimed at it, its angle to the shooter for each hit
  (bow-on ... broadside ... stern-on), time spotted and fire opened unspotted, damage-control delay after a fire,
  distance to the nearest enemy and to its team.
- Battle flow: each team's remaining HP over time (turning points = biggest 60 s swings), score, cap control, fights
  (damage clustered in time and place, both sides), highlights (citadels, torpedo hits, big hits, multi-kills, first
  blood, last kill - also as timeline marks: "Marks: highlights"), ships sunk and how.
- Aircraft: squadrons, planes lost and damage per carrier; planes shot down per ship (approximate: the replay doesn't
  say who shot a plane down, each loss goes to the nearest enemy ship).

Trends (`/trends`, linked from the library): win rate, damage, accuracy, kills and survival across the library's
replays per player / mode, damage per battle over time, by ship, by map, where you sank on each map, playstyle
(distance to enemies and team, time spotted, broadside hits, first to sink). Replays processed before analytics
existed are re-analysed in the background when the page opens.

3D cameras: Orbit, Follow (own or the clicked ship), Player camera (the recorded one) and Fly - WASD move, Q/E down/up,
drag to look, Shift for x4, mouse wheel sets the flying speed. Fly links: `#t=450&view=3d&cam=fly&pos=x,height,z&look=yaw,pitch`.

## Game data

Parsing needs the entity definitions of the replay's game version (`wowsreplay/data/<version>/`).
Maps, terrain and ship models come from the game client and are optional (the viewer falls back to shapes).
The game files are read with Wargaming's `wowsunpack.exe` (ModSDK), which can run on another PC:

```
python tools/update.py plan                 # what the replays need -> tools/game_update.bat
game_update.bat "C:\Games\World_of_Warships"   (on the game PC, next to wowsunpack.exe -> wows_update.zip)
python tools/update.py import               # imports wows_update.zip, rebuilds pages, plans again
```

After a game patch: `plan --refresh`. A new version copies the arena-state key map from the previous one;
`import` and the tests flag it if the game changed it (as 15.10 did: see `data/15_10_0/constants.json`).

Extracted game files and pages built from them contain Wargaming assets: keep them private
(`.gitignore` leaves them out).

## Layout

| Path | What |
|---|---|
| `wowsreplay/container.py`, `packets.py` | replay file (Blowfish + zlib) and packet framing |
| `wowsreplay/defs.py`, `nested.py`, `safe_pickle.py` | entity definitions, property/method decoding, restricted unpickling |
| `wowsreplay/timeline.py` | packets -> timeline JSON (tracks, shells, aim, zones, score, ...) |
| `wowsreplay/analytics.py` | timeline -> ship reports, battle flow, fights, highlights, aircraft, trends line |
| `wowsreplay/geometry.py`, `assets.py` | `.geometry` (meshoptimizer) and `assets.bin` readers |
| `viewer/viewer.html` | the page; three.js r158 is inlined at build time |
| `wowsreplay/server.py` | web app: library, uploads, /assets/, /trends, /app + /wasm/ for in-browser parsing |
| `rust/` | the parser + analytics in Rust: WebAssembly for /app, native CLI for the parity test (tools/build_wasm.py) |
| `tools/update.py` | plan/import game data; `import_*.py` do the individual steps |
