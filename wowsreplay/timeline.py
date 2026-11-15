"""Turn a replay into a compact timeline for the minimap viewer.

All times are seconds since replay start (packet clock). World coordinates
are BigWorld units on the x/z plane; +z is north (up on the minimap) and
yaw 0 points along +z, so heading = (sin yaw, cos yaw).
"""
import json
import math
import os
import struct
from collections import Counter, defaultdict

from . import nested, packets as P, safe_pickle
from .container import read_replay
from .defs import Definitions, Reader

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
MINIMAP_STEP = 5000 / 2047    # minimap packs x/z into 11 bits over [-2500, 2500]
AIM_RANGE_STEP = 5.92         # targetLocalPos high byte -> world units (median over ~8k shells, 3 replays)
# serverSpeedRaw per knot of displayed speed, approximate (+-3%): maxServerSpeedRaw is ~26250 x base knots and
# ~5050 x the serverSpeedRaw of ships running at that cap (Thunderer replay), so 26250 / 5050 ~ 5.2
SPEED_RAW_PER_KNOT = 5.2
RUDDER_FULL = math.pi / 6     # ruddersAngle at full rudder (values seen: 0, +-pi/12, +-pi/6, ...)


def version_dir(version: tuple) -> str:
    name = '_'.join(str(v) for v in version[:3])
    path = os.path.join(DATA_DIR, name)
    if not os.path.isdir(path):
        have = ', '.join(sorted(d for d in os.listdir(DATA_DIR) if d[:1].isdigit())) or 'none'
        raise RuntimeError(f'no data for game version {name} (have: {have}); '
                           f'import it with tools/import_version.py')
    return path


def unpack_minimap(packed: int):
    """updateMinimapVisionInfo packedData -> (x, z, yaw), or None when hidden.

    Bits: 0-10 x, 11-21 z, 22-29 yaw (0..255 over -pi..pi), 31 hidden flag.
    """
    if packed >> 31:
        return None
    x = (packed & 0x7FF) * MINIMAP_STEP - 2500
    z = ((packed >> 11) & 0x7FF) * MINIMAP_STEP - 2500
    yaw = ((packed >> 22) & 0xFF) / 255 * 2 * math.pi - math.pi
    return x, z, yaw


def _str(v):
    return v.decode('utf-8', 'replace') if isinstance(v, bytes) else v


def _r(v, nd=2):
    return round(v, nd)


class TimelineBuilder:
    ROSTER_FIELDS = ('name', 'clanTag', 'shipId', 'shipParamsId', 'teamId', 'maxHealth',
                     'avatarId', 'accountDBID')

    def __init__(self, replay, data_dir):
        self.replay = replay
        self.defs = Definitions(os.path.join(data_dir, 'scripts'))
        with open(os.path.join(data_dir, 'constants.json'), encoding='utf-8') as f:
            self.consts = json.load(f)
        with open(os.path.join(data_dir, 'ships.json'), encoding='utf-8') as f:
            self.ship_params = json.load(f)
        with open(os.path.join(DATA_DIR, 'consumables.json'), encoding='utf-8') as f:
            self.consumable_ids = json.load(f)['ids']

        self.entities = {}                 # entity id -> EntityDef
        self.values = defaultdict(dict)    # entity id -> {prop name: value}
        self.avatar_id = None
        self.roster = {}                   # vehicle id -> roster dict
        self.errors = Counter()

        self.tracks = defaultdict(list)    # vid -> [t, x, z, yaw]
        self.minimap = defaultdict(list)   # vid -> [t, x, z, yaw]
        self.aoi = defaultdict(list)       # vid -> [enter_t, leave_t | None]
        self.health = defaultdict(list)    # vid -> [t, hp]
        self.detected = defaultdict(list)  # vid -> [t, visibilityFlags]
        # vid -> [t, packed aim point] from Vehicle.targetLocalPos / torpedoLocalPos (u16):
        # low byte = world bearing, (lo + .5) / 256 * 2pi + pi (yaw convention, 0 = +z);
        # high byte = range, (hi + .5) * AIM_RANGE_STEP world units. Fitted to shell landing points.
        self.aim = defaultdict(list)
        self.torp_aim = defaultdict(list)
        self.deaths = []                   # [t, victim, killer, reason]
        self.zones = {}                    # zone id -> dict
        self.score = []                    # [t, team0, team1]
        self.clock = []                    # [t, timeLeft]
        self.camera = []                   # [t, x, y, z, dirx, diry, dirz, fov]
        self.view_mode = []                # [t, mode]
        self.target_lock = []              # [t, vid or 0]
        self.controls = []                 # [t, kind, level]
        self.shots = []                    # [t, owner, x0, z0, x1, z1, flight_s, gun, pitch_rad]
        # [t, owner, x, z, dirx, dirz, end_t, end_x, end_z, end_hitType, path]: dir is per second; end from
        # receiveShotKills (None if never seen); path = [[t, x, z]] course points of maneuvering / homing torpedoes
        self.torpedoes = []
        self.speed = defaultdict(list)     # vid -> [t, knots]  (serverSpeedRaw / SPEED_RAW_PER_KNOT, < 0 astern)
        self.rudder = defaultdict(list)    # vid -> [t, rudder -1..1]  (ruddersAngle / 30 deg, + = starboard)
        self._torp = {}                    # (owner, shotID) -> torpedo row still running
        self.chat = []                     # [t, sender, channel, text]
        self.consumables = []              # [t, vid, label, duration_s, range | None]
        self.smokes = {}                   # smoke entity -> {radius, frames: [[t, [x, z, ...]]], end}
        self.damage = []                   # [t, victim, attacker, amount]
        self.fires = defaultdict(list)     # vid -> [t, burningFlags]: bits 0-3 burn nodes (fires), bits 4-5 flood nodes
        self.hits = []                     # [t, shooter, hitType, x, z]  shell endings (m_Avatar_receiveShotKills)
        self.explosions = []               # [t, x, z]
        self._eid = None                   # entity of the method being handled
        # squadrons by planeID (low 32 bits = owning ship): {id, owner, team, params, n,
        #   track [[t, x, z, y | None]], losses [[t, count]], start, end}
        self.planes = {}
        self.depth = defaultdict(list)     # vid -> [t, buoyancyCurrentWaterline] (submarines, < 0 = submerged)
        self.battle_result = None
        self.win_score = None

    # ------------------------------------------------------------ main loop

    def run(self):
        handlers = {
            P.BASE_PLAYER_CREATE: self.on_base_player,
            P.CELL_PLAYER_CREATE: self.on_cell_player,
            P.ENTITY_CREATE: self.on_entity_create,
            P.ENTITY_LEAVE: self.on_entity_leave,
            P.ENTITY_PROPERTY: self.on_property,
            P.ENTITY_METHOD: self.on_method,
            P.NESTED_PROPERTY: self.on_nested,
            P.POSITION: self.on_position,
            P.PLAYER_POSITION: self.on_player_position,
            P.CAMERA: self.on_camera,
            P.VIEW_MODE: self.on_view_mode,
            P.TARGET_LOCK: self.on_target_lock,
            P.STEERING: self.on_steering,
        }
        last_t = 0.0
        for pkt in P.iter_packets(self.replay.stream):
            if pkt.type != P.END_OF_STREAM:
                last_t = max(last_t, pkt.time)
            handler = handlers.get(pkt.type)
            if handler:
                try:
                    handler(pkt.time, pkt.body)
                except Exception as e:  # keep going: one bad packet must not kill the replay
                    self.errors[f'{hex(pkt.type)} {type(e).__name__}: {e}'[:160]] += 1
        self.end_time = last_t
        return self.result()

    # ------------------------------------------------------------ entities

    def on_base_player(self, t, b):
        eid, type_idx = struct.unpack_from('<ih', b)
        self.entities[eid] = self.defs.entity_by_index(type_idx)
        self.avatar_id = eid

    def on_cell_player(self, t, b):
        eid, = struct.unpack_from('<i', b)
        self.entities.setdefault(eid, self.defs.entity('Avatar'))

    def on_entity_create(self, t, b):
        eid, type_idx, _vehicle, _space, x, y, z = struct.unpack_from('<ihii3f', b)
        edef = self.entities[eid] = self.defs.entity_by_index(type_idx)
        r = Reader(b, 38)
        state = Reader(r.read(r.unpack('<I')[0]))
        for _ in range(state.unpack('<B')[0]):
            prop = edef.client_props[state.unpack('<B')[0]]
            self.values[eid][prop.name] = prop.type.decode(state)

        vals = self.values[eid]
        if edef.name == 'Vehicle':
            self.aoi[eid].append([_r(t), None])
            if 'health' in vals:
                self.health[eid].append([_r(t), _r(vals['health'], 0)])
            self.detected[eid].append([_r(t), vals.get('visibilityFlags', 0)])
        elif edef.name == 'InteractiveZone':
            cap = (vals.get('componentsState') or {}).get('captureLogic') or {}
            cp = (vals.get('componentsState') or {}).get('controlPoint') or {}
            self.zones[eid] = {
                'id': eid, 'x': _r(x), 'z': _r(z), 'radius': _r(vals.get('radius', 0)),
                'index': cp.get('index'),
                'team': [[_r(t), vals.get('teamId')]],
                'capture': [[_r(t), _r(cap.get('progress', 0), 3), cap.get('invaderTeam', -1)]],
            }
        elif edef.name == 'BattleLogic':
            self._record_battle_logic(t, vals)
        elif edef.name == 'SmokeScreen':
            self.smokes[eid] = {'id': eid, 'radius': _r(vals.get('radius', 15)), 'frames': [], 'end': None}
            self._smoke_frame(t, eid)
        if edef.name == 'Vehicle' and vals.get('burningFlags'):
            self.fires[eid].append([_r(t), vals['burningFlags']])

    def on_entity_leave(self, t, b):
        eid, = struct.unpack_from('<i', b)
        spans = self.aoi.get(eid)
        if spans and spans[-1][1] is None:
            spans[-1][1] = _r(t)
        if eid in self.smokes and self.smokes[eid]['end'] is None:
            self.smokes[eid]['end'] = _r(t)

    def on_property(self, t, b):
        eid, idx, size = struct.unpack_from('<III', b)
        edef = self.entities.get(eid)
        if edef is None:
            return
        prop = edef.client_props[idx]
        value = prop.type.decode(Reader(b, 12))
        self.values[eid][prop.name] = value
        key = (edef.name, prop.name)
        if key == ('Vehicle', 'health'):
            self.health[eid].append([_r(t), _r(value, 0)])
        elif key == ('Vehicle', 'visibilityFlags'):
            self.detected[eid].append([_r(t), value])
        elif key == ('Vehicle', 'buoyancyCurrentWaterline'):
            series = self.depth[eid]
            if not series or abs(series[-1][1] - value) >= .05 or t - series[-1][0] > 5:
                series.append([_r(t), _r(value, 3)])
        elif key == ('Vehicle', 'serverSpeedRaw'):
            # speedSignDir -1 = going astern; it can flip a tick before the speed it belongs to, so it is only
            # read when a new speed arrives
            v = self.values[eid]
            series = self.speed[eid]
            kn = _r(v.get('serverSpeedRaw', 0) / SPEED_RAW_PER_KNOT * (-1 if (v.get('speedSignDir') or 1) < 0 else 1), 1)
            if not series or abs(series[-1][1] - kn) >= .5 or (t - series[-1][0] > 5 and series[-1][1] != kn):
                series.append([_r(t), kn])
        elif key == ('Vehicle', 'ruddersAngle'):
            series, r = self.rudder[eid], _r(value / RUDDER_FULL, 2)
            if not series or series[-1][1] != r:
                series.append([_r(t), r])
        elif key == ('Vehicle', 'burningFlags'):
            if not self.fires[eid] or self.fires[eid][-1][1] != value:
                self.fires[eid].append([_r(t), value])
        elif edef.name == 'SmokeScreen' and prop.name in ('points', 'radius') and eid in self.smokes:
            if prop.name == 'radius':
                self.smokes[eid]['radius'] = _r(value)
            self._smoke_frame(t, eid)
        elif key in (('Vehicle', 'targetLocalPos'), ('Vehicle', 'torpedoLocalPos')):
            series = (self.aim if prop.name == 'targetLocalPos' else self.torp_aim)[eid]
            if not series or series[-1][1] != value:
                series.append([_r(t), value])
        elif key == ('BattleLogic', 'timeLeft'):
            self.clock.append([_r(t), value])
        elif key == ('BattleLogic', 'battleResult'):
            self.battle_result = value
        elif key == ('InteractiveZone', 'teamId') and eid in self.zones:
            self.zones[eid]['team'].append([_r(t), value])

    def on_nested(self, t, b):
        eid, = struct.unpack_from('<I', b)
        edef = self.entities.get(eid)
        if edef is None or edef.name == 'Avatar':   # Avatar cell props are not decoded
            return
        _, path = nested.apply(b, edef, self.values[eid])
        if edef.name == 'BattleLogic' and path[:3] == ['state', 'missions', 'teamsScore']:
            self._record_battle_logic(t, self.values[eid])
        elif edef.name == 'SmokeScreen' and path[:1] == ['points'] and eid in self.smokes:
            self._smoke_frame(t, eid)
        elif edef.name == 'InteractiveZone' and path[:2] == ['componentsState', 'captureLogic'] \
                and eid in self.zones:
            cap = self.values[eid]['componentsState']['captureLogic']
            self.zones[eid]['capture'].append([_r(t), _r(cap['progress'], 3), cap['invaderTeam']])

    def _record_battle_logic(self, t, vals):
        missions = (vals.get('state') or {}).get('missions') or {}
        scores = {s['teamId']: s['score'] for s in missions.get('teamsScore', [])}
        if scores:
            self.score.append([_r(t), scores.get(0, 0), scores.get(1, 0)])
        if missions.get('teamWinScore'):
            self.win_score = missions['teamWinScore']
        if 'timeLeft' in vals and not self.clock:
            self.clock.append([_r(t), vals['timeLeft']])

    # ------------------------------------------------------------ methods

    def on_method(self, t, b):
        eid, idx, size = struct.unpack_from('<III', b)
        edef = self.entities.get(eid)
        if edef is None:
            return
        method = edef.client_methods[idx]
        handler = getattr(self, f'm_{edef.name}_{method.name}', None)
        if handler:
            self._eid = eid
            handler(t, method.decode(Reader(b, 12)))

    def m_Avatar_onArenaStateReceived(self, t, a):
        self._merge_roster(a['playersStates'], 'id_property_map', False)
        self._merge_roster(a['botsStates'], 'id_property_map_bots', True)

    def m_Avatar_onGameRoomStateChanged(self, t, a):
        players, bots = (a['playersData'], a['botsData']) if isinstance(a, dict) else a[:2]
        self._merge_roster(players, 'id_property_map', False)
        self._merge_roster(bots, 'id_property_map_bots', True)

    m_Avatar_onNewPlayerSpawnedInBattle = m_Avatar_onGameRoomStateChanged

    def _merge_roster(self, blob, map_name, is_bot):
        keys = self.consts[map_name]
        for entry in safe_pickle.loads(blob):
            if entry and max(k for k, _ in entry) >= len(keys):   # game added keys: names would shift
                self.errors[f'{map_name}: record has key {max(k for k, _ in entry)}, map has {len(keys)}'] += 1
                continue
            info = {keys[str(k)]: _str(v) for k, v in entry if str(k) in keys}
            vid = info.get('shipId')
            if not vid:
                continue
            row = self.roster.setdefault(vid, {'isBot': is_bot})
            row.update({k: info[k] for k in self.ROSTER_FIELDS if k in info})

    def m_Avatar_receiveVehicleDeath(self, t, a):
        victim, killer, reason = a
        self.deaths.append([_r(t), victim, killer, self.consts['DEATH_TYPES'].get(str(reason), str(reason))])

    def m_Avatar_receiveArtilleryShots(self, t, a):
        for pack in a[0]:
            for s in pack['shots']:
                (x0, _, z0), (x1, _, z1) = s['pos'], s['tarPos']
                flight = s['serverTimeLeft'] if s['serverTimeLeft'] > 0 else 2.0
                # gunBarrelID: 0x8000 = secondary (ATBA), & 31 = gun index (HP_xxx_<n> - 1), >> 5 = barrel
                gun = s.get('gunBarrelID', 0) & 0x801F
                self.shots.append([_r(t), pack['ownerID'], _r(x0), _r(z0), _r(x1), _r(z1), _r(flight),
                                   gun, _r(s.get('pitch', 0.0), 3)])

    def _smoke_frame(self, t, eid):
        pts = [v for x, _y, z in self.values[eid].get('points') or () for v in (_r(x), _r(z))]
        frames = self.smokes[eid]['frames']
        if not frames or frames[-1][1] != pts:
            frames.append([_r(t), pts])

    # consumable names (ModSDK consumables_mapping.txt) -> GameParams consumableType keys where they differ
    CONSUMABLE_ALIASES = {'tareloader': 'torpedoreloader', 'mbbooster': 'artilleryboosters'}
    CONSUMABLE_LABELS = {'CRASH_CREW': 'Damage control', 'REGEN_CREW': 'Repair party', 'SONAR': 'Hydro',
                         'RLS': 'Radar', 'HYDROPHONE': 'Hydrophone', 'FIGHTER': 'Fighter', 'SCOUT': 'Spotter',
                         'SPEED_BOOSTER': 'Speed boost', 'SMOKE_GENERATOR': 'Smoke',
                         'MB_BOOSTER': 'Main battery reload', 'FAST_RUDDERS': 'Rudder shift',
                         'AUX_TORP_BOOSTER': 'Torpedo boost', 'SUBMARINE_LOCATOR': 'Sub surveillance',
                         'TA_RELOADER': 'Torpedo reload', 'AIR_DEFENSE_DISP': 'Defensive AA'}

    def m_Vehicle_onConsumableUsed(self, t, a):
        # consumableUsageParams: 2 bytes, [1, type id]; workTimeLeft: how long it stays active
        usage = a['consumableUsageParams']
        name = self.consumable_ids.get(str(usage[-1]), f'CONSUMABLE_{usage[-1]}')[len('CONSUMABLE_'):]
        params = self.ship_params.get(str(self.roster.get(self._eid, {}).get('shipParamsId')), {})
        norm = lambda k: k.lower().replace('_', '').rstrip('s')
        want = norm(self.CONSUMABLE_ALIASES.get(name.lower().replace('_', ''), name))
        info = next((v for k, v in (params.get('consumables') or {}).items() if norm(k) == want), {})
        label = self.CONSUMABLE_LABELS.get(name, name.replace('_', ' ').capitalize())
        self.consumables.append([_r(t), self._eid, label, _r(a['workTimeLeft']), info.get('range')])

    def m_Vehicle_receiveDamagesOnShip(self, t, a):
        # sent to the damaged ship: [{vehicleID: attacker, damage}]
        for d in a[0]:
            self.damage.append([_r(t), self._eid, d['vehicleID'], round(d['damage'])])

    def m_Avatar_receiveShotKills(self, t, a):
        # where shells ended. hitType, worked out from where they land: 0x60 | n = on a ship
        # (n = kind of hit, not mapped), 0x40 = above water away from ships (terrain),
        # 0x20 / 0x22 = water, 0 = expired
        for pack in a[0]:
            for k in pack['kills']:
                x, _y, z = k['pos']
                self.hits.append([_r(t), pack['ownerID'], pack['hitType'], _r(x), _r(z)])
                row = self._torp.pop((pack['ownerID'], k['shotID']), None)
                if row:
                    row[6:10] = [_r(t), _r(x), _r(z), pack['hitType']]

    def m_Avatar_receiveExplosions(self, t, a):
        for e in a[0]:
            x, _y, z = e['pos']
            self.explosions.append([_r(t), _r(x), _r(z)])

    def _plane(self, t, pid, team=None, params=None):
        sq = self.planes.get(pid)
        if sq is None or (sq['end'] is not None and t > sq['end'] + 1):
            sq = self.planes[pid] = {'id': pid, 'owner': pid & 0xFFFFFFFF, 'team': team, 'params': params,
                                     'n': None, 'track': [], 'losses': [], 'start': _r(t), 'end': None}
        if team is not None:
            sq['team'] = team
        if params is not None:
            sq['params'] = params
        return sq

    def _plane_pos(self, t, pid, x, z, y=None):
        tr = self._plane(t, pid)['track']
        if tr and t - tr[-1][0] < .5 and (y is None or tr[-1][3] is not None):
            return                                          # ~2 points/s is plenty
        tr.append([_r(t), _r(x), _r(z), None if y is None else _r(y)])

    def m_Avatar_receive_addMinimapSquadron(self, t, a):
        pid, team, params, (x, z) = a[0], a[1], a[2], a[3]
        self._plane(t, pid, team, params)
        self._plane_pos(t, pid, x, z)

    def m_Avatar_receive_updateMinimapSquadron(self, t, a):
        self._plane_pos(t, a[0], *a[1])

    def m_Avatar_receive_removeMinimapSquadron(self, t, a):
        sq = self.planes.get(a[0])
        if sq and sq['end'] is None:
            sq['end'] = _r(t)

    def m_Avatar_receive_addSquadron(self, t, a):
        st = a['squadronState']
        sq = self._plane(t, st['planeID'], params=a['paramsID'])
        sq['n'] = st.get('numPlanes')
        x, y, z = st['position']
        self._plane_pos(t, st['planeID'], x, z, y)

    def m_Avatar_receive_updateSquadron(self, t, a):
        if a[2]:
            x, y, z = a[2][-1]['position']
            self._plane_pos(t, a[0], x, z, y)

    def m_Avatar_receive_removeSquadron(self, t, a):
        self.m_Avatar_receive_removeMinimapSquadron(t, a)

    def m_Avatar_receive_planeDeath(self, t, a):
        sq = self.planes.get(a[0])
        if sq:
            sq['losses'].append([_r(t), len(a[1])])

    def m_Avatar_receiveTorpedoes(self, t, a):
        for pack in a[0]:
            for tp in pack['torpedoes']:
                (x, _, z), (dx, _, dz) = tp['pos'], tp['dir']
                row = [_r(t), pack['ownerID'], _r(x), _r(z), _r(dx, 3), _r(dz, 3), None, None, None, None, []]
                self.torpedoes.append(row)
                self._torp[(pack['ownerID'], tp['shotID'])] = row

    def _torp_point(self, t, owner, shot, pos):
        row = self._torp.get((owner, shot))
        if row:
            row[10].append([_r(t), _r(pos[0]), _r(pos[2])])

    def m_Avatar_receiveTorpedoDirection(self, t, a):     # owner, shotID, pos, ... (maneuvering / homing)
        self._torp_point(t, a[0], a[1], a[2])

    def m_Avatar_receiveTorpedoManeuverEnd(self, t, a):   # owner, shotID, pos, yaw, ...
        self._torp_point(t, a[0], a[1], a[2])

    def m_Avatar_receiveTorpedoSynchronization(self, t, a):
        self._torp_point(t, a['ownerID'], a['shotID'], a['serverPos'])

    def m_Avatar_updateMinimapVisionInfo(self, t, a):
        for item in a[0]:
            pos = unpack_minimap(item['packedData'])
            if pos:
                self.minimap[item['vehicleID']].append([_r(t), _r(pos[0], 1), _r(pos[1], 1), _r(pos[2], 3)])

    def m_Avatar_onChatMessage(self, t, a):
        sender, channel, text = a[0], _str(a[1]), _str(a[2])
        self.chat.append([_r(t), sender, channel, text])

    # ------------------------------------------------------------ movement / player input

    def _is_vehicle(self, eid):
        edef = self.entities.get(eid)
        return edef is not None and edef.name == 'Vehicle'

    def on_position(self, t, b):
        eid, _vehicle, x, y, z, _ex, _ey, _ez, yaw = struct.unpack_from('<ii3f3ff', b)
        if self._is_vehicle(eid):
            self.tracks[eid].append([_r(t), _r(x), _r(z), _r(yaw, 3)])

    def on_player_position(self, t, b):
        e1, e2, x, y, z, yaw = struct.unpack_from('<ii3ff', b)
        if e2 == 0 and self._is_vehicle(e1):
            self.tracks[e1].append([_r(t), _r(x), _r(z), _r(yaw, 3)])

    def on_camera(self, t, b):
        f = struct.unpack_from('<8fi6f', b)
        # f[0:4] rotation quaternion, f[4:7] world position, f[7] fov (rad),
        # f[8] mode-ish int, f[9:12] relative offset, f[12:15] forward vector
        self.camera.append([_r(t), _r(f[4]), _r(f[5]), _r(f[6]),
                            _r(f[12], 3), _r(f[13], 3), _r(f[14], 3), _r(f[7], 3)])

    def on_view_mode(self, t, b):
        self.view_mode.append([_r(t), struct.unpack_from('<i', b)[0]])

    def on_target_lock(self, t, b):
        _zero, state, vid = struct.unpack_from('<iii', b)
        self.target_lock.append([_r(t), vid if state else 0])

    def on_steering(self, t, b):
        kind, level = struct.unpack_from('<ii', b)
        self.controls.append([_r(t), kind, level])

    # ------------------------------------------------------------ output

    def result(self):
        h = self.replay.header
        own_vid = next((vid for vid, r in self.roster.items() if r.get('avatarId') == self.avatar_id), None)
        if own_vid is None:   # fall back to the vehicle driven by PLAYER_POSITION
            own_vid = next(iter(self.tracks), None)
        own_team = self.values.get(own_vid, {}).get('teamId', self.roster.get(own_vid, {}).get('teamId'))

        vids = sorted(set(self.roster) | {e for e, d in self.entities.items() if d.name == 'Vehicle'})
        ships = []
        for vid in vids:
            r = self.roster.get(vid, {})
            team = self.values.get(vid, {}).get('teamId', r.get('teamId'))
            params = self.ship_params.get(str(r.get('shipParamsId')), {})
            ships.append({
                'id': vid, 'name': r.get('name', f'#{vid}'), 'clan': r.get('clanTag') or '',
                'team': team, 'relation': 'self' if vid == own_vid else 'ally' if team == own_team else 'enemy',
                'maxHealth': r.get('maxHealth'), 'isBot': r.get('isBot', False),
                'ship': params.get('index') or str(r.get('shipParamsId', '?')),
                'species': params.get('species'), 'level': params.get('level'),
                'length': params.get('length'),     # metres (GameParams hull size)
            })

        xs, zs = [], []
        for series in list(self.tracks.values()) + list(self.minimap.values()):
            xs += [p[1] for p in series]
            zs += [p[2] for p in series]
        for zone in self.zones.values():
            xs += [zone['x'] - zone['radius'], zone['x'] + zone['radius']]
            zs += [zone['z'] - zone['radius'], zone['z'] + zone['radius']]
        bounds = None
        if xs:
            cx, cz = (min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2
            half = max(max(xs) - min(xs), max(zs) - min(zs)) / 2 * 1.1 + 20
            bounds = [_r(cx - half), _r(cz - half), _r(cx + half), _r(cz + half)]

        return {
            'meta': {
                'map': h.get('mapDisplayName'), 'mapId': h.get('mapId'),
                'version': '.'.join(str(v) for v in self.replay.version),
                'dateTime': h.get('dateTime'), 'gameType': h.get('gameType'),
                'scenario': h.get('scenario'), 'player': h.get('playerName'),
                'playerVehicle': h.get('playerVehicle'), 'battleDuration': h.get('battleDuration'),
                'end': _r(self.end_time), 'ownVehicle': own_vid, 'ownTeam': own_team,
                'winScore': self.win_score, 'bounds': bounds, 'aimRangeStep': AIM_RANGE_STEP,
                'result': self.battle_result,
            },
            'ships': ships,
            'tracks': {str(k): v for k, v in self.tracks.items()},
            'minimap': {str(k): v for k, v in self.minimap.items()},
            'aoi': {str(k): v for k, v in self.aoi.items()},
            'health': {str(k): v for k, v in self.health.items()},
            'detected': {str(k): v for k, v in self.detected.items()},
            'aim': {str(k): v for k, v in self.aim.items()},
            'consumables': self.consumables,
            'smokes': [v for v in self.smokes.values() if v['frames']],
            'damage': self.damage,
            'fires': {str(k): v for k, v in self.fires.items()},
            'hits': self.hits,
            'explosions': self.explosions,
            'planes': [v for v in self.planes.values() if v['track']],
            'depth': {str(k): v for k, v in self.depth.items() if any(d[1] < -.5 for d in v)},
            'torpAim': {str(k): v for k, v in self.torp_aim.items()},
            'speed': {str(k): v for k, v in self.speed.items()},
            'rudder': {str(k): v for k, v in self.rudder.items()},
            'deaths': self.deaths,
            'zones': sorted(self.zones.values(), key=lambda z: (z['index'] is None, z['index'])),
            'score': self.score,
            'clock': self.clock,
            'camera': self.camera,
            'viewMode': self.view_mode,
            'targetLock': self.target_lock,
            'controls': self.controls,
            'shots': self.shots,
            'torpedoes': self.torpedoes,
            'chat': self.chat,
            'parseErrors': dict(self.errors),
        }


def build_timeline(path) -> dict:
    replay = read_replay(path)
    return TimelineBuilder(replay, version_dir(replay.version)).run()
