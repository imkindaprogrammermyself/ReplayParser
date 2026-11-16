"""Battle analytics from a timeline (build_timeline's dict): per-ship reports, battle flow, highlights, aircraft.

    analyze(data) -> {'ships': {vid: report}, 'flow': {...}, 'fights': [...], 'highlights': [...],
                      'aircraft': {...}, 'personal': {...}}

Everything comes from what the recording client saw: the own ship's numbers are complete, other ships' are as far as
they were in view (damage between ships far away can be missing). Distances in the output are km (1 world unit =
30 m). Shell accuracy counts a shell as a hit only when the game reported where it ended (~70 % of the own ship's
shells get an end), so it is a lower bound.
"""
import bisect
import math
from collections import Counter, defaultdict

KM = 0.03                      # world units -> km
SHIP_HIT = 0x60                # hitType & 0x60 == 0x60: the shell ended on a ship
HIT_KINDS = {0x62: 'cit', 0x63: 'pen', 0x64: 'over', 0x61: 'rico', 0x60: 'shatter'}
RANGE_BINS = [0, 4, 8, 12, 16, 20]                    # km
ANGLE_BINS = [('bow-on', 30), ('angled fore', 60), ('broadside', 120), ('angled aft', 150), ('stern-on', 181)]
SOURCES = ['main', 'secondary', 'torpedo', 'aircraft', 'fire', 'flood', 'ram', 'other']


class Series:
    """[t, ...] rows sorted by t: the row in effect at time t."""
    def __init__(self, rows):
        self.rows = rows or []
        self.ts = [r[0] for r in self.rows]

    def at(self, t, default=None):
        i = bisect.bisect_right(self.ts, t) - 1
        return self.rows[i] if i >= 0 else default

    def fresh(self, t, max_age):
        r = self.at(t)
        return r if r is not None and t - r[0] <= max_age else None


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _median(v):
    v = sorted(v)
    return None if not v else v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2


def _r(x, n=1):
    return None if x is None else round(x, n)


def analyze(data):
    A = _Analysis(data)
    return A.run()


class _Analysis:
    def __init__(self, data):
        self.d = data
        self.end = data['meta']['end'] or 0
        self.ships = {s['id']: s for s in data['ships']}
        own_team = data['meta'].get('ownTeam')
        self.side = {vid: ('ally' if s['team'] == own_team else 'enemy') for vid, s in self.ships.items()}
        self.tracks = {int(k): Series(v) for k, v in data['tracks'].items()}
        self.minimap = {int(k): Series(v) for k, v in (data.get('minimap') or {}).items()}
        self.health = {int(k): Series(v) for k, v in data['health'].items()}
        self.fires = {int(k): Series(v) for k, v in data['fires'].items()}
        self.detected = {int(k): Series(v) for k, v in data['detected'].items()}
        self.death = {d[1]: d for d in data['deaths']}
        self.half_len = {vid: (s.get('length') or 150) / 2 / 30 for vid, s in self.ships.items()}   # world units
        # Arms Race HP pickups raise health above the ship's listed maximum
        self.max_hp = {vid: max([s.get('maxHealth') or 0] + [r[1] for r in (self.health[vid].rows if vid in self.health else ())])
                       for vid, s in self.ships.items()}

    # ---------------------------------------------------------------- positions
    def pos(self, vid, t, max_age=15):
        """Best known (x, z, yaw) of a ship at t: its own track, else a recent minimap fix."""
        r = self.tracks.get(vid)
        r = r.fresh(t, max_age) if r else None
        if r is None:
            m = self.minimap.get(vid)
            r = m.fresh(t, max_age) if m else None
        return (r[1], r[2], r[3]) if r is not None else None

    def alive_until(self, vid):
        d = self.death.get(vid)
        return d[0] if d else self.end

    def nearest_ship(self, x, z, t, exclude=None):
        best = None
        for vid in self.ships:
            if vid == exclude or t > self.alive_until(vid) + 1:
                continue
            p = self.pos(vid, t, 5)
            if p:
                d = math.hypot(p[0] - x, p[1] - z) - self.half_len[vid]
                if best is None or d < best[0]:
                    best = (d, vid)
        return best[1] if best and best[0] < 4 else None      # within ~120 m of the hull

    # ---------------------------------------------------------------- run
    def run(self):
        self.index_events()
        self.attribute_damage()
        reports = {vid: self.ship_report(vid) for vid in self.ships}
        out = {'ships': {str(k): v for k, v in reports.items()}, 'flow': self.flow(), 'fights': self.fight_list(),
               'highlights': self.highlight_list(), 'aircraft': self.aircraft()}
        out['personal'] = self.personal(out)
        return out

    def index_events(self):
        """Shell and torpedo endings per shooter, with the ship they ended on."""
        self.shell_ends = defaultdict(list)     # shooter -> [(end_t, victim, kind, secondary)]
        self.shell_hits_on = defaultdict(list)  # victim -> [(end_t, shooter, hitType, x0, z0)]
        for s in self.d['shots']:
            ht = s[12]
            if ht is None or ht & SHIP_HIT != SHIP_HIT:
                continue
            victim = self.nearest_ship(s[10], s[11], s[9], exclude=s[1])
            self.shell_ends[s[1]].append((s[9], victim, ht, bool(s[7] & 0x8000)))
            if victim is not None:
                self.shell_hits_on[victim].append((s[9], s[1], ht, s[2], s[3], bool(s[7] & 0x8000)))
        self.torp_ends = defaultdict(list)      # shooter -> [(end_t, victim)]
        for tp in self.d['torpedoes']:
            if tp[9] is not None and tp[9] & SHIP_HIT == SHIP_HIT:
                victim = self.nearest_ship(tp[7], tp[8], tp[6], exclude=tp[1])
                self.torp_ends[tp[1]].append((tp[6], victim))
        for v in (*self.shell_ends.values(), *self.torp_ends.values()):
            v.sort(key=lambda e: e[0])          # victims can be None: sort by time only
        self.squadrons = defaultdict(list)      # owner -> squadrons
        for sq in self.d.get('planes') or ():
            self.squadrons[sq['owner']].append(sq)

    def _near_in_time(self, events, t, victim, before=1.6, after=0.4):
        lo = bisect.bisect_left(events, t - before, key=lambda e: e[0])
        out = []
        for e in events[lo:]:
            if e[0] > t + after:
                break
            if e[1] in (victim, None):
                out.append(e)
        return out

    def attribute_damage(self):
        """Each damage row -> its likely source: the attacker's shell / torpedo that ended on the victim just
        before (damage arrives ~0.5 s after the impact), a squadron of the attacker's over the victim, the victim's
        fire / flood (damage over time, credited to whoever started it), a ram."""
        self.dmg = []      # [t, victim, attacker, amount, source]
        rams = {(d[1], d[2]) for d in self.d['deaths'] if d[3] == 'RAM'} | {(d[2], d[1]) for d in self.d['deaths'] if d[3] == 'RAM'}
        for t, victim, attacker, amount in self.d['damage']:
            src = None
            shells = self._near_in_time(self.shell_ends.get(attacker, ()), t, victim)
            if shells:
                src = 'secondary' if all(e[3] for e in shells) else 'main'
            elif self._near_in_time(self.torp_ends.get(attacker, ()), t, victim, 2.5, 0.5):
                src = 'torpedo'
            else:
                vp = self.pos(victim, t, 5)
                for sq in self.squadrons.get(attacker, ()):
                    if sq['start'] - 1 <= t <= (sq['end'] or self.end) + 3 and vp:
                        p = Series(sq['track']).at(t)
                        if p and _dist((p[1], p[2]), vp) < 40:
                            src = 'aircraft'
                            break
                if src is None:
                    f = self.fires.get(victim)
                    flags = (f.at(t) or [0, 0])[1] if f else 0
                    if flags & 0xF:
                        src = 'fire'
                    elif flags & 0x30:
                        src = 'flood'
                    elif (victim, attacker) in rams:
                        src = 'ram'
                    else:
                        src = 'other'
            self.dmg.append([t, victim, attacker, amount, src])

    # ---------------------------------------------------------------- per ship
    def ship_report(self, vid):
        s = self.ships[vid]
        alive = self.alive_until(vid)
        rep = {'alive': _r(alive), 'survived': vid not in self.death}
        dealt, taken = Counter(), Counter()
        to, frm = Counter(), Counter()
        for t, victim, attacker, amount, src in self.dmg:
            if attacker == vid and victim != vid:
                dealt[src] += amount
                to[victim] += amount
            if victim == vid:
                taken[src] += amount
                frm[attacker] += amount
        rep['damage'] = {'dealt': sum(dealt.values()), 'bySource': dict(dealt), 'taken': sum(taken.values()),
                         'takenBySource': dict(taken), 'to': to.most_common(6), 'from': frm.most_common(6)}
        rep['main'] = self.gunnery(vid, alive)
        rep['secondary'] = self.secondaries(vid)
        rep['torpedo'] = self.torps(vid)
        rep['tank'] = self.tanking(vid, alive)
        rep['spot'] = self.spotting(vid, alive) if self.side[vid] == 'ally' else None
        rep['react'] = self.reactions(vid)
        rep['pos'] = self.positioning(vid, alive)
        d = self.death.get(vid)
        if d:
            p = self.pos(vid, d[0], 60)
            team_deaths = sorted(x[0] for x in self.d['deaths'] if self.ships.get(x[1], {}).get('team') == s['team'])
            rep['death'] = {'t': d[0], 'killer': d[2], 'cause': d[3], 'x': p and _r(p[0]), 'z': p and _r(p[1]),
                            'order': team_deaths.index(d[0]) + 1, 'of': sum(1 for x in self.ships.values() if x['team'] == s['team'])}
        else:
            rep['death'] = None
        rep['kills'] = sum(1 for x in self.d['deaths'] if x[2] == vid and x[1] != vid)
        return rep

    def gunnery(self, vid, alive):
        shots = [s for s in self.d['shots'] if s[1] == vid and not s[7] & 0x8000]
        if not shots:
            return None
        kinds = Counter()
        bins = [[lo, hi, 0, 0] for lo, hi in zip(RANGE_BINS, RANGE_BINS[1:] + [None])]
        tracked = hits = 0
        for s in shots:
            rng = math.hypot(s[4] - s[2], s[5] - s[3]) * KM
            b = next(b for b in reversed(bins) if rng >= b[0])
            b[2] += 1
            if s[12] is not None:
                tracked += 1
                if s[12] & SHIP_HIT == SHIP_HIT:
                    hits += 1
                    b[3] += 1
                    kinds[HIT_KINDS.get(s[12], 'shatter')] += 1
        # salvos: shells within 1.5 s; reload: per gun, the usual gap between its firings
        times = sorted(s[0] for s in shots)
        salvos = 1 + sum(1 for a, b in zip(times, times[1:]) if b - a > 1.5)
        per_gun = defaultdict(set)
        for s in shots:
            per_gun[s[7] & 31].add(round(s[0], 1))
        gaps = []
        for ts in per_gun.values():
            ts = sorted(ts)
            gaps += [b - a for a, b in zip(ts, ts[1:]) if b - a > 2]
        reload = None
        if len(gaps) >= 4:
            gaps.sort()
            reload = gaps[len(gaps) // 5]           # 20th percentile: firing as soon as reloaded
        use = None
        if reload and alive > 0:
            fired = sum(len(v) for v in per_gun.values())
            use = min(1.0, fired / (len(per_gun) * max(1.0, (alive - min(times) + reload) / reload)))
        return {'shells': len(shots), 'salvos': salvos, 'tracked': tracked, 'hits': hits,
                'kinds': dict(kinds), 'byRange': [b for b in bins if b[2]], 'reload': _r(reload),
                'gunUse': _r(use, 3), 'firstShot': _r(min(times))}

    def secondaries(self, vid):
        shots = [s for s in self.d['shots'] if s[1] == vid and s[7] & 0x8000]
        if not shots:
            return None
        return {'shells': len(shots), 'tracked': sum(1 for s in shots if s[12] is not None),
                'hits': sum(1 for s in shots if s[12] is not None and s[12] & SHIP_HIT == SHIP_HIT)}

    def torps(self, vid):
        ts = [tp for tp in self.d['torpedoes'] if tp[1] == vid]
        if not ts:
            return None
        return {'launched': len(ts), 'tracked': sum(1 for tp in ts if tp[9] is not None),
                'hits': sum(1 for tp in ts if tp[9] is not None and tp[9] & SHIP_HIT == SHIP_HIT)}

    def tanking(self, vid, alive):
        """Shells aimed at the ship, hits it took and at what angle it showed the shooter."""
        aimed = 0
        for s in self.d['shots']:
            if s[7] & 0x8000 or self.side.get(s[1]) == self.side[vid] or s[0] > alive:
                continue
            land = s[0] + (s[6] or 0)
            p = self.pos(vid, land, 5)
            if p and math.hypot(s[4] - p[0], s[5] - p[1]) < 10 + self.half_len[vid]:      # within ~300 m
                aimed += 1
        angles = [0] * len(ANGLE_BINS)
        cit_angles = [0] * len(ANGLE_BINS)
        hits = 0
        for end_t, shooter, ht, x0, z0, secondary in self.shell_hits_on.get(vid, ()):
            if secondary or self.side.get(shooter) == self.side[vid]:
                continue
            p = self.pos(vid, end_t, 5)
            if not p:
                continue
            hits += 1
            rel = abs(math.degrees(_wrap(math.atan2(x0 - p[0], z0 - p[1]) - p[2])))
            i = next(i for i, (_n, hi) in enumerate(ANGLE_BINS) if rel < hi)
            angles[i] += 1
            if ht == 0x62:
                cit_angles[i] += 1
        return {'shellsAimed': aimed, 'hits': hits, 'angles': angles, 'citAngles': cit_angles,
                'broadside': _r(angles[2] / hits, 3) if hits else None}

    def spotting(self, vid, alive):
        """Time the ship was spotted (its visibilityFlags), salvos it opened from concealment, time spotted while
        neither shooting nor launching torpedoes."""
        det = self.detected.get(vid)
        if not det:
            return None
        spotted, first = 0.0, None
        rows = [r for r in det.rows if r[0] <= alive]
        for (t0, f), nxt in zip(rows, rows[1:] + [[alive, 0]]):
            if f:
                spotted += max(0.0, nxt[0] - t0)
                first = t0 if first is None else first
        fire = sorted({round(s[0]) for s in self.d['shots'] if s[1] == vid and not s[7] & 0x8000} |
                      {round(tp[0]) for tp in self.d['torpedoes'] if tp[1] == vid})
        salvo_starts = [t for i, t in enumerate(fire) if i == 0 or t - fire[i - 1] > 3]
        hidden = sum(1 for t in salvo_starts if not (det.at(t - 0.5) or [0, 0])[1])
        idle = 0.0
        for t in range(0, int(alive), 2):
            if (det.at(t) or [0, 0])[1]:
                i = bisect.bisect_right(fire, t)
                if i == 0 or t - fire[i - 1] > 20:
                    idle += 2
        return {'spotted': round(spotted), 'share': _r(spotted / alive, 3) if alive else None,
                'firstSpotted': _r(first), 'openedHidden': hidden, 'openings': len(salvo_starts), 'idleSpotted': round(idle)}

    def reactions(self, vid):
        """Fires / floods taken and how long each damage-control use came after the burning / flooding began."""
        f = self.fires.get(vid)
        cons = [c for c in self.d['consumables'] if c[1] == vid]
        out = {'fires': 0, 'floods': 0, 'dcp': [], 'dcpMedian': None,
               'heals': sum(1 for c in cons if c[2] == 'Repair party')}
        if not f:
            return out
        prev, began = 0, None
        for t, flags in f.rows:
            new = flags & ~prev
            out['fires'] += bin(new & 0xF).count('1')
            out['floods'] += bin(new & 0x30).count('1')
            if flags and not prev:
                began = t
            prev = flags
            if not flags:
                began = None
        for c in cons:
            if c[2] != 'Damage control':
                continue
            row = f.at(c[0] + 0.01)
            before = f.at(c[0] - 0.01)
            flags = (before or [0, 0])[1]
            if flags:
                start = c[0]
                for t, fl in reversed(f.rows[:bisect.bisect_right(f.ts, c[0] - 0.01)]):
                    if not fl:
                        break
                    start = t
                out['dcp'].append([c[0], _r(c[0] - start)])
        out['dcpMedian'] = _r(_median([x[1] for x in out['dcp']]))
        return out

    def positioning(self, vid, alive):
        """Average distance to the nearest known enemy and to the own team's centre, every 5 s while alive."""
        near, centre = [], []
        for t in range(5, int(alive), 5):
            me = self.pos(vid, t, 10)
            if not me:
                continue
            enemies = [p for e in self.ships if self.side[e] != self.side[vid] and t <= self.alive_until(e)
                       for p in [self.pos(e, t, 10)] if p]
            if enemies:
                near.append(min(_dist(me, p) for p in enemies) * KM)
            mates = [p for e in self.ships if e != vid and self.side[e] == self.side[vid] and t <= self.alive_until(e)
                     for p in [self.pos(e, t, 10)] if p]
            if mates:
                cx, cz = sum(p[0] for p in mates) / len(mates), sum(p[1] for p in mates) / len(mates)
                centre.append(math.hypot(me[0] - cx, me[1] - cz) * KM)
        return {'nearestEnemy': _r(sum(near) / len(near), 2) if near else None,
                'teamCentre': _r(sum(centre) / len(centre), 2) if centre else None}

    # ---------------------------------------------------------------- battle flow
    def flow(self, step=5):
        """Each side's remaining HP (share of its total) and ships alive every `step` s, and the turning points."""
        sides = {'ally': [v for v in self.ships if self.side[v] == 'ally'],
                 'enemy': [v for v in self.ships if self.side[v] == 'enemy']}
        total = {k: sum(self.max_hp[v] for v in vs) or 1 for k, vs in sides.items()}
        ts, hp, alive = [], [], []
        t = 0.0
        while True:
            row_hp, row_alive = [], []
            for k in ('ally', 'enemy'):
                s = n = 0
                for v in sides[k]:
                    if t >= self.alive_until(v) and v in self.death:
                        continue
                    n += 1
                    h = self.health.get(v)
                    r = h.at(t) if h else None
                    s += r[1] if r else (self.ships[v]['maxHealth'] or 0)   # listed max until the first update
                row_hp.append(round(s / total[k], 4))
                row_alive.append(n)
            ts.append(round(t, 1))
            hp.append(row_hp)
            alive.append(row_alive)
            if t >= self.end:
                break
            t = min(self.end, t + step)
        # turning points: the 60 s windows where the HP balance (ally share - enemy share) swung most
        bal = [a - e for a, e in hp]
        w = max(1, int(60 / step))
        swings = sorted(((bal[i + w] - bal[i], i) for i in range(len(bal) - w)), key=lambda x: -abs(x[0]))
        turns, used = [], []
        for sw, i in swings:
            if abs(sw) < .08 or len(turns) >= 4:
                break
            if any(abs(i - j) < w for j in used):
                continue
            used.append(i)
            t0, t1 = ts[i], ts[i + w]
            kills = [d for d in self.d['deaths'] if t0 <= d[0] <= t1]
            turns.append({'t': t0, 'end': t1, 'swing': round(sw, 3), 'for': 'ally' if sw > 0 else 'enemy',
                          'kills': [[d[0], d[1], d[2]] for d in kills]})
        turns.sort(key=lambda x: x['t'])
        lead = [{'t': ts[i], 'to': 'ally' if bal[i] > 0 else 'enemy'}
                for i in range(1, len(bal)) if (bal[i] > 0) != (bal[i - 1] > 0) and abs(bal[i]) > .01]
        return {'t': ts, 'hp': hp, 'alive': alive, 'total': total, 'turns': turns, 'leadChanges': lead}

    def fight_list(self):
        """Clusters of damage close in time and place: who fought, where, and how it went."""
        fights = []
        for t, victim, attacker, amount, src in self.dmg:
            if src in ('fire', 'flood') or attacker == victim:
                continue
            v, a = self.pos(victim, t, 15), self.pos(attacker, t, 15)
            if not v and not a:
                continue
            # midway between shooter and target: both sides of an exchange land in the same fight
            p = ((v[0] + a[0]) / 2, (v[1] + a[1]) / 2) if v and a else (v or a)
            f = next((f for f in reversed(fights) if t - f['end'] < 30 and _dist(p, (f['x'], f['z'])) < 150), None)
            if not f:
                f = {'start': t, 'end': t, 'x': p[0], 'z': p[1], 'n': 0, 'dealt': {'ally': 0, 'enemy': 0}, 'ships': set()}
                fights.append(f)
            f['end'] = t
            f['n'] += 1
            f['x'] += (p[0] - f['x']) / f['n']
            f['z'] += (p[1] - f['z']) / f['n']
            if attacker in self.side:
                f['dealt'][self.side[attacker]] += amount
                f['ships'].add(attacker)
            f['ships'].add(victim)
        caps = [z for z in self.d['zones'] if z.get('kind') == 'capture' and z.get('index') is not None]
        out = []
        for f in fights:
            if f['dealt']['ally'] + f['dealt']['enemy'] < 10000:
                continue
            kills = [d for d in self.d['deaths'] if f['start'] - 5 <= d[0] <= f['end'] + 5 and d[1] in f['ships']]
            near = min(caps, key=lambda z: math.hypot(z['x'] - f['x'], z['z'] - f['z']), default=None)
            near_cap = chr(65 + near['index']) if near and math.hypot(near['x'] - f['x'], near['z'] - f['z']) < near['radius'] + 100 else None
            out.append({'start': f['start'], 'end': f['end'], 'x': _r(f['x']), 'z': _r(f['z']), 'cap': near_cap,
                        'dealt': f['dealt'], 'ships': sorted(f['ships']),
                        'lost': {'ally': sum(1 for d in kills if self.side.get(d[1]) == 'ally'),
                                 'enemy': sum(1 for d in kills if self.side.get(d[1]) == 'enemy')}})
        return out

    def highlight_list(self):
        """Moments worth a look: citadels, torpedo hits, big hits, multi-kills, first blood, the last kill."""
        out = []
        for shooter, ends in self.shell_ends.items():
            cits = [e for e in ends if e[2] == 0x62]
            for group in _group(cits, 3):
                out.append([group[0][0], 'citadel', shooter, group[0][1], len(group)])
        for shooter, ends in self.torp_ends.items():
            for group in _group(ends, 5):
                out.append([group[0][0], 'torpedo', shooter, group[0][1], len(group)])
        for t, victim, attacker, amount, src in self.dmg:
            mx = self.ships.get(victim, {}).get('maxHealth') or 0
            if mx and amount >= .3 * mx and attacker != victim:
                out.append([t, 'big hit', attacker, victim, amount])
        kills = sorted((d for d in self.d['deaths'] if d[2] and d[2] != d[1]), key=lambda d: d[0])
        by_killer = defaultdict(list)
        for d in kills:
            by_killer[d[2]].append(d)
        for killer, ds in by_killer.items():
            for group in _group([(d[0], d[1]) for d in ds], 60):
                if len(group) >= 2:
                    out.append([group[-1][0], 'multi-kill', killer, group[-1][1], len(group)])
        if kills:
            out.append([kills[0][0], 'first blood', kills[0][2], kills[0][1], 1])
            if len(kills) > 1:
                out.append([kills[-1][0], 'last kill', kills[-1][2], kills[-1][1], 1])
        out.sort(key=lambda h: h[0])
        return [[_r(h[0]), h[1], h[2], h[3], h[4]] for h in out]

    def aircraft(self):
        """Squadrons per owner, planes lost, damage by aircraft; planes shot down attributed to the nearest enemy
        ship (approximate: the replay doesn't say who shot a plane down)."""
        owners, aa = {}, Counter()
        dmg_air = Counter()
        for t, victim, attacker, amount, src in self.dmg:
            if src == 'aircraft':
                dmg_air[attacker] += amount
        for owner, sqs in self.squadrons.items():
            lost = sum(c or 0 for sq in sqs for _t, c in sq['losses'])
            owners[str(owner)] = {'squadrons': len(sqs), 'planes': sum(sq['n'] or 0 for sq in sqs), 'lost': lost,
                                  'damage': dmg_air.get(owner, 0)}
            for sq in sqs:
                tr = Series(sq['track'])
                for t, c in sq['losses']:
                    p = tr.at(t)
                    if not p:
                        continue
                    best = None
                    for v in self.ships:
                        if self.ships[v]['team'] == sq['team'] or t > self.alive_until(v):
                            continue
                        q = self.pos(v, t, 10)
                        if q:
                            d = _dist((p[1], p[2]), q)
                            if d < 200 and (best is None or d < best[0]):
                                best = (d, v)
                    if best:
                        aa[best[1]] += c or 0
        return {'owners': owners, 'aa': {str(k): v for k, v in aa.items()}} if owners else None

    # ---------------------------------------------------------------- one line per battle (library trends)
    def personal(self, out):
        own = self.d['meta'].get('ownVehicle')
        if own not in self.ships:
            return None
        s, r = self.ships[own], out['ships'][str(own)]
        res = self.d['meta'].get('result') or {}
        g, tp, tk, sp = r['main'] or {}, r['torpedo'] or {}, r['tank'], r['spot'] or {}
        return {
            'ship': s.get('ship'), 'species': s.get('species'), 'level': s.get('level'), 'map': self.d['meta'].get('map'),
            'win': None if not res else (res.get('winnerTeamId') == self.d['meta'].get('ownTeam') and 1)
            or (-1 if (res.get('winnerTeamId') or 0) < 0 else 0),
            'dealt': r['damage']['dealt'], 'taken': r['damage']['taken'], 'kills': r['kills'],
            'shells': g.get('shells', 0), 'tracked': g.get('tracked', 0), 'hits': g.get('hits', 0),
            'cit': (g.get('kinds') or {}).get('cit', 0), 'torps': tp.get('launched', 0), 'torpHits': tp.get('hits', 0),
            'spotted': sp.get('share'), 'broadside': tk['broadside'], 'nearestEnemy': r['pos']['nearestEnemy'],
            'teamCentre': r['pos']['teamCentre'],
            'survived': r['survived'], 'alive': r['alive'], 'end': _r(self.end),
            'death': r['death'] and {k: r['death'][k] for k in ('t', 'x', 'z', 'cause', 'order', 'of')},
        }


def _group(events, gap):
    """Split time-sorted events (t, ...) into runs whose neighbours are at most `gap` s apart."""
    groups = []
    for e in sorted(events, key=lambda e: e[0]):
        if groups and e[0] - groups[-1][-1][0] <= gap:
            groups[-1].append(e)
        else:
            groups.append([e])
    return groups
