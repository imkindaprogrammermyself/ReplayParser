"""Keep the viewer's game data in step with your replays and the game client.

    python tools/update.py plan [REPLAY_OR_DIR ...] [--refresh]
        Works out what the replays need that isn't imported yet - game-version data (entity scripts,
        GameParams), maps (terrain + minimap) and ship models - and writes tools/game_update.bat.
        Copy that and wowsunpack.exe (ModSDK) to the PC with the game and run
            game_update.bat "C:\\Games\\World_of_Warships"
        It only reads the game and leaves wows_update.zip next to itself. Bring that back here.
        --refresh pulls every map/ship the replays use, not just missing ones (after a game patch).

    python tools/update.py import [wows_update.zip] [--version 15_11_0] [--no-pages]
        Imports everything in the zip, rebuilds out/<replay>.html for every replay and checks they
        decode. Then plans again: data from a new game version can name ships the old data didn't
        know, which takes one more round.

Replays default to *.wowsreplay in the project folder and in replays/ (recursively).
Entity scripts and GameParams can only come from the installed game version: replays from older
versions keep working only if their version was imported before the game patched.
"""
import argparse
import glob
import io
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, 'wowsreplay', 'data')
BAT = os.path.join(HERE, 'game_update.bat')
sys.path[:0] = [ROOT, HERE]

from wowsreplay.container import read_header  # noqa: E402

VERSION_RE = re.compile(r'\d+_\d+_\d+$')
BAD_BATCH_CHARS = set('"%&|<>^')


# ------------------------------------------------------------------ what we have / what replays need

def replay_files(args):
    paths = args or [ROOT, os.path.join(ROOT, 'replays')]
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += glob.glob(os.path.join(p, '*.wowsreplay'))
            if os.path.basename(p) == 'replays' or args:
                out += glob.glob(os.path.join(p, '**', '*.wowsreplay'), recursive=True)
        elif os.path.isfile(p):
            out.append(p)
    return sorted(set(map(os.path.abspath, out)))


def version_name(raw):
    parts = [int(x) for x in raw.replace(' ', '').split(',') if x]
    return '_'.join(map(str, parts[:3])), (parts[3] if len(parts) > 3 else None)


def imported_versions():
    vs = [d for d in os.listdir(DATA) if VERSION_RE.match(d)
          and os.path.isfile(os.path.join(DATA, d, 'scripts', 'entities.xml'))]
    return sorted(vs, key=lambda v: tuple(map(int, v.split('_'))))


def load_json(*parts, default=None):
    p = os.path.join(DATA, *parts)
    if not os.path.isfile(p):
        return default
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def survey(replays):
    """{replay: (version, build, map, [ship param ids])}"""
    info = {}
    for r in replays:
        try:
            h = read_header(r)
        except (OSError, ValueError) as e:
            print(f'skip {os.path.basename(r)}: {e}')
            continue
        v, build = version_name(h.get('clientVersionFromXml', ''))
        m = (h.get('mapName') or '').split('/')[-1]
        info[r] = (v, build, m, [x.get('shipId') for x in h.get('vehicles', []) if x.get('shipId')])
    return info


def ship_keys(versions):
    """param id (str) -> GameParams key, newest version wins."""
    keys = {}
    for v in versions:
        for pid, e in (load_json(v, 'ships.json', default={}) or {}).items():
            keys[pid] = e.get('index')
    return keys


# ------------------------------------------------------------------ plan

def plan(replays, refresh=False, quiet=False):
    info = survey(replays)
    if not info:
        print('no replays found')
        return False
    versions = imported_versions()
    keys = ship_keys(versions)
    index = {}
    for v in versions:                       # newest model index wins
        index.update(load_json(v, 'model_dirs.json', default={}) or {})

    need_versions = sorted({v for v, *_ in info.values()} - set(versions))
    maps, ships, unknown = set(), set(), set()
    for v, build, m, ids in info.values():
        if m:
            maps.add(m)
        for pid in ids:
            k = keys.get(str(pid))
            (ships.add(k) if k else unknown.add(pid))
    have_maps = {m for m in maps if os.path.isfile(os.path.join(DATA, 'maps', m, 'map.json'))}
    have_ships = {s for s in ships if os.path.isfile(os.path.join(DATA, 'models', s + '.json'))}
    want_maps = sorted(maps if refresh else maps - have_maps)
    want_ships = sorted(ships if refresh else ships - have_ships)
    no_index = sorted(s for s in want_ships if s not in index)
    dirs = sorted({d for s in want_ships for d in index.get(s, [])})

    newest = max(info.values(), key=lambda x: tuple(map(int, x[0].split('_'))))[0]
    print(f'{len(info)} replays, versions {sorted({v for v, *_ in info.values()})}; imported: {versions or "none"}')
    print(f'maps: {len(maps)} used, {len(want_maps)} to pull {want_maps if want_maps else ""}')
    print(f'ships: {len(ships)} types used, {len(want_ships)} to pull ({len(dirs)} model folders)')
    if no_index:
        print(f'  {len(no_index)} ship types have no model index yet (import game data first): {no_index[:8]}')
    if unknown:
        print(f'  {len(unknown)} ship ids unknown to the imported GameParams - they appear after the next import')
    if need_versions:
        print(f'versions not imported: {need_versions}. Only the version installed on the game PC can be pulled'
              f' (newest replay: {newest}).')

    gui = os.path.join(DATA, 'gui')
    no_icon = sorted(s for s in ships if not os.path.isfile(os.path.join(gui, 'ship_icons', s.split('_')[0] + '.png')))
    want_gui = refresh or bool(no_icon) or not os.path.isfile(os.path.join(gui, 'gui_files.txt'))
    print(f'gui: {"pull ship icons + file list" if want_gui else "up to date"}'
          + (f' ({len(no_icon)} ship types without an icon)' if no_icon else ''))

    nothing = not (want_maps or dirs or need_versions or unknown or no_index or want_gui)
    if nothing:
        print('nothing to pull: everything the replays use is imported')
        if os.path.exists(BAT):
            os.remove(BAT)
        return False
    write_bat(want_maps, dirs, want_ships, want_gui)
    if not quiet:
        print(f'\nwrote {os.path.relpath(BAT, ROOT)}. On the game PC, next to wowsunpack.exe:\n'
              f'    game_update.bat "C:\\path\\to\\World_of_Warships"\n'
              f'then copy wows_update.zip here and run: python tools/update.py import')
    return True


HEAD = r'''@echo off
rem Generated by tools/update.py {cmd}. {what}
rem Usage: put this file and wowsunpack.exe (from ModSDK) in one folder, then
rem        {bat} "C:\Games\World_of_Warships"
rem Reads from the game only. Writes .\{out} and zips it to .\{out}.zip.
setlocal
if "%~1"=="" (
    echo usage: %~nx0 "path\to\World_of_Warships"
    exit /b 1
)
set "GAME=%~1"
set "HERE=%~dp0"
set "UNPACK=%HERE%wowsunpack.exe"
set "OUT=%HERE%{out}"
if not exist "%UNPACK%" (
    echo wowsunpack.exe not found next to this script
    exit /b 1
)
set "BUILD="
for /f "delims=" %%d in ('dir /b /ad /o-n "%GAME%\bin"') do (
    if not defined BUILD if exist "%GAME%\bin\%%d\idx" set "BUILD=%%d"
)
if not defined BUILD (
    echo no bin\^<build^>\idx folder found in "%GAME%"
    exit /b 1
)
set "IDX=%GAME%\bin\%BUILD%\idx"
set "PKG=..\..\..\res_packages"
rem clear only a previous run of this script (it leaves update_manifest.txt)
if exist "%OUT%\update_manifest.txt" rmdir /s /q "%OUT%"
if not exist "%OUT%" mkdir "%OUT%"
set "LOG=%OUT%\extract_log.txt"
echo build %BUILD% > "%OUT%\update_manifest.txt"
echo build %BUILD% > "%LOG%"
if exist "%GAME%\game_info.xml" copy /y "%GAME%\game_info.xml" "%OUT%\game_info.xml" > nul
echo build %BUILD%

echo game data...
"%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%" -I "scripts/*" >> "%LOG%" 2>&1
"%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%" -I "content/GameParams.data" >> "%LOG%" 2>&1
'''

TAIL = r'''
echo zipping (a big pull takes a while)...
if exist "%HERE%{out}.zip" del "%HERE%{out}.zip"
rem Windows' own tar (Windows 10 1803+, bsdtar) writes zip64 quickly - called by full path, as a GNU tar earlier
rem on PATH (Git's) can't write zips. Compress-Archive is the slow fallback.
set "TAR=%SystemRoot%\System32\tar.exe"
if not exist "%TAR%" goto ps_zip
pushd "%OUT%"
set "ITEMS="
for /f "delims=" %%e in ('dir /b') do call set ITEMS=%%ITEMS%% "%%e"
"%TAR%" -a -c -f "%HERE%{out}.zip" %ITEMS%
set "ZIPERR=%errorlevel%"
popd
if "%ZIPERR%"=="0" goto zipped
:ps_zip
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\*' -DestinationPath '%HERE%{out}.zip'"
:zipped
echo done: %HERE%{out}.zip
endlocal
'''


def write_bat(maps, dirs, ships, gui=False):
    for s in list(maps) + list(dirs):
        if BAD_BATCH_CHARS & set(s):
            sys.exit(f'unexpected characters in path: {s}')
    x = '"%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%" -I "{}" >> "%LOG%" 2>&1\n'
    lines = [HEAD.format(cmd='plan', bat='game_update.bat', out='wows_update',
                         what=f'Pulls what the replays need: entity scripts + GameParams, {len(maps)} maps, '
                              f'{len(dirs)} model folders for {len(ships)} ship types.')]
    for i, m in enumerate(maps, 1):
        lines.append(f'echo map {i}/{len(maps)} {m}\n')
        lines += [x.format(f'spaces/{m}/{f}') for f in ('terrain.bin', 'space.settings', 'minimap.png')]
    if dirs:
        lines.append('echo assets.bin...\n' + x.format('content/assets.bin'))
        for i, d in enumerate(dirs, 1):
            lines.append(f'echo model {i}/{len(dirs)} {d.rsplit("/", 1)[1]}\n' + x.format(d + '/*'))
    if gui:   # ship side icons (roster), everything minimap-related (class icons), and a list of all gui files
        lines.append('echo gui...\n')
        lines.append('"%UNPACK%" -l -x "%IDX%" -p "%PKG%" -I "gui/*" > "%OUT%\\gui_files.txt" 2>> "%LOG%"\n')
        lines += [x.format(g) for g in GUI_PATTERNS]
    lines.append(TAIL.format(out='wows_update'))
    with open(BAT, 'w', encoding='ascii', newline='\r\n') as f:
        f.write(''.join(lines))


GUI_PATTERNS = ('gui/ship_icons/*', 'gui/ship_dead_icons/*', 'gui/*minimap*')
FULL_BAT = os.path.join(HERE, 'game_full.bat')
# Everything the importers read, for every ship and map: each map's terrain / settings / minimap, assets.bin, the
# .geometry of the hull folders ships use and of every gun / fitting folder (hulls, guns, directors, finders,
# radars, catapults, misc fittings, caps), and the gui icons. Textures, sounds and effects stay in the game.
FULL_PATTERNS = (
    [('maps', f'spaces/*/{f}') for f in ('terrain.bin', 'space.settings', 'minimap.png')]
    + [('ship models', 'content/assets.bin')]
    + [('ship models', f'content/gameplay/*/{kind}/*.geometry')
       for kind in ('gun', 'director', 'finder', 'radar', 'catapult', 'misc', 'cap')]
    + [('gui icons', g) for g in GUI_PATTERNS])


def used_hull_dirs():
    """Hull model folders some ship in the newest imported GameParams uses (model_dirs.json). 988 of 1505 in 15.10:
    the rest are camouflage / event / prototype hulls no ship points at - 2.6 GB of the 7.8 GB zip."""
    versions = imported_versions()
    index = load_json(versions[-1], 'model_dirs.json', default={}) if versions else {}
    return sorted({d for dirs in index.values() for d in dirs if '/ship/' in d})


def write_full_bat():
    """tools/game_full.bat: every map, ship model and icon the viewer can use - one big pull per game patch."""
    x = '"%UNPACK%" -x "%IDX%" -p "%PKG%" -o "%OUT%" -I "{}" >> "%LOG%" 2>&1\n'
    hulls = used_hull_dirs()
    for d in hulls:
        if BAD_BATCH_CHARS & set(d):
            sys.exit(f'unexpected characters in path: {d}')
    what = ('Pulls everything the viewer uses for every map and ship (needs ~8 GB free); '
            f'hulls: the {len(hulls)} folders ships use in the newest imported game data.' if hulls else
            'Pulls everything the viewer uses for every map and ship (needs ~11 GB free); '
            'all hull folders (no game data imported yet to pick the used ones).')
    lines = [HEAD.format(cmd='full', bat='game_full.bat', out='wows_full', what=what)]
    last = None
    for step, pattern in FULL_PATTERNS:
        if step != last:
            lines.append(f'echo {step}...\n')
            last = step
        lines.append(x.format(pattern))
    if hulls:   # after a patch, brand-new ships' hulls aren't listed: update.py import's final plan lists them
        for i, d in enumerate(hulls, 1):
            if i % 100 == 1:
                lines.append(f'echo hulls {i}-{min(i + 99, len(hulls))} of {len(hulls)}...\n')
            lines.append(x.format(d + '/*.geometry'))
    else:
        lines.append(x.format('content/gameplay/*/ship/*.geometry'))
    lines.append('"%UNPACK%" -l -x "%IDX%" -p "%PKG%" -I "gui/*" > "%OUT%\\gui_files.txt" 2>> "%LOG%"\n')
    lines.append(TAIL.format(out='wows_full'))
    with open(FULL_BAT, 'w', encoding='ascii', newline='\r\n') as f:
        f.write(''.join(lines))
    print(f'wrote {os.path.relpath(FULL_BAT, ROOT)}. On the game PC, next to wowsunpack.exe:\n'
          f'    game_full.bat "C:\\path\\to\\World_of_Warships"\n'
          f'then copy wows_full.zip here and run: python tools/update.py import wows_full.zip')


# ------------------------------------------------------------------ import

def zip_version(z, info, override):
    """Game version of the files in the zip: --version, else a replay with the same build, else game_info.xml."""
    if override:
        if not VERSION_RE.match(override):
            sys.exit('--version must look like 15_11_0')
        return override
    names = {n.replace('\\', '/').split('/')[-1]: n for n in z.namelist()}
    build = None
    if 'update_manifest.txt' in names:
        m = re.search(rb'build\s+(\d+)', z.read(names['update_manifest.txt']))
        build = int(m.group(1)) if m else None
    for v, b, *_ in info.values():
        if build and b == build:
            return v
    if 'game_info.xml' in names:
        m = re.search(rb'(\d+)\.(\d+)\.(\d+)\.(\d+)', z.read(names['game_info.xml']))
        if m:
            return '_'.join(x.decode() for x in m.groups()[:3])
    sys.exit(f'cannot tell the game version of build {build}: pass --version (e.g. 15_11_0)')


def do_import(zpath, replays, override=None, pages=True):
    import import_gamedata
    import import_models
    import import_spaces

    if not os.path.isfile(zpath):
        sys.exit(f'{zpath} not found')
    info = survey(replays)
    z = zipfile.ZipFile(zpath)
    names = [n.replace('\\', '/') for n in z.namelist()]
    has = lambda suffix: any(n == suffix or n.endswith('/' + suffix) for n in names)

    if has('scripts/entities.xml') and has('content/GameParams.data'):
        version = zip_version(z, info, override)
        older = [v for v in imported_versions() if tuple(map(int, v.split('_'))) < tuple(map(int, version.split('_')))]
        args = [zpath, version]
        if not os.path.isfile(os.path.join(DATA, version, 'constants.json')):
            if not older:
                sys.exit('no older version to copy constants.json from')
            args += ['--constants-from', older[-1]]
            print(f'note: {version} is new; constants.json copied from {older[-1]} - check the roster below')
        print(f'--- game data -> {version}')
        import_gamedata.main(args)
    if any(re.search(r'(^|/)spaces/[^/]+/terrain\.bin$', n) for n in names):
        print('--- maps')
        import_spaces.main([zpath])
    n_gui = import_gui(z, names)
    if n_gui:
        print(f'--- gui: {n_gui} files -> wowsreplay/data/gui')
    if has('content/assets.bin'):
        print('--- ship models')
        if has('content/GameParams.data'):
            import_models.main([zpath, zpath])
        else:
            print('skipped: models need content/GameParams.data from the same zip')

    if pages:
        rebuild_and_check(replays)
    print('--- what is still missing')
    plan(replays)


def import_gui(z, names):
    """Copy gui/... files (ship icons, minimap icons) and gui_files.txt from an update zip to data/gui."""
    n = 0
    for raw, name in zip(z.namelist(), names):
        if name.endswith('/'):
            continue
        m = re.search(r'(?:^|/)gui/(.+)$', name)
        rel = m.group(1) if m else ('gui_files.txt' if name.endswith('gui_files.txt') else None)
        if not rel or '..' in rel.split('/'):
            continue
        out = os.path.join(DATA, 'gui', *rel.split('/'))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'wb') as f:
            f.write(z.read(raw))
        n += 1
    return n


def rebuild_and_check(replays):
    from wowsreplay import __main__ as cli
    from wowsreplay.timeline import build_timeline
    print('--- pages')
    for r in replays:
        stem = os.path.splitext(os.path.basename(r))[0]
        try:
            cli.main([r, '-o', os.path.join(ROOT, 'out', stem + '.html')])
        except (Exception, SystemExit) as e:   # one bad replay must not stop the rest
            print(f'FAILED {stem}: {e}')
            continue
        d = build_timeline(r)
        bad = [k for k in d['parseErrors'] if 'map has' in k]
        unnamed = sum(1 for s in d['ships'] if str(s['name']).startswith('#') or not isinstance(s['name'], str))
        if bad or unnamed:
            print(f'  CHECK {stem}: roster looks wrong ({unnamed} unnamed ships; {bad[:2]}) - '
                  f'the arena-state key map in constants.json probably changed for this version')


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan', help='write tools/game_update.bat for what the replays need')
    p.add_argument('replays', nargs='*')
    p.add_argument('--refresh', action='store_true', help='pull every map/ship used, not only missing ones')
    sub.add_parser('full', help='write tools/game_full.bat: every map, ship model and icon (one big pull per patch)')
    i = sub.add_parser('import', help='import wows_update.zip / wows_full.zip, rebuild pages, plan again')
    i.add_argument('zip', nargs='?', default=os.path.join(ROOT, 'wows_update.zip'))
    i.add_argument('--replays', nargs='*', default=[])
    i.add_argument('--version', help='game version of the zip, e.g. 15_11_0 (normally detected)')
    i.add_argument('--no-pages', action='store_true')
    a = ap.parse_args(argv)
    if a.cmd == 'plan':
        plan(replay_files(a.replays), a.refresh)
    elif a.cmd == 'full':
        write_full_bat()
    else:
        do_import(a.zip, replay_files(a.replays), a.version, not a.no_pages)


if __name__ == '__main__':
    main(sys.argv[1:])
