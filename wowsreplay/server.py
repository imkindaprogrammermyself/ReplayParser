"""Web app: upload .wowsreplay files, get the viewer page for each, keep a library of processed replays.

    python -m wowsreplay.server [--host 127.0.0.1] [--port 8000] [--data server_data] [--workers 2]

Standard library only. Uploads are stored under their SHA-256 (a re-upload is instant), processed on a worker
pool and served as the same self-contained page `python -m wowsreplay` writes. It listens on localhost by default;
pass --host 0.0.0.0 to share it on your network (there is no login - anyone who can reach it can upload).

  GET  /                 upload page + library
  POST /api/upload       raw file body (X-Filename header) -> {"id", "status"}
  GET  /api/jobs/<id>    {"status": queued|processing|done|error, "error"?, "meta"?}
  GET  /api/replays      library, newest battle first
  GET  /r/<id>           the viewer page
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .__main__ import DATA, THREE_JS, build_data, render_page
from .container import MAGIC

ASSET_BASE = '/assets/'                                   # game data the viewer fetches (cached by the browser)
THREE_URL = '/static/' + os.path.basename(THREE_JS)
# only the files a page references: map images / heights, ship models, icons - nothing else under data/
ASSET_RE = re.compile(r'/assets/((?:maps/[\w.-]+/(?:minimap\.png|terrain\.i16))|(?:models/[\w.-]+\.json)'
                      r'|(?:gui/[\w./-]+\.(?:png|svg)))')
PYODIDE_URL = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/'   # Python 3.12 + pycryptodome, for /app


def _cached(key, build):
    """build() once per game-data / parser change (data_stamp)."""
    stamp = data_stamp()
    hit = _CACHE.get(key)
    if not hit or hit[0] != stamp:
        hit = _CACHE[key] = (stamp, build())
    return hit[1]


_CACHE = {}


def asset_index():
    """/assets/index.json: every file /assets/ serves, plus each map's map.json - what the in-browser parser needs
    to reference maps, models and icons by URL without the files themselves."""
    def build():
        files, js = [], {}
        for d, _sub, names in os.walk(DATA):
            for n in names:
                rel = os.path.relpath(os.path.join(d, n), DATA).replace(os.sep, '/')
                if ASSET_RE.fullmatch('/assets/' + rel):
                    files.append(rel)
                elif rel.startswith('maps/') and n == 'map.json':
                    with open(os.path.join(d, n), encoding='utf-8') as f:
                        js[rel] = json.load(f)
        return json.dumps({'files': sorted(files), 'json': js}, separators=(',', ':')).encode()
    return _cached('index', build)


def parser_bundle(version):
    """/pybundle/<version>.zip: the parser package and the data it reads for that game version, for Pyodide."""
    vdir = os.path.join(DATA, version)
    if not os.path.isfile(os.path.join(vdir, 'scripts', 'entities.xml')):
        return None

    def build():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for n in sorted(os.listdir(PKG_DIR)):
                if n.endswith('.py') and n != 'server.py':
                    z.write(os.path.join(PKG_DIR, n), f'wowsreplay/{n}')
            for n in ('consumables.json', 'drops.json', 'aircraft.json'):
                if os.path.isfile(os.path.join(DATA, n)):
                    z.write(os.path.join(DATA, n), f'wowsreplay/data/{n}')
            for d, _sub, names in os.walk(vdir):
                for n in names:
                    full = os.path.join(d, n)
                    rel = os.path.relpath(full, vdir).replace(os.sep, '/')
                    if rel.startswith('scripts/') or rel in ('constants.json', 'ships.json'):
                        z.write(full, f'wowsreplay/data/{version}/{rel}')
        return buf.getvalue()
    return _cached('bundle:' + version, build)


def app_page():
    """/app: the viewer with no battle in it; dropping a .wowsreplay parses it in the browser (nothing uploaded)."""
    def build():
        page = render_page(None, three_url=THREE_URL)
        cfg = json.dumps({'pyodide': PYODIDE_URL, 'bundle': '/pybundle/', 'assets': ASSET_BASE})
        return page.replace('<head>', f'<head>\n<script>window.WOWS_LOCAL = {cfg};</script>', 1).encode()
    return _cached('app', build)


TYPES = {'.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json', '.i16': 'application/octet-stream',
         '.js': 'text/javascript; charset=utf-8'}

MAX_UPLOAD = 50 << 20                 # real replays are a few MB
ID_RE = re.compile(r'^[0-9a-f]{64}$')


PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_stamp = [0.0, 0.0]          # [checked at, value]


def data_stamp():
    """Newest mtime of what a page is built from: imported game data (maps, models, icons, versions), the parser and
    the viewer. A page older than this is rebuilt - e.g. after tools/update.py import adds the map it was missing."""
    now = time.time()
    if now - _stamp[0] < 10:
        return _stamp[1]
    newest = 0.0
    roots = [os.path.join(PKG_DIR, 'data')]
    files = [os.path.join(PKG_DIR, '..', 'viewer', 'viewer.html')] +         [os.path.join(PKG_DIR, f) for f in os.listdir(PKG_DIR) if f.endswith('.py') and f != 'server.py']
    for root in roots:
        for d, _sub, names in os.walk(root):
            newest = max([newest, os.path.getmtime(d)] + [os.path.getmtime(os.path.join(d, n)) for n in names])
    for f in files:
        if os.path.isfile(f):
            newest = max(newest, os.path.getmtime(f))
    _stamp[:] = [now, newest]
    return newest


class Library:
    """Replays on disk: <data>/replays/<id>.wowsreplay, <data>/pages/<id>.html, <data>/index.json."""

    def __init__(self, root, workers=2):
        self.root = root
        for d in ('replays', 'pages'):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.index_path = os.path.join(root, 'index.json')
        self.lock = threading.Lock()
        self.jobs = {}                # id -> {"status", "error"?}
        try:
            with open(self.index_path, encoding='utf-8') as f:
                self.index = json.load(f)
        except FileNotFoundError:
            self.index = {}
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='replay')
        self.futures = {}
        stale = [rid for rid in self.index if self.stale(rid)]
        if stale:
            print(f'rebuilding {len(stale)} page(s) made before the latest game data / viewer update')
        for rid in stale:
            self._queue(rid, self.index[rid].get('file') or 'replay.wowsreplay', rebuild=True)

    def stale(self, rid):
        e = self.index.get(rid)
        return bool(e) and os.path.isfile(self.path('replays', rid)) and (e.get('stamp') or 0) < data_stamp()

    def _queue(self, rid, filename, rebuild=False):
        """Start (or join) the build of rid; returns its future."""
        with self.lock:
            if self.jobs.get(rid, {}).get('status') in ('queued', 'processing') and rid in self.futures:
                return self.futures[rid]
            self.jobs[rid] = {'status': 'queued', 'filename': filename}
            fut = self.futures[rid] = self.pool.submit(self._process, rid, filename, rebuild)
            return fut

    def ensure_fresh(self, rid, timeout=180):
        """Rebuild rid's page first if it predates the current game data / viewer (the request waits for it)."""
        if self.stale(rid):
            try:
                self._queue(rid, self.index[rid].get('file') or 'replay.wowsreplay', rebuild=True).result(timeout)
            except Exception:
                pass                  # keep serving the old page; the error is in the job status / log

    def path(self, kind, rid):
        return os.path.join(self.root, kind, rid + ('.html' if kind == 'pages' else '.wowsreplay'))

    def _save_index(self):
        tmp = self.index_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, ensure_ascii=False, indent=0)
        os.replace(tmp, self.index_path)

    def submit(self, body: bytes, filename: str):
        rid = hashlib.sha256(body).hexdigest()
        with self.lock:
            if rid in self.index and os.path.isfile(self.path('pages', rid)):
                return rid, 'done'
            if self.jobs.get(rid, {}).get('status') in ('queued', 'processing'):
                return rid, self.jobs[rid]['status']
            with open(self.path('replays', rid), 'wb') as f:
                f.write(body)
        self._queue(rid, filename)
        return rid, 'queued'

    def _process(self, rid, filename, rebuild=False):
        with self.lock:
            self.jobs[rid]['status'] = 'processing'
        t0, stamp = time.time(), data_stamp()
        try:
            data = build_data(self.path('replays', rid), asset_base=ASSET_BASE)   # files by URL, not embedded
            page = render_page(data, three_url=THREE_URL)
            tmp = self.path('pages', rid) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(page)
            os.replace(tmp, self.path('pages', rid))
            m, own = data['meta'], next((s for s in data['ships'] if s['relation'] == 'self'), {})
            res = m.get('result') or {}
            entry = {
                'id': rid, 'file': filename, 'map': m.get('map'), 'gameType': m.get('gameType'),
                'scenario': m.get('scenario'), 'dateTime': m.get('dateTime'), 'version': m.get('version'),
                'duration': round(m.get('end') or 0), 'player': own.get('name'), 'ship': own.get('ship'),
                'result': None if not res else 'win' if res.get('winnerTeamId') == m.get('ownTeam')
                else 'draw' if (res.get('winnerTeamId') or 0) < 0 else 'loss',
                'processed': time.strftime('%Y-%m-%d %H:%M:%S'), 'seconds': round(time.time() - t0, 1), 'stamp': stamp,
                'warnings': warnings_for(data),
                'stats': (data.get('analytics') or {}).get('personal'),   # the Trends page
            }
            with self.lock:
                self.index[rid] = entry
                self._save_index()
                self.jobs[rid] = {'status': 'done'}
        except Exception as e:          # one bad replay must not take the server down
            traceback.print_exc()
            msg = str(e)
            if 'no data for game version' in msg:
                msg += ' - replays from a game version need that version imported (tools/update.py)'
            with self.lock:
                self.jobs[rid] = {'status': 'error', 'error': msg}
            if not rebuild:           # a failed rebuild keeps the replay and its previous page
                try:
                    os.remove(self.path('replays', rid))
                except OSError:
                    pass

    def rebuild(self, rid):
        if rid not in self.index or not os.path.isfile(self.path('replays', rid)):
            return None
        self._queue(rid, self.index[rid].get('file') or 'replay.wowsreplay', rebuild=True)
        return 'queued'

    def delete(self, rid):
        """Remove a replay, its page and its library entry; False while it is being built."""
        with self.lock:
            if self.jobs.get(rid, {}).get('status') in ('queued', 'processing'):
                return False
            self.index.pop(rid, None)
            self.jobs.pop(rid, None)
            self.futures.pop(rid, None)
            self._save_index()
        for kind in ('replays', 'pages'):
            try:
                os.remove(self.path(kind, rid))
            except OSError:
                pass
        return True

    def status(self, rid):
        with self.lock:
            if rid in self.jobs:
                out = dict(self.jobs[rid])
            elif rid in self.index:
                out = {'status': 'done'}
            else:
                return None
            if out['status'] == 'done' and rid in self.index:
                out['meta'] = self.index[rid]
            return out

    def fill_stats(self):
        """Rebuild, in the background, replays processed before battle analytics (no 'stats' for the Trends page)."""
        with self.lock:
            todo = [rid for rid, e in self.index.items() if 'stats' not in e and os.path.isfile(self.path('replays', rid))
                    and self.jobs.get(rid, {}).get('status') not in ('queued', 'processing', 'error')]
        for rid in todo:
            self.rebuild(rid)
        return len(todo)

    def listing(self):
        with self.lock:
            return sorted(self.index.values(), key=lambda e: _battle_time(e.get('dateTime')), reverse=True)


def warnings_for(data):
    """What is missing from a built page: the map, ship models, packets the parser skipped."""
    out = [] if data['map'] else ['map not imported: no minimap or terrain']
    types = {s['ship'] for s in data['ships'] if s.get('ship')}
    missing = sorted(types - set(data.get('modelUrls') or data['models']))
    if missing:
        out.append(f'{len(missing)} of {len(types)} ship types without a 3D model: {", ".join(missing[:4])}'
                   + (' ...' if len(missing) > 4 else ''))
    if data.get('parseErrors'):
        out.append(f'{sum(data["parseErrors"].values())} packets could not be read')
    return out


def _battle_time(s):
    """'28.10.2026 23:52:25' -> sortable '2026-10-28 23:52:25'."""
    m = re.match(r'(\d+)\.(\d+)\.(\d+) (\S+)', s or '')
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)} {m.group(4)}' if m else ''


class Handler(BaseHTTPRequestHandler):
    server_version = 'wowsreplay'
    library: Library = None

    def log_message(self, fmt, *args):
        sys.stderr.write(f'{self.address_string()} - {fmt % args}\n')

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8')
        extra = dict(extra or {})
        if len(body) > 1024 and re.match(r'(text/|application/json|image/svg)', ctype)                 and 'gzip' in self.headers.get('Accept-Encoding', ''):
            body = gzip.compress(body, 5)
            extra['Content-Encoding'] = 'gzip'
            extra['Vary'] = 'Accept-Encoding'
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _error(self, code, msg):
        self._send(code, {'error': msg})

    def _file(self, path, ctype, cache='no-cache'):
        """A file with an ETag: a browser that has it gets 304 (no-cache = always ask, so imports show at once)."""
        try:
            st = os.stat(path)
        except OSError:
            return self._error(404, 'not found')
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', cache)
            self.end_headers()
            return
        with open(path, 'rb') as f:
            body = f.read()
        self._send(200, body, ctype, {'ETag': etag, 'Cache-Control': cache})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/':
            return self._send(200, INDEX_HTML, 'text/html; charset=utf-8')
        if path == '/trends':
            self.library.fill_stats()
            return self._send(200, TRENDS_HTML, 'text/html; charset=utf-8')
        if path == '/api/replays':
            return self._send(200, self.library.listing())
        m = re.fullmatch(r'/api/jobs/([0-9a-f]{64})', path)
        if m:
            st = self.library.status(m.group(1))
            return self._send(200, st) if st else self._error(404, 'unknown replay')
        m = re.fullmatch(r'/r/([0-9a-f]{64})', path)
        if m:
            self.library.ensure_fresh(m.group(1))
            p = self.library.path('pages', m.group(1))
            if not os.path.isfile(p):
                return self._error(404, 'no page for that replay (yet)')
            return self._file(p, 'text/html; charset=utf-8', 'private, no-cache')
        if path == THREE_URL:
            return self._file(THREE_JS, TYPES['.js'], 'public, max-age=86400')
        if path == '/app':
            return self._send(200, app_page(), 'text/html; charset=utf-8', {'Cache-Control': 'no-cache'})
        if path == '/assets/index.json':
            return self._send(200, asset_index(), 'application/json', {'Cache-Control': 'no-cache'})
        m = re.fullmatch(r'/pybundle/(\d+_\d+_\d+)\.zip', path)
        if m:
            body = parser_bundle(m.group(1))
            if body is None:
                return self._error(404, f'game version {m.group(1)} is not imported on this server')
            return self._send(200, body, 'application/zip', {'Cache-Control': 'no-cache'})
        m = ASSET_RE.fullmatch(path)
        if m and '..' not in m.group(1).split('/'):
            full = os.path.realpath(os.path.join(DATA, *m.group(1).split('/')))
            if full.startswith(os.path.realpath(DATA) + os.sep):
                return self._file(full, TYPES.get(os.path.splitext(full)[1], 'application/octet-stream'))
        self._error(404, 'not found')

    def do_DELETE(self):   # DELETE can't come from another site's page without a CORS preflight, which we don't answer
        m = re.fullmatch(r'/api/replays/([0-9a-f]{64})', self.path.split('?', 1)[0])
        if not m:
            return self._error(404, 'not found')
        if m.group(1) not in self.library.index:
            return self._error(404, 'unknown replay')
        if not self.library.delete(m.group(1)):
            return self._error(409, 'still processing - try again in a moment')
        self._send(200, {'deleted': m.group(1)})

    def do_POST(self):
        path = self.path.split('?', 1)[0]
        m = re.fullmatch(r'/api/replays/([0-9a-f]{64})/rebuild', path)
        if m:
            st = self.library.rebuild(m.group(1))
            return self._send(202, {'id': m.group(1), 'status': st}) if st else self._error(404, 'unknown replay')
        if path != '/api/upload':
            return self._error(404, 'not found')
        try:
            size = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return self._error(411, 'Content-Length required')
        if size <= 0 or size > MAX_UPLOAD:
            return self._error(413, f'upload must be 1 byte to {MAX_UPLOAD >> 20} MB')
        body = self.rfile.read(size)
        if len(body) != size:
            return self._error(400, 'upload was cut short')
        if body[:4] != MAGIC:
            return self._error(415, 'not a .wowsreplay file')
        raw = unquote(self.headers.get('X-Filename') or 'replay.wowsreplay')   # the page URL-encodes it
        name = os.path.basename(raw.replace('\\', '/'))[:200] or 'replay.wowsreplay'
        rid, status = self.library.submit(body, name)
        self._send(202 if status != 'done' else 200, {'id': rid, 'status': status})


INDEX_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Replay Library</title>
<style>
  :root { --bg: #0b1220; --panel: #111a2e; --panel-2: #1a2540; --line: #24314f; --text: #dbe4f3; --muted: #8394b4;
          --accent: #60a5fa; --win: #4ade80; --loss: #f87171; --warn: #fbbf24; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, sans-serif; }
  main { max-width: 1000px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 18px; }
  .sub a { color: var(--accent); }
  .h1link { font-size: 13px; font-weight: 500; color: var(--accent); margin-left: 10px; text-decoration: none; }
  #drop { display: block; border: 2px dashed var(--line); border-radius: 10px; padding: 28px 16px; text-align: center; background: var(--panel);
          cursor: pointer; transition: border-color .15s, background .15s; }
  #drop.over { border-color: var(--accent); background: var(--panel-2); }
  #drop b { color: var(--accent); }
  #drop input { display: none; }
  #jobs { margin: 12px 0 0; display: grid; gap: 6px; }
  .job { display: flex; gap: 10px; align-items: center; padding: 8px 10px; background: var(--panel); border: 1px solid var(--line);
         border-radius: 8px; }
  .job .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .job .st { color: var(--muted); white-space: nowrap; }
  .job.error .st { color: var(--loss); white-space: normal; }
  .job a, td a { color: var(--accent); text-decoration: none; }
  .spin { width: 14px; height: 14px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%;
          animation: spin .8s linear infinite; flex: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
  h2 { font-size: 15px; margin: 28px 0 8px; display: flex; justify-content: space-between; align-items: baseline; }
  h2 input { background: var(--panel); border: 1px solid var(--line); color: var(--text); border-radius: 6px; padding: 5px 8px;
             font: inherit; font-weight: 400; width: min(260px, 50vw); }
  .table { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; background: var(--panel); }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--panel); }
  .win { color: var(--win); } .loss { color: var(--loss); } .muted { color: var(--muted); } .warn { color: var(--warn); }
  .empty { padding: 18px; color: var(--muted); text-align: center; }
  td.acts { text-align: right; }
  td.acts button { background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 3px 8px;
    font: inherit; font-size: 12px; cursor: pointer; }
  td.acts button:hover { border-color: var(--accent); }
  td.acts button[data-act=delete]:hover { border-color: var(--loss); color: var(--loss); }
</style></head>
<body><main>
  <h1>Replay Library <a class="h1link" href="/trends">Trends</a></h1>
  <p class="sub">Drop World of Warships replays to turn them into the 2D / 3D viewer. Processed replays stay listed below.
    Or <a href="/app">open one in your browser only</a>: parsed on your machine, not uploaded or listed.</p>
  <label id="drop"><input type="file" id="file" accept=".wowsreplay" multiple>
    Drop <b>.wowsreplay</b> files here, or click to choose</label>
  <div id="jobs"></div>
  <h2>Replays <input id="q" type="search" placeholder="Filter by map, player, ship"></h2>
  <div class="table"><table><thead><tr><th>Battle</th><th>Map</th><th>Mode</th><th>Player</th><th>Ship</th><th>Result</th>
    <th>Length</th><th>Version</th><th></th></tr></thead><tbody id="list"><tr><td colspan="9" class="empty">Loading…</td></tr></tbody></table></div>
</main>
<script>
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const ship = s => String(s || '').replace(/^P[A-Z]{3}\d+_/, '').replace(/_/g, ' ');
const mmss = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
let rows = [];
async function loadList() {
  try { rows = await (await fetch('/api/replays')).json(); } catch { rows = []; }
  render();
}
function render() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const shown = rows.filter(r => !q || [r.map, r.player, ship(r.ship), r.gameType, r.file].join(' ').toLowerCase().includes(q));
  document.getElementById('list').innerHTML = shown.length ? shown.map(r => `<tr>
    <td><a href="/r/${r.id}" target="_blank" rel="noopener">${esc(r.dateTime || r.file)}</a></td>
    <td>${esc(r.map)}${r.warnings && r.warnings.length ? ` <span class="warn" title="${esc(r.warnings.join('; '))}">⚠</span>` : ''}</td>
    <td>${esc(r.gameType)}</td><td>${esc(r.player)}</td><td>${esc(ship(r.ship))}</td>
    <td class="${r.result || 'muted'}">${esc(r.result || '–')}</td><td>${mmss(r.duration || 0)}</td>
    <td class="muted">${esc(r.version)}</td>
    <td class="acts"><button data-act="rebuild" data-id="${r.id}" title="Build the page again with the current game data">Rebuild</button>
      <button data-act="delete" data-id="${r.id}" title="Remove from the library">Delete</button></td></tr>`).join('')
    : `<tr><td colspan="9" class="empty">${rows.length ? 'No replay matches the filter.' : 'No replays yet - drop one above.'}</td></tr>`;
}
document.getElementById('q').addEventListener('input', render);
document.getElementById('list').addEventListener('click', async e => {
  const b = e.target.closest('button[data-act]');
  if (!b) return;
  const r = rows.find(x => x.id === b.dataset.id);
  if (b.dataset.act === 'delete') {
    if (!confirm(`Remove ${r ? (r.map + ' · ' + (r.player || '') + ' · ' + (r.dateTime || '')) : 'this replay'} from the library?`)) return;
    const res = await fetch('/api/replays/' + b.dataset.id, { method: 'DELETE' });
    if (!res.ok) alert((await res.json()).error || res.statusText);
    return loadList();
  }
  b.disabled = true; b.textContent = 'Rebuilding…';
  await fetch(`/api/replays/${b.dataset.id}/rebuild`, { method: 'POST' });
  for (;;) {
    const st = await (await fetch('/api/jobs/' + b.dataset.id)).json();
    if (st.status === 'done' || st.status === 'error') break;
    await new Promise(r => setTimeout(r, 700));
  }
  loadList();
});

function jobRow(name) {
  const el = document.createElement('div');
  el.className = 'job';
  el.innerHTML = `<div class="spin"></div><div class="name"></div><div class="st">uploading…</div>`;
  el.querySelector('.name').textContent = name;
  document.getElementById('jobs').prepend(el);
  return el;
}
function setJob(el, state, html) {
  el.className = 'job' + (state === 'error' ? ' error' : '');
  if (state !== 'busy') el.querySelector('.spin')?.remove();
  el.querySelector('.st').innerHTML = html;
}
async function upload(file) {
  const el = jobRow(file.name);
  if (!/\.wowsreplay$/i.test(file.name)) return setJob(el, 'error', 'not a .wowsreplay file');
  let res;
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: file, headers: { 'X-Filename': encodeURIComponent(file.name) } });
    res = await r.json();
    if (!r.ok) return setJob(el, 'error', esc(res.error || r.statusText));
  } catch (e) { return setJob(el, 'error', 'upload failed: ' + esc(e.message)); }
  setJob(el, 'busy', 'processing…');
  for (;;) {
    const st = await (await fetch('/api/jobs/' + res.id)).json();
    if (st.status === 'done') {
      setJob(el, 'done', `<a href="/r/${res.id}" target="_blank" rel="noopener">open viewer ↗</a>`);
      return loadList();
    }
    if (st.status === 'error') return setJob(el, 'error', esc(st.error));
    setJob(el, 'busy', st.status + '…');
    await new Promise(r => setTimeout(r, 800));
  }
}
const drop = document.getElementById('drop');
document.getElementById('file').addEventListener('change', e => { [...e.target.files].forEach(upload); e.target.value = ''; });
for (const ev of ['dragenter', 'dragover']) drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); });
for (const ev of ['dragleave', 'drop']) drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); });
drop.addEventListener('drop', e => [...e.dataTransfer.files].forEach(upload));
loadList();
</script></body></html>
'''

TRENDS_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Replay Trends</title>
<style>
  :root { --bg: #0b1220; --panel: #111a2e; --panel-2: #1a2540; --line: #24314f; --text: #dbe4f3; --muted: #8394b4;
          --accent: #60a5fa; --win: #4ade80; --loss: #f87171; --draw: #94a3b8; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, sans-serif; }
  main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 20px; margin: 0 0 4px; } h1 a { font-size: 13px; font-weight: 500; color: var(--accent); margin-left: 10px; text-decoration: none; }
  h2 { font-size: 15px; margin: 26px 0 8px; display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
  .sub, .note, .muted { color: var(--muted); } .sub { margin: 0 0 14px; } .note { font-size: 12px; margin-top: 6px; }
  .bar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
  select { background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; font: inherit; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
  .tile { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; }
  .tile .l, .tile .s { color: var(--muted); font-size: 12px; } .tile .v { font-size: 22px; font-weight: 600; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; position: relative; }
  .table { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; background: var(--panel); }
  tr:last-child td { border-bottom: none; }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; color: var(--muted); font-size: 12px; font-weight: 400; }
  .legend b { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; vertical-align: -1px; }
  .legend b.ln { border-radius: 1px; height: 3px; width: 14px; vertical-align: 3px; }
  svg { width: 100%; height: auto; display: block; }
  svg .grid { stroke: var(--line); } svg .ax { fill: var(--muted); font-size: 11px; }
  svg circle { cursor: pointer; stroke: var(--panel); stroke-width: 2; }
  .tip { position: absolute; background: rgba(11, 18, 32, .96); border: 1px solid var(--line); border-radius: 6px; padding: 5px 9px;
         font-size: 12px; pointer-events: none; white-space: nowrap; }
  .two { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
  .map { position: relative; width: 100%; max-width: 520px; aspect-ratio: 1; background: #0e2238 center / cover; border-radius: 6px; }
  .map i { position: absolute; width: 10px; height: 10px; margin: -5px 0 0 -5px; border-radius: 50%; background: var(--loss);
           border: 2px solid #0b1220; cursor: pointer; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; } .kv span { color: var(--muted); }
  .empty { padding: 30px; text-align: center; color: var(--muted); }
</style></head>
<body><main>
  <h1>Trends <a href="/">Library</a></h1>
  <p class="sub">Across the replays in the library, from the recording player's point of view.
    Accuracy is hits / shells fired (a lower bound: not every landing is reported).</p>
  <div class="bar"><label>Player <select id="player"></select></label><label>Mode <select id="mode"></select></label>
    <span id="pending" class="muted"></span></div>
  <div id="out"><div class="empty">Loading...</div></div>
</main>
<script>
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const ship = s => String(s || '').replace(/^P[A-Z]{3}\d+_/, '').replace(/_/g, ' ');
const num = v => Math.round(v || 0).toLocaleString();
const pct = (a, b) => b ? Math.round(100 * a / b) + '%' : '-';
const mmss = s => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
const when = s => { const m = /(\d+)\.(\d+)\.(\d+) (\S+)/.exec(s || ''); return m ? `${m[3]}-${m[2]}-${m[1]} ${m[4]}` : ''; };
const avg = (list, f) => { const v = list.map(f).filter(x => x != null && !Number.isNaN(x)); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
const cause = c => ({ AP_SHELL: 'AP shell', HE_SHELL: 'HE shell', CS_SHELL: 'SAP shell', BURNING: 'fire', FLOOD: 'flooding' })[c] || String(c || '?').toLowerCase().replace(/_/g, ' ');
const COL = { 1: 'var(--win)', 0: 'var(--loss)' };
let all = [], index = null;

async function load() {
  try { all = await (await fetch('/api/replays')).json(); } catch { all = []; }
  const missing = all.filter(e => !e.stats).length;
  document.getElementById('pending').textContent = missing ? `${missing} replay(s) still being analysed - this page updates itself` : '';
  if (missing) setTimeout(load, 4000);
  const have = all.filter(e => e.stats);
  fillSelect('player', have.map(e => e.player), 'All players', true);
  fillSelect('mode', have.map(e => e.gameType), 'All modes');
  render();
}
function fillSelect(id, values, allLabel, allLast) {
  const el = document.getElementById(id), keep = el.dataset.set ? el.value : null, counts = {};
  for (const v of values) if (v) counts[v] = (counts[v] || 0) + 1;
  const opts = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const all = allLabel ? `<option value="">${allLabel} (${values.length})</option>` : '';
  const list = opts.map(([v, n]) => `<option value="${esc(v)}">${esc(v)} (${n})</option>`).join('');
  el.innerHTML = allLast ? list + all : all + list;   // players: the most frequent one first (you, usually)
  if (keep !== null && (keep === '' || counts[keep])) el.value = keep;
  el.dataset.set = '1';
}
document.getElementById('player').onchange = render;
document.getElementById('mode').onchange = render;

function render() {
  const player = document.getElementById('player').value, mode = document.getElementById('mode').value;
  const B = all.filter(e => e.stats && (!player || e.player === player) && (!mode || e.gameType === mode))
    .sort((a, b) => when(a.dateTime).localeCompare(when(b.dateTime)));
  const out = document.getElementById('out');
  if (!B.length) { out.innerHTML = '<div class="empty">No analysed replays yet. Upload some in the <a href="/" style="color:var(--accent)">library</a>.</div>'; return; }
  const S = B.map(e => e.stats), decided = S.filter(s => s.win === 1 || s.win === 0);
  const shells = S.reduce((a, s) => a + s.shells, 0), hits = S.reduce((a, s) => a + s.hits, 0);
  const torps = S.reduce((a, s) => a + s.torps, 0), thits = S.reduce((a, s) => a + s.torpHits, 0);
  const tile = (l, v, s) => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`;
  let h = `<div class="tiles">
    ${tile('Battles', B.length, `${decided.filter(s => s.win === 1).length} won, ${decided.filter(s => s.win === 0).length} lost`)}
    ${tile('Win rate', pct(decided.filter(s => s.win === 1).length, decided.length))}
    ${tile('Average damage', num(avg(S, s => s.dealt)), `taken ${num(avg(S, s => s.taken))}`)}
    ${tile('Accuracy', pct(hits, shells), `${num(hits)} hits of ${num(shells)} shells`)}
    ${tile('Kills per battle', (avg(S, s => s.kills) || 0).toFixed(2))}
    ${tile('Survived', pct(S.filter(s => s.survived).length, S.length))}
    ${torps ? tile('Torpedo hits', pct(thits, torps), `${thits} of ${torps}`) : ''}
  </div>`;
  h += `<h2>Damage per battle <span class="legend"><span><b style="background:var(--win)"></b>win</span><span><b style="background:var(--loss)"></b>loss</span><span><b style="background:var(--draw)"></b>draw / unknown</span><span><b class="ln" style="background:var(--accent)"></b>average of the last 5</span></span></h2>
    <div class="panel">${damageChart(B)}<div class="tip" hidden></div></div>`;
  h += `<h2>By ship</h2>${groupTable(B, e => ship(e.stats.ship), 'Ship')}`;
  h += `<h2>By map</h2>${groupTable(B, e => e.map, 'Map', true)}`;
  h += `<h2>Where you sank <select id="dmap"></select></h2><div class="two"><div class="map" id="dmapimg"></div><div id="dmapinfo"></div></div>`;
  h += `<h2>Playstyle</h2><div class="panel">${playstyle(S)}</div>`;
  out.innerHTML = h;
  wireChart(out.querySelector('svg.dmg'), B);
  const maps = [...new Set(B.filter(e => e.stats.death && e.stats.death.x != null).map(e => e.map))];
  const dm = document.getElementById('dmap');
  if (!maps.length) {
    dm.hidden = true;
    dm.closest('h2').nextElementSibling.outerHTML = '<div class="panel muted">No sinkings with a known position in these battles.</div>';
    return;
  }
  dm.innerHTML = maps.map(m => `<option>${esc(m)}</option>`).join('');
  dm.onchange = () => deathMap(B, dm.value);
  deathMap(B, dm.value);
}

function damageChart(B) {
  const W = 1000, H = 230, L = 52, R = 12, T = 10, Bm = 26, n = B.length;
  const max = Math.max(...B.map(e => e.stats.dealt), 1) * 1.08;
  const X = i => n === 1 ? (L + W - R) / 2 : L + (W - L - R) * i / (n - 1), Y = v => T + (H - T - Bm) * (1 - v / max);
  let g = '';
  for (let k = 0; k <= 4; k++) { const v = max * k / 4; g += `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="ax" x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${v >= 1000 ? Math.round(v / 1000) + 'k' : Math.round(v)}</text>`; }
  const every = Math.max(1, Math.ceil(n / 8));
  B.forEach((e, i) => { if (i % every === 0 || i === n - 1) g += `<text class="ax" x="${X(i)}" y="${H - 8}" text-anchor="${n > 1 && i === 0 ? 'start' : n > 1 && i === n - 1 ? 'end' : 'middle'}">${esc(when(e.dateTime).slice(5, 10))}</text>`; });
  const roll = B.map((_, i) => { const w = B.slice(Math.max(0, i - 4), i + 1); return w.reduce((a, e) => a + e.stats.dealt, 0) / w.length; });
  const line = n > 1 ? `<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="${roll.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')}"/>` : '';
  const dots = B.map((e, i) => `<circle data-i="${i}" cx="${X(i).toFixed(1)}" cy="${Y(e.stats.dealt).toFixed(1)}" r="5" fill="${COL[e.stats.win] || 'var(--draw)'}"/>`).join('');
  return `<svg class="dmg" viewBox="0 0 ${W} ${H}">${g}${line}${dots}</svg>`;
}
function wireChart(svg, B) {
  const tip = svg.parentNode.querySelector('.tip');
  svg.querySelectorAll('circle').forEach(c => {
    const e = B[c.dataset.i], s = e.stats;
    c.onmouseenter = () => {
      tip.hidden = false;
      tip.innerHTML = `<b>${num(s.dealt)}</b> damage · ${s.kills} kills · ${esc(ship(s.ship))} on ${esc(e.map)}<br><span class="muted">${esc(e.dateTime)} · ${s.win === 1 ? 'win' : s.win === 0 ? 'loss' : 'draw'} · click to open</span>`;
      const r = svg.getBoundingClientRect(), b = c.getBoundingClientRect();
      tip.style.left = `${Math.min(r.width - 300, Math.max(0, b.left - r.left + 12))}px`; tip.style.top = `${b.top - r.top - 8}px`;
    };
    c.onmouseleave = () => { tip.hidden = true; };
    c.onclick = () => { location.href = '/r/' + e.id; };
  });
}
function groupTable(B, key, label, withDeath) {
  const groups = {};
  for (const e of B) (groups[key(e)] = groups[key(e)] || []).push(e);
  const rows = Object.entries(groups).sort((a, b) => b[1].length - a[1].length).map(([k, list]) => {
    const S = list.map(e => e.stats), dec = S.filter(s => s.win === 1 || s.win === 0);
    const sh = S.reduce((a, s) => a + s.shells, 0), hi = S.reduce((a, s) => a + s.hits, 0), dead = S.filter(s => s.death);
    return `<tr><td>${esc(k)}</td><td>${list.length}</td><td>${pct(dec.filter(s => s.win === 1).length, dec.length)}</td><td>${num(avg(S, s => s.dealt))}</td>
      <td>${(avg(S, s => s.kills) || 0).toFixed(1)}</td><td>${pct(hi, sh)}</td><td>${pct(S.filter(s => s.survived).length, S.length)}</td>
      ${withDeath ? `<td>${dead.length ? mmss(avg(dead, s => s.death.t)) : '-'}</td>` : ''}</tr>`;
  }).join('');
  return `<div class="table"><table><thead><tr><th>${label}</th><th>Battles</th><th>Win rate</th><th>Avg damage</th><th>Kills</th><th>Accuracy</th><th>Survived</th>${withDeath ? '<th>Avg time sunk</th>' : ''}</tr></thead><tbody>${rows}</tbody></table></div>`;
}
async function deathMap(B, map) {
  const box = document.getElementById('dmapimg'), info = document.getElementById('dmapinfo');
  if (!map) { box.innerHTML = ''; info.innerHTML = ''; return; }
  if (!index) { try { index = await (await fetch('/assets/index.json')).json(); } catch { index = { json: {} }; } }
  const mj = (index.json || {})[`maps/${map}/map.json`], bounds = mj && mj.minimap;
  const games = B.filter(e => e.map === map), dead = games.filter(e => e.stats.death && e.stats.death.x != null);
  box.style.backgroundImage = bounds ? `url('/assets/maps/${encodeURIComponent(map)}/minimap.png')` : 'none';
  box.innerHTML = bounds ? dead.map(e => {
    const d = e.stats.death, [x0, z0, x1, z1] = bounds;
    return `<a href="/r/${e.id}" title="${esc(e.dateTime)}: sunk at ${mmss(d.t)} (${esc(cause(d.cause))}), ${esc(ship(e.stats.ship))}"><i style="left:${100 * (d.x - x0) / (x1 - x0)}%;top:${100 * (z1 - d.z) / (z1 - z0)}%"></i></a>`;
  }).join('') : '<div class="empty">This map is not imported on the server.</div>';
  const causes = {};
  for (const e of dead) { const c = cause(e.stats.death.cause); causes[c] = (causes[c] || 0) + 1; }
  info.innerHTML = `<div class="kv"><span>Battles here</span><div>${games.length}</div><span>Sunk</span><div>${dead.length} (${pct(dead.length, games.length)})</div>
    <span>Average time sunk</span><div>${dead.length ? mmss(avg(dead, e => e.stats.death.t)) : '-'}</div>
    <span>Causes</span><div>${Object.entries(causes).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${esc(c)} ${n}`).join(', ') || '-'}</div></div>
    <div class="note">Each dot is where you sank; hover for when and how, click to open that battle.</div>`;
}
function playstyle(S) {
  const dead = S.filter(s => s.death), f = (v, d = 1, u = '') => v == null ? '-' : v.toFixed(d) + u;
  return `<div class="kv">
    <span>Distance to the nearest enemy</span><div>${f(avg(S, s => s.nearestEnemy), 1, ' km')} on average</div>
    <span>Distance from your team's centre</span><div>${f(avg(S, s => s.teamCentre), 1, ' km')} on average</div>
    <span>Time spotted</span><div>${f(100 * (avg(S, s => s.spotted) || 0), 0, '%')} of your time alive</div>
    <span>Hits taken while broadside</span><div>${f(100 * (avg(S, s => s.broadside) || 0), 0, '%')}</div>
    <span>First of your team to sink</span><div>${pct(dead.filter(s => s.death.order === 1).length, S.length)} of battles</div>
    <span>Time alive</span><div>${f(100 * (avg(S, s => s.end ? s.alive / s.end : null) || 0), 0, '%')} of the battle on average</div>
  </div>`;
}
load();
</script></body></html>
'''


def make_server(host='127.0.0.1', port=8000, data='server_data', workers=2):
    handler = type('BoundHandler', (Handler,), {'library': Library(data, workers)})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m wowsreplay.server', description='Replay upload server + viewer library')
    ap.add_argument('--host', default='127.0.0.1', help='127.0.0.1 = this PC only (default); 0.0.0.0 = your network')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--data', default='server_data', help='where uploads, pages and the index are kept')
    ap.add_argument('--workers', type=int, default=2, help='replays processed at once')
    a = ap.parse_args(argv)
    srv = make_server(a.host, a.port, a.data, a.workers)
    print(f'serving http://{"localhost" if a.host in ("127.0.0.1", "0.0.0.0") else a.host}:{a.port}/  (data: {os.path.abspath(a.data)})')
    if a.host not in ('127.0.0.1', 'localhost', '::1'):
        print('note: listening beyond this PC - there is no login, anyone who can reach the port can upload')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
