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


MODELS = sorted(glob.glob(os.path.join(ROOT, 'wowsreplay', 'data', 'models', '*.json')))


@unittest.skipUnless(MODELS, 'no imported ship models')
class ModelTests(unittest.TestCase):
    def test_fire_points_bow_to_stern(self):
        """EP_Fire_1..4 (burningFlags bits 0..3) run bow (-z in viewer space) to stern on 4-node hulls."""
        import json
        for path in MODELS:
            with self.subTest(model=os.path.basename(path)):
                with open(path, encoding='utf-8') as f:
                    fires = json.load(f).get('fires')
                self.assertIsNotNone(fires, 're-run tools/import_models.py')
                pts = [p for p in fires if p]
                self.assertTrue(pts)
                if len(pts) == 4:
                    zs = [p[2] for p in pts]
                    self.assertEqual(zs, sorted(zs), zs)


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

    def test_shell_flights(self):
        """Shells end where receiveShotKills says; ones that land where aimed took ~0.363 x serverTimeLeft."""
        import statistics
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                ended = [s for s in d['shots'] if s[9] is not None]
                self.assertGreater(len(ended), .25 * len(d['shots']))
                for s in ended:
                    self.assertAlmostEqual(s[9] - s[0], s[6], delta=.06)
                    self.assertTrue(0 < s[6] < 60, s[6])
                aimed = [s for s in ended if math.hypot(s[10] - s[4], s[11] - s[5]) < 3]
                self.assertLess(statistics.median(math.hypot(s[10] - s[4], s[11] - s[5]) for s in ended), 10)
                self.assertGreater(len(aimed), 100)

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

    def test_plane_tracks_smooth(self):
        """Squadron tracks rarely jump (> 2.5x the squadron's median speed); mixing waypoints (1.3-2.3 s ahead)
        with current minimap points as if they were simultaneous made ~25% of consumable-plane steps jump."""
        import statistics
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                steps = jumps = 0
                for sq in d['planes']:
                    tr = sq['track']
                    sp = [math.hypot(b[1] - a[1], b[2] - a[2]) / (b[0] - a[0]) for a, b in zip(tr, tr[1:]) if b[0] > a[0]]
                    if len(sp) < 4:
                        continue
                    med = statistics.median(sp)
                    steps += len(sp)
                    jumps += sum(1 for v in sp if v > 2.5 * med + 1)
                if steps > 200:
                    self.assertLess(jumps / steps, .03, f'{jumps} of {steps}')

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


    def test_analytics(self):
        """Damage sources account for every damage row, almost all matched to a real source; sane per-ship numbers;
        fights have both sides in them; the flow ends where the battle does."""
        from wowsreplay.analytics import analyze, SOURCES
        for r, d in self.each():
            with self.subTest(replay=os.path.basename(r)):
                a = analyze(d)
                own = d['meta']['ownVehicle']
                rep = a['ships'][str(own)]
                self.assertEqual(rep['damage']['dealt'], sum(x[3] for x in d['damage'] if x[2] == own and x[1] != own))
                self.assertEqual(rep['damage']['taken'], sum(x[3] for x in d['damage'] if x[1] == own))
                total = {k: 0 for k in SOURCES}
                for v in a['ships'].values():
                    self.assertLessEqual(set(v['damage']['bySource']), set(SOURCES))
                    for k, x in v['damage']['bySource'].items():
                        total[k] += x
                self.assertLess(total['other'] / sum(total.values()), .05)       # matched to a shell / fire / ...
                g = rep['main']
                if g:
                    self.assertLessEqual(g['hits'], g['tracked'])
                    self.assertLessEqual(g['tracked'], g['shells'])
                    self.assertEqual(sum(b[2] for b in g['byRange']), g['shells'])
                    self.assertEqual(sum(g['kinds'].values()), g['hits'])
                tk = rep['tank']
                self.assertEqual(sum(tk['angles']), tk['hits'])
                self.assertTrue(any(f['dealt']['ally'] and f['dealt']['enemy'] for f in a['fights']))
                self.assertEqual(a['flow']['t'][-1], round(d['meta']['end'], 1))
                self.assertTrue(all(0 <= h <= 1 for row in a['flow']['hp'] for h in row))
                p = a['personal']
                self.assertEqual((p['dealt'], p['kills']), (rep['damage']['dealt'], rep['kills']))
                self.assertIn(p['win'], (1, 0, -1, None))
                self.assertEqual(len(a['damageSource']), len(d['damage']))       # heatmaps index it by damage row
                self.assertLessEqual(set(a['damageSource']), set('mstafwro'))


if __name__ == '__main__':
    unittest.main()
