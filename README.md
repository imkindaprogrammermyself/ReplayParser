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
the browser parses it itself - the Python parser runs in Pyodide (WebAssembly, from cdn.jsdelivr.net) in a worker -
so the replay is never uploaded or stored. The server only hands out the parser bundle for the replay's game version
(`/pybundle/15_10_0.zip`: the parser + entity defs, constants, ships, ~150 KB) and `/assets/index.json` (which maps,
models and icons it has); the rest loads from /assets/ like a library page. First visit: ~6 MB from the CDN
(Pyodide + pycryptodome, cached after); a 24-ship battle parses in ~3 s. Needs internet access for Pyodide.
A progress bar follows the parse (real packet progress), and the Log panel (bottom bar) lists every step and every
file the page and the parser worker fetched - size, time, and whether it came from the CDN, this server or the cache.

Game files, all in one (game installed on another PC): copy `tools/game_full.bat` and wowsunpack.exe (ModSDK) there,
run `game_full.bat "C:\path\to\World_of_Warships"` (needs ~10 GB free), bring back `wows_full.zip`, then
`python tools/update.py import wows_full.zip` - every map, ship model and icon, once per game patch
(regenerate the .bat with `python tools/update.py full`).

Page links can jump straight to a moment: `out/my.html#t=450&view=3d&cam=follow&dist=26&pitch=1.5`.

2D map: mouse wheel zooms, drag pans, double-click shows the whole map. Click a ship (on the map, in 3D or its
roster row) to follow it in both views; Esc goes back to your own ship.
The ship card (top right; 'card' layer) shows the selected ship's health, speed, fires/floods, active
consumables, module damage (turrets, torpedoes, secondaries, AA, engine, steering) and damage/hits.
Heatmap (2D, bottom bar): where all ships / allies / enemies / the selected ship spent time, since the start or
over the last 2 minutes; hover the map for the time spent under the cursor.
Aim ('aim' layer): where the selected ship's main battery points (Vehicle.targetLocalPos), drawn as the patch the
real aim lies in (one 1.4 deg bearing step by one ~178 m range step). Player camera mode in 3D adds the game-style
crosshair: range from own ship to the ship, land or water under it, held at the guns' reach.

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
| `wowsreplay/geometry.py`, `assets.py` | `.geometry` (meshoptimizer) and `assets.bin` readers |
| `viewer/viewer.html` | the page; three.js r158 is inlined at build time |
| `wowsreplay/server.py` | web app: library, uploads, /assets/, /app + /pybundle/ for in-browser parsing |
| `tools/update.py` | plan/import game data; `import_*.py` do the individual steps |
