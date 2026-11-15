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
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .__main__ import build_data, render_page
from .container import MAGIC

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
            data = build_data(self.path('replays', rid))
            page = render_page(data)
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
                'warnings': [] if data['map'] else ['map not imported: no minimap or terrain'],
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

    def listing(self):
        with self.lock:
            return sorted(self.index.values(), key=lambda e: _battle_time(e.get('dateTime')), reverse=True)


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
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _error(self, code, msg):
        self._send(code, {'error': msg})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/':
            return self._send(200, INDEX_HTML, 'text/html; charset=utf-8')
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
            with open(p, 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8', {'Cache-Control': 'private, max-age=3600'})
        self._error(404, 'not found')

    def do_POST(self):
        if self.path.split('?', 1)[0] != '/api/upload':
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
</style></head>
<body><main>
  <h1>Replay Library</h1>
  <p class="sub">Drop World of Warships replays to turn them into the 2D / 3D viewer. Processed replays stay listed below.</p>
  <label id="drop"><input type="file" id="file" accept=".wowsreplay" multiple>
    Drop <b>.wowsreplay</b> files here, or click to choose</label>
  <div id="jobs"></div>
  <h2>Replays <input id="q" type="search" placeholder="Filter by map, player, ship"></h2>
  <div class="table"><table><thead><tr><th>Battle</th><th>Map</th><th>Mode</th><th>Player</th><th>Ship</th><th>Result</th>
    <th>Length</th><th>Version</th></tr></thead><tbody id="list"><tr><td colspan="8" class="empty">Loading…</td></tr></tbody></table></div>
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
    <td class="muted">${esc(r.version)}</td></tr>`).join('')
    : `<tr><td colspan="8" class="empty">${rows.length ? 'No replay matches the filter.' : 'No replays yet - drop one above.'}</td></tr>`;
}
document.getElementById('q').addEventListener('input', render);

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
