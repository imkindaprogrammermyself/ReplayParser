"""Replay -> video: python -m wowsreplay.video REPLAY [-o out.mp4] [--speed 10] [--cam player] [--start 2:00] ...

The viewer page in its video mode (#video=1: the 3D view, the ship card and HUD, team scores, the event bar with the
score graph; no side panel or controls) is opened in headless Chrome and driven one frame at a time over the DevTools
protocol: each frame sets the battle time (start + frame * speed / fps), renders, and is screenshotted; the frames are
piped to ffmpeg. Deterministic - a slow machine makes a slow render, not a choppy video.

Needs Chrome or Chromium and ffmpeg with libx264. WebGL runs on the GPU when Chrome can use it (else in software,
SwiftShader; --no-gpu forces that); --encoder vaapi encodes on the GPU too.
"""
import argparse
import base64
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .__main__ import build_data, render_page
from .privacy import anonymise as anonymise_data, leaks

CHROMES = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome')
CAMS = ('player', 'follow', 'follow-rotate', 'orbit', 'orbit-rotate', '2d')
KM = 1000 / 30   # world units per km (1 u = 30 m)


# ------------------------------------------------------------------ minimal WebSocket client (RFC 6455, stdlib only)

class WebSocket:
    def __init__(self, url, timeout=120):
        assert url.startswith('ws://'), url
        hostport, _, path = url[5:].partition('/')
        host, _, port = hostport.partition(':')
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError('DevTools closed the connection during the handshake')
            head += chunk
        head, self.buf = head.split(b'\r\n\r\n', 1)
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise ConnectionError('DevTools refused the WebSocket: ' + head.split(b'\r\n', 1)[0].decode(errors='replace'))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError('DevTools connection closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        data, mask = text.encode(), os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                                struct.pack('>BH', 0x80 | 126, n) if n < 65536 else struct.pack('>BQ', 0x80 | 127, n))
        masked = (int.from_bytes(data, 'big') ^ int.from_bytes((mask * (n // 4 + 1))[:n], 'big')).to_bytes(n, 'big') if n else b''
        self.sock.sendall(head + mask + masked)

    def recv(self):
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7f
            if n == 126:
                n, = struct.unpack('>H', self._read(2))
            elif n == 127:
                n, = struct.unpack('>Q', self._read(8))
            mask = self._read(4) if b1 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            op = b0 & 0x0f
            if op == 8:
                raise ConnectionError('DevTools closed the connection')
            if op == 9:   # ping -> pong (client frames are masked)
                m = os.urandom(4)
                self.sock.sendall(bytes([0x8a, 0x80 | len(data)]) + m + bytes(c ^ m[i % 4] for i, c in enumerate(data)))
                continue
            if op in (0, 1, 2):
                parts.append(data)
                if b0 & 0x80:
                    return b''.join(parts).decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class DevTools:
    """One page target: call(method, **params) -> result; events are skipped."""
    def __init__(self, ws_url):
        self.ws, self.next_id = WebSocket(ws_url), 0

    def call(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps({'id': self.next_id, 'method': method, 'params': params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == self.next_id:
                if 'error' in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg['result']

    def js(self, expr, await_promise=False):
        r = self.call('Runtime.evaluate', expression=expr, awaitPromise=await_promise, returnByValue=True)
        if 'exceptionDetails' in r:
            d = r['exceptionDetails']
            raise RuntimeError('page script error: ' + (d.get('exception', {}).get('description') or d.get('text', '?')))
        return r['result'].get('value')


# ------------------------------------------------------------------ helpers

def find_chrome(path=None):
    if path:
        return path
    for name in CHROMES:
        p = shutil.which(name)
        if p:
            return p
    sys.exit('Chrome / Chromium not found: install it, or pass --chrome PATH')


def parse_time(s):
    """'90', '1:30' -> seconds."""
    if s is None:
        return None
    m, _, sec = s.rpartition(':')
    return (int(m) * 60 if m else 0) + float(sec)


# WebGL on the GPU through ANGLE over OpenGL/EGL (~2.7x faster than software on a Radeon 780M: the screenshot, which
# waits for the frame to be drawn, drops from ~150 to ~48 ms); Chrome falls back to SwiftShader if the GPU can't be used
GPU_FLAGS = ['--use-gl=angle', '--use-angle=gl-egl', '--ignore-gpu-blocklist']


def launch_chrome(chrome, profile, w, h, no_sandbox, gpu=True):
    args = [chrome, '--headless=new', '--remote-debugging-port=0', f'--user-data-dir={profile}', '--no-first-run',
            '--no-default-browser-check', '--enable-unsafe-swiftshader', '--hide-scrollbars', '--mute-audio',
            '--allow-file-access-from-files', f'--window-size={w},{h}', *(GPU_FLAGS if gpu else []), 'about:blank']
    if no_sandbox:
        args.insert(1, '--no-sandbox')
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = Path(profile) / 'DevToolsActivePort'
    for _ in range(300):   # Chrome writes its port here once DevTools listens
        if port_file.exists() and port_file.read_text().strip():
            port = int(port_file.read_text().split()[0])
            break
        if proc.poll() is not None:
            sys.exit(f'Chrome exited at start (code {proc.returncode}); try --no-sandbox')
        time.sleep(.1)
    else:
        proc.kill()
        sys.exit('Chrome did not open its DevTools port')
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=10) as r:
        page = next(t for t in json.load(r) if t['type'] == 'page')
    return proc, page['webSocketDebuggerUrl']


def vaapi_device():
    nodes = sorted(Path('/dev/dri').glob('renderD*')) if Path('/dev/dri').is_dir() else []
    if not nodes:
        raise RuntimeError('--encoder vaapi: no /dev/dri/renderD* device')
    return str(nodes[0])


def battle_start(clock):
    """Replay time the battle starts: the clock jumps from the pre-battle countdown (30 .. 0) to the battle length
    (e.g. 1200). Ships can't move before it. 0 if the replay has no countdown."""
    return next((clock[i][0] for i in range(1, len(clock)) if clock[i][1] > clock[i - 1][1] + 60), 0.0)


def render_video(replay, out, speed=10.0, fps=30, cam='player', start=None, end=None, size=(1920, 1080),
                 crf=20, chrome=None, no_sandbox=False, quiet=False, countdown=False, gpu=True, encoder='x264', rotate=6.0, dist=None, pitch=None, yaw=None,
                 anonymise=False):
    """cam: see CAMS; *-rotate turn `rotate` degrees per second of video. dist (km), pitch / yaw (degrees) frame the
    orbit / follow cameras (default: whole map for orbit, ~1.1 km behind the ship for follow)."""
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    if not shutil.which('ffmpeg'):
        sys.exit('ffmpeg not found')
    w, h = size
    if w % 2 or h % 2:
        sys.exit('--size: width and height must be even (H.264, yuv420p)')
    say('parsing replay ...')
    data = build_data(replay)
    secrets = anonymise_data(data) if anonymise else set()   # other players -> 'Ally 3' / 'Enemy 5'
    if cam != '2d' and not data['models']:
        say('note: no ship models imported for this battle - 3D shows placeholder hulls')
    t_end = data['meta']['end']
    if start is None and not countdown:   # skip the ~30 s pre-battle countdown: nothing moves in it
        start = battle_start(data['clock'])
    t0 = max(0.0, start or 0.0)
    t1 = min(t_end, end if end is not None else t_end)
    if t1 <= t0:
        sys.exit(f'nothing to render: start {t0:.0f} s >= end {t1:.0f} s (battle is {t_end:.0f} s)')
    frames = max(1, int((t1 - t0) * fps / speed))
    say(f'{data["meta"]["map"]}, {t0:.0f}-{t1:.0f} s at {speed:g}x -> {frames} frames, {frames / fps:.1f} s of video')

    with tempfile.TemporaryDirectory(prefix='wowsvideo-', ignore_cleanup_errors=True) as tmp:   # Chrome may lag on exit
        page = Path(tmp) / 'page.html'
        html = render_page(data)
        if leaks(secrets, html):
            raise RuntimeError('--anonymise: a player name / id is still in the page')
        page.write_text(html, encoding='utf-8')
        del data
        hash_ = f'video=1&t={t0}'
        if cam != '2d':
            base = cam.split('-')[0]
            hash_ += f'&view=3d&cam={base}'
            if base in ('orbit', 'follow'):
                if dist is not None:
                    hash_ += f'&dist={dist * KM:.1f}'
                if pitch is not None:
                    hash_ += f'&pitch={math.radians(pitch):.4f}'
                if yaw is not None:
                    hash_ += f'&yaw={math.radians(yaw) or 1e-6:.6f}'   # 0 would mean 'default' to the page
                if cam.endswith('-rotate') and rotate:
                    hash_ += f'&rot={rotate:g}'
        proc, ws_url = launch_chrome(find_chrome(chrome), Path(tmp) / 'profile', w, h, no_sandbox, gpu)
        ff = part = None
        try:
            dt = DevTools(ws_url)
            dt.call('Emulation.setDeviceMetricsOverride', width=w, height=h, deviceScaleFactor=1, mobile=False)
            dt.call('Page.enable')
            dt.call('Page.navigate', url=page.as_uri() + '#' + hash_)
            say('loading the page (data, models, terrain) ...')
            for _ in range(600):
                if dt.js('typeof window.VIDEO_READY !== "undefined"'):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('the viewer page did not start')
            dt.js('window.VIDEO_READY', await_promise=True)
            if cam != '2d' and not dt.js('V3.active'):
                raise RuntimeError('3D did not start in headless Chrome (WebGL unavailable?) - try --cam 2d')
            if cam != '2d':
                say('WebGL: ' + str(dt.js("(() => { const gl = V3.renderer.getContext(), e = gl.getExtension("
                                          "'WEBGL_debug_renderer_info'); return e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL)"
                                          " : gl.getParameter(gl.RENDERER); })()")))
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            # encode into a private file and rename it at the end: two runs writing the same output can't interleave
            # (that corrupts the MP4), and a failed run leaves the previous video in place
            stem, ext = os.path.splitext(out)
            part = f'{stem}.{os.getpid()}.part{ext or ".mp4"}'
            if encoder == 'vaapi':   # GPU encoder (AMD / Intel through VAAPI); quality as a constant QP
                enc = ['-vaapi_device', vaapi_device(), '-f', 'image2pipe', '-c:v', 'mjpeg', '-framerate', str(fps), '-i', '-',
                       '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-rc_mode', 'CQP', '-qp', str(crf)]
            else:
                enc = ['-f', 'image2pipe', '-c:v', 'mjpeg', '-framerate', str(fps), '-i', '-', '-c:v', 'libx264',
                       '-preset', 'medium', '-crf', str(crf), '-pix_fmt', 'yuv420p']
            ff = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *enc, '-movflags', '+faststart', part],
                                  stdin=subprocess.PIPE)
            began, last_report = time.time(), 0
            for i in range(frames + 1):
                dt.js(f'VIDEO.frame({t0 + i * speed / fps}, {i * 1000 / fps})')
                shot = dt.call('Page.captureScreenshot', format='jpeg', quality=92, optimizeForSpeed=True)
                ff.stdin.write(base64.b64decode(shot['data']))
                now = time.time()
                if not quiet and (now - last_report > 2 or i == frames):
                    last_report = now
                    rate = (i + 1) / max(1e-6, now - began)
                    print(f'\r  frame {i + 1}/{frames + 1}  battle {t0 + i * speed / fps:6.1f} s  {rate:4.1f} frames/s  '
                          f'eta {(frames - i) / rate:5.0f} s ', end='', flush=True)
            say()
            ff.stdin.close()
            if ff.wait() != 0:
                raise RuntimeError(f'ffmpeg failed (exit {ff.returncode})')
            ff = None
            os.replace(part, out)
        finally:
            if ff:
                ff.kill()
                ff.wait()
            if part and os.path.exists(part):
                os.remove(part)
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    say(f'video: {out} ({os.path.getsize(out) / 1048576:.1f} MB)')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='python -m wowsreplay.video', description='Render a replay to an MP4 video')
    ap.add_argument('replay', help='.wowsreplay file')
    ap.add_argument('-o', '--out', help='output .mp4 (default: out/<replay>.mp4)')
    ap.add_argument('--speed', type=float, default=10, help='replay speed: battle seconds per video second (default 10)')
    ap.add_argument('--cam', choices=CAMS, default='player',
                    help='player = the recorded game camera (default); follow = your ship from a fixed angle, '
                         'follow-rotate = circling your ship; orbit = the whole map from a fixed angle, orbit-rotate = '
                         'circling the map; 2d = minimap')
    ap.add_argument('--rotate', type=float, default=6, help='*-rotate cameras: degrees per second of video (default 6 = '
                    'a turn a minute; negative = the other way)')
    ap.add_argument('--dist', type=float, help='orbit / follow: camera distance in km (default: whole map / ~1.1 km)')
    ap.add_argument('--pitch', type=float, help='orbit / follow: degrees above the horizon (default 49 / 17)')
    ap.add_argument('--yaw', type=float, help='orbit / follow: start direction in degrees (0 = camera south of the target, '
                    'looking north)')
    ap.add_argument('--start', help='from this replay time (seconds or m:ss, as the viewer shows it; default: when the '
                                    'battle starts, after the pre-battle countdown)')
    ap.add_argument('--countdown', action='store_true', help='start at 0:00, with the ~30 s pre-battle countdown')
    ap.add_argument('--end', help='to this battle time (seconds or m:ss, default the end)')
    ap.add_argument('--fps', type=int, default=30, help='frames per second (default 30)')
    ap.add_argument('--size', default='1920x1080', help='WIDTHxHEIGHT (default 1920x1080)')
    ap.add_argument('--crf', type=int, default=20, help='x264 quality, lower = better / bigger (default 20)')
    ap.add_argument('--chrome', help='Chrome / Chromium binary (default: found on PATH)')
    ap.add_argument('--no-sandbox', action='store_true', help="pass --no-sandbox to Chrome (if it won't start)")
    ap.add_argument('--anonymise', action='store_true', help="other players as 'Ally 3' / 'Enemy 5' (for sharing)")
    ap.add_argument('--no-gpu', action='store_true', help='render WebGL in software (SwiftShader) instead of on the GPU')
    ap.add_argument('--encoder', choices=('x264', 'vaapi'), default='x264',
                    help='x264 = libx264 on the CPU (default), vaapi = the GPU\'s H.264 encoder (AMD / Intel)')
    a = ap.parse_args(argv)
    if a.speed <= 0 or a.fps <= 0:
        ap.error('--speed and --fps must be positive')
    try:
        size = tuple(int(v) for v in a.size.lower().split('x'))
        assert len(size) == 2 and min(size) >= 320
    except (ValueError, AssertionError):
        ap.error('--size must look like 1920x1080')
    out = a.out or os.path.join('out', os.path.splitext(os.path.basename(a.replay))[0] + '.mp4')
    try:
        render_video(a.replay, out, a.speed, a.fps, a.cam, parse_time(a.start), parse_time(a.end), size, a.crf,
                     a.chrome, a.no_sandbox, countdown=a.countdown, gpu=not a.no_gpu, encoder=a.encoder,
                     rotate=a.rotate, dist=a.dist, pitch=a.pitch, yaw=a.yaw, anonymise=a.anonymise)
    except (RuntimeError, ConnectionError) as e:
        sys.exit(f'error: {e}')


if __name__ == '__main__':
    main()
