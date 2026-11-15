"""Import map data unpacked by wowsunpack.exe (see unpack_spaces.bat).

    python tools/import_spaces.py wows_spaces.zip      # or the unpacked wows_spaces folder

For every spaces/<map>/ found, writes wowsreplay/data/maps/<map>/:
  minimap.png   copied as-is
  terrain.i16   int16 LE heights (x0.01 world units), n*n, row-major, row = +z, col = +x
  map.json      space extent, minimap extent, terrain grid

terrain.bin format (worked out from 15.9/15.10 files):
  b'trb\\0', u32 width, u32 height, u16 chunks per side, u16 samples per chunk,
  f32 (min, max) for the whole map, then f32 (min, max) per chunk,
  then width*height f32 heights, run-length coded: two equal words in a row are followed
  by a u32 count that is the run's full length (including those two).
  Rows run along +z from the space's min corner, columns along +x. Each chunk has
  129 shared-edge samples plus 2 border samples, so sample 128 of a chunk == sample 0 of the next.
  Chunks are 100 world units (BigWorld); space.settings <bounds> gives the chunk index range.

The minimap's world extent isn't in these files; it's fitted by matching the minimap's
land (alpha) against the heightmap's land. ±800 for every map tried so far.
"""
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib

import numpy as np

OUT = os.path.join(os.path.dirname(__file__), '..', 'wowsreplay', 'data', 'maps')
CHUNK = 100.0
STEP = 4          # keep every 4th sample: 3.125 world units


class Source:
    """Read files from a zip or a folder, by their path below 'spaces/'."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path) if zipfile.is_zipfile(path) else None
        self.root = path
        if self.zip:
            self.names = {}
            for n in self.zip.namelist():
                m = re.search(r'(?:^|/)spaces/([^/]+/[^/]+)$', n.replace('\\', '/'))
                if m:
                    self.names[m.group(1)] = n
        else:
            base = path if os.path.basename(path) == 'spaces' else os.path.join(path, 'spaces')
            self.names = {f'{m}/{f}': os.path.join(base, m, f)
                          for m in os.listdir(base) for f in os.listdir(os.path.join(base, m))}

    def maps(self):
        return sorted({k.split('/')[0] for k in self.names if k.endswith('/terrain.bin')})

    def read(self, rel):
        n = self.names.get(rel)
        if n is None:
            return None
        if self.zip:
            return self.zip.read(n)
        with open(n, 'rb') as f:
            return f.read()


def decode_terrain(d):
    if d[:4] != b'trb\0':
        raise ValueError('not a terrain.bin')
    w, h, nch, cs = struct.unpack_from('<IIHH', d, 4)
    o = 16 + (1 + nch * nch) * 8
    words = np.frombuffer(d, '<u4', (len(d) - o) // 4, o)
    out = np.empty(w * h, '<u4')
    # vectorised RLE: find each "a, a, count" triple, then expand
    n = i = 0
    L = len(words)
    same = np.flatnonzero(words[1:] == words[:-1])          # i where words[i] == words[i+1]
    k = 0
    while i < L and n < w * h:
        while k < len(same) and same[k] < i:
            k += 1
        j = same[k] if k < len(same) else L                 # next run start
        lit = min(j, L) - i
        out[n:n + lit] = words[i:i + lit]
        n += lit
        i += lit
        if j >= L or n >= w * h:
            break
        run = int(words[j + 2])
        out[n:n + run] = words[j]
        n += run
        i = j + 3
    if n != w * h or i != L:
        raise ValueError(f'RLE mismatch: {n} of {w * h} samples, {i} of {L} words')
    g = out.view('<f4').reshape(h, w)
    keep = np.concatenate([np.arange(c * cs, c * cs + cs - 3) for c in range(nch)] + [[nch * cs - 3]])
    return g[np.ix_(keep, keep)], (cs - 3)                 # (n, n) seamless grid, cells per chunk


def read_png(d):
    pos, idat = 8, b''
    while pos < len(d):
        n, t = struct.unpack('>I4s', d[pos:pos + 8])
        body = d[pos + 8:pos + 8 + n]
        pos += 12 + n
        if t == b'IHDR':
            w, h, bd, ct = struct.unpack('>IIBB', body[:10])
        elif t == b'IDAT':
            idat += body
    if bd != 8 or ct not in (2, 6):
        raise ValueError('unsupported PNG')
    c = 4 if ct == 6 else 3
    raw, st = zlib.decompress(idat), w * c
    out = np.zeros((h, st), np.int32)
    prev = np.zeros(st, np.int32)
    p = 0
    for y in range(h):
        f = raw[p]
        line = np.frombuffer(raw, np.uint8, st, p + 1).astype(np.int32)
        p += 1 + st
        if f == 0:
            cur = line
        elif f == 2:
            cur = (line + prev) & 255
        else:   # sub / average / paeth depend on the pixel to the left: per pixel
            cur = np.zeros(st, np.int32)
            for x in range(0, st, c):
                a = cur[x - c:x] if x else np.zeros(c, np.int32)
                b = prev[x:x + c]
                cc = prev[x - c:x] if x else np.zeros(c, np.int32)
                if f == 1:
                    pr = a
                elif f == 3:
                    pr = (a + b) // 2
                else:
                    pa, pb, pc = np.abs(b - cc), np.abs(a - cc), np.abs(a + b - 2 * cc)
                    pr = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, cc))
                cur[x:x + c] = (line[x:x + c] + pr) & 255
        out[y] = cur
        prev = cur
    return out.reshape(h, w, c).astype(np.uint8)


def fit_minimap(G, origin, spacing, png):
    """Square world extent centred on 0 whose heightmap land best matches the minimap's alpha."""
    img = read_png(png)
    if img.shape[2] != 4:
        return None, 0.0
    M = img.shape[0]
    land = img[::4, ::4, 3] > 128
    ys, xs = np.mgrid[0:M:4, 0:M:4] + 0.5
    best = (0.0, None)
    for half in range(300, int(-origin), 10):
        x = -half + xs / M * 2 * half
        z = half - ys / M * 2 * half          # image top = +z
        col = ((x - origin) / spacing).astype(int)
        row = ((z - origin) / spacing).astype(int)
        L = G[row, col] > 0
        iou = (L & land).sum() / max((L | land).sum(), 1)
        if iou > best[0]:
            best = (iou, half)
    iou, half = best
    return half, iou


def main(argv):
    if len(argv) != 1:
        sys.exit(__doc__)
    src = Source(argv[0])
    for name in src.maps():
        settings = (src.read(f'{name}/space.settings') or b'').decode('utf-8', 'replace')
        b = {k: int(v) for k, v in re.findall(r'(min[XY]|max[XY])="(-?\d+)"', settings)}
        if b.get('minX') != b.get('minY') or b.get('maxX') != b.get('maxY'):
            print(f'{name}: non-square bounds {b}, skipped')
            continue
        G, cells = decode_terrain(src.read(f'{name}/terrain.bin'))
        spacing = CHUNK / cells
        origin = b['minX'] * CHUNK
        if abs(origin + (G.shape[0] - 1) * spacing - (b['maxX'] + 1) * CHUNK) > 1e-6:
            print(f'{name}: grid size {G.shape} does not match bounds {b}, skipped')
            continue

        dst = os.path.join(OUT, name)
        os.makedirs(dst, exist_ok=True)
        meta = {'space': [origin, origin, (b['maxX'] + 1) * CHUNK, (b['maxY'] + 1) * CHUNK]}
        png = src.read(f'{name}/minimap.png')
        if png:
            half, iou = fit_minimap(G, origin, spacing, png)
            meta['minimap'] = [-half, -half, half, half] if half else None
            meta['minimapFitIoU'] = round(float(iou), 3)
            with open(os.path.join(dst, 'minimap.png'), 'wb') as f:
                f.write(png)

        g = G[::STEP, ::STEP]
        meta['terrain'] = {'n': g.shape[0], 'x0': origin, 'z0': origin, 'step': spacing * STEP,
                           'scale': 0.01, 'min': round(float(G.min()), 2), 'max': round(float(G.max()), 2)}
        np.clip(np.round(g * 100), -32768, 32767).astype('<i2').tofile(os.path.join(dst, 'terrain.i16'))
        with open(os.path.join(dst, 'map.json'), 'w') as f:
            json.dump(meta, f, indent=1)
        print(f"{name}: terrain {G.shape[0]}² -> {g.shape[0]}², heights {meta['terrain']['min']}..{meta['terrain']['max']}, "
              f"minimap {meta.get('minimap')} (IoU {meta.get('minimapFitIoU')})")


if __name__ == '__main__':
    main(sys.argv[1:])
