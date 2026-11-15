"""Smoke tests over whatever replays are in the project folder / replays/ (skipped if none).

    python -m unittest discover tests
"""
import glob
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wowsreplay.container import read_header  # noqa: E402
from wowsreplay.timeline import AIM_RANGE_STEP, build_timeline  # noqa: E402

REPLAYS = sorted(glob.glob(os.path.join(ROOT, '*.wowsreplay')) +
                 glob.glob(os.path.join(ROOT, 'replays', '**', '*.wowsreplay'), recursive=True))
_cache = {}


def timeline(path):
    if path not in _cache:
        _cache[path] = build_timeline(path)
    return _cache[path]


@unittest.skipUnless(REPLAYS, 'no replays in the project folder')
class ReplayTests(unittest.TestCase):
    def each(self):
        return [(r, timeline(r)) for r in REPLAYS]

    def test_no_parse_errors(self):
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                self.assertEqual(d['parseErrors'], {})

    def test_roster_matches_header(self):
        """Every player in the header shows up by name (catches arena-state key map shifts)."""
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                names = {v['name'] for v in read_header(r)['vehicles']}
                got = {s['name'] for s in d['ships']}
                self.assertTrue(all(isinstance(n, str) for n in got), 'non-string ship names')
                self.assertLessEqual(names, got)

    def test_ship_types_known(self):
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                self.assertTrue(all(s['species'] for s in d['ships']), 'ships without a type')

    def test_aim_matches_shells(self):
        """targetLocalPos low byte points where main-battery shells land.

        ~85% of shells within 5 degrees (stale aim values included; ~95% of fresh ones). A wrong decode gives ~3%."""
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                ok = n = 0
                for t, owner, x0, z0, x1, z1, *rest in d['shots']:
                    if rest[1] & 0x8000:            # secondaries aim on their own
                        continue
                    series = d['aim'].get(str(owner))
                    tr = d['tracks'].get(str(owner))
                    if not series or not tr:
                        continue
                    row = max((a for a in series if a[0] <= t), default=None, key=lambda a: a[0])
                    pos = max((p for p in tr if p[0] <= t), default=None, key=lambda p: p[0])
                    if not row or not pos or t - row[0] > 2:
                        continue
                    bearing = ((row[1] & 255) + .5) / 256 * 360 + 180
                    actual = math.degrees(math.atan2(x1 - pos[1], z1 - pos[2]))
                    n += 1
                    ok += abs((actual - bearing + 180) % 360 - 180) < 5
                if n >= 50:
                    self.assertGreaterEqual(ok / n, .8, f'{ok}/{n} shells match the aim bearing')

    def test_battle_events(self):
        """Consumables resolve to known types with sensible ranges; damage names real ships."""
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                ids = {s['id'] for s in d['ships']}
                self.assertTrue(d['consumables'])
                for t, vid, label, dur, rng in d['consumables']:
                    self.assertIn(vid, ids)
                    self.assertFalse(label.startswith(('Consumable ', '7')), label)
                    if label == 'Radar':
                        self.assertTrue(rng and 200 < rng < 500, rng)       # 6-15 km
                self.assertTrue(d['damage'])
                for t, victim, attacker, dmg in d['damage']:
                    self.assertIn(victim, ids)
                    self.assertGreaterEqual(dmg, 0)
                for sm in d['smokes']:
                    self.assertTrue(all(len(f[1]) % 2 == 0 for f in sm['frames']))

    def test_torpedo_ends(self):
        """Torpedoes end where receiveShotKills says; straight ones end on their launch line."""
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                ended = [tp for tp in d['torpedoes'] if tp[6] is not None]
                self.assertGreaterEqual(len(ended), .95 * len(d['torpedoes']))
                for t0, owner, x, z, dx, dz, t1, x1, z1, hit, path in ended:
                    self.assertTrue(0 <= t1 - t0 < 200, (t0, t1))
                    if not path:
                        err = math.hypot(x + dx * (t1 - t0) - x1, z + dz * (t1 - t0) - z1)
                        self.assertLess(err, 5, (owner, t0))
                    else:
                        self.assertTrue(all(t0 <= p[0] <= t1 + 1 for p in path))

    def test_speed_matches_tracks(self):
        """Knots from serverSpeedRaw agree with how fast the ship moves on its track (~0.09 world units/s per knot)."""
        import bisect
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                ratios = []
                for vid, series in d['speed'].items():
                    tr = d['tracks'].get(vid) or []
                    ts = [p[0] for p in tr]
                    for t, kn in series[::5]:
                        self.assertTrue(-25 < kn < 60, kn)
                        i = bisect.bisect_left(ts, t)
                        if abs(kn) > 10 and 2 <= i < len(tr) - 2 and tr[i + 2][0] - tr[i - 2][0] > .3:
                            a, b = tr[i - 2], tr[i + 2]
                            ratios.append(math.hypot(b[1] - a[1], b[2] - a[2]) / (b[0] - a[0]) / abs(kn))
                self.assertGreater(len(ratios), 100)
                ratios.sort()
                self.assertTrue(.08 < ratios[len(ratios) // 2] < .10, ratios[len(ratios) // 2])
                for series in d['rudder'].values():
                    self.assertTrue(all(-1.01 <= x <= 1.01 for _, x in series))

    def test_burn_flood_bits(self):
        """burningFlags = 4 burn nodes (bits 0-3) + 2 flood nodes (bits 4-5); a ram floods the survivor."""
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                for series in d['fires'].values():
                    self.assertTrue(all(0 <= f <= 0x3f for _, f in series))
                died = {x[1]: x[0] for x in d['deaths']}
                for t, dead, other, cause in d['deaths']:
                    if cause == 'RAM' and died.get(other, 1e9) > t + 1:   # rams flood the ship that survives
                        flags = [f for ft, f in d['fires'].get(str(other), []) if t - 1 <= ft <= t + 1]
                        self.assertTrue(any(f >> 4 for f in flags), f'no flood on {other} after ram at {t}')

    def test_timeline_shapes(self):
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                self.assertGreater(d['meta']['end'], 60)
                self.assertTrue(d['tracks'])
                self.assertEqual(d['meta']['aimRangeStep'], AIM_RANGE_STEP)


if __name__ == '__main__':
    unittest.main()
