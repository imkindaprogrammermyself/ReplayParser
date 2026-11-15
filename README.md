# wowsreplay

Parses World of Warships replays (15.x) and turns them into a self-contained HTML viewer:
a 2D minimap/timeline and a 3D view with the game's terrain and ship models.

```
pip install -r requirements.txt
python -m wowsreplay my.wowsreplay          # -> out/my.html   (--json t.json to dump the timeline)
python -m unittest discover tests           # smoke tests over the replays in this folder
```

Page links can jump straight to a moment: `out/my.html#t=450&view=3d&cam=follow&dist=26&pitch=1.5`.

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
| `tools/update.py` | plan/import game data; `import_*.py` do the individual steps |
