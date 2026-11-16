//! Replay -> timeline JSON for the viewer. A port of wowsreplay/timeline.py: same handlers, same rounding, same
//! output (tests/test_rust.py compares the two on every replay).
use crate::container::Replay;
use crate::defs::{le_f32, le_i16, le_i32, le_u32, Definitions, Reader};
use crate::nested;
use crate::pickle;
use crate::value::{jf, r, r2, V};
use serde_json::{json, Map, Value as J};
use std::collections::HashMap;
use std::f64::consts::PI;

pub const MINIMAP_STEP: f64 = 5000.0 / 2047.0;
pub const AIM_RANGE_STEP: f64 = 5.92;
const SPEED_RAW_PER_KNOT: f64 = 5.2;
const SHELL_TIME_FACTOR: f64 = 0.363;
const RUDDER_FULL: f64 = PI / 6.0;
const END_OF_STREAM: u32 = 0xFFFF_FFFF;
const ROSTER_FIELDS: [&str; 8] = ["name", "clanTag", "shipId", "shipParamsId", "teamId", "maxHealth", "avatarId", "accountDBID"];
const MODULE_TYPES: [i64; 8] = [1, 2, 5, 9, 12, 14, 15, 21];
const CONSUMABLE_ALIASES: [(&str, &str); 2] = [("tareloader", "torpedoreloader"), ("mbbooster", "artilleryboosters")];
const CONSUMABLE_LABELS: [(&str, &str); 15] = [
    ("CRASH_CREW", "Damage control"), ("REGEN_CREW", "Repair party"), ("SONAR", "Hydro"), ("RLS", "Radar"),
    ("HYDROPHONE", "Hydrophone"), ("FIGHTER", "Fighter"), ("SCOUT", "Spotter"), ("SPEED_BOOSTER", "Speed boost"),
    ("SMOKE_GENERATOR", "Smoke"), ("MB_BOOSTER", "Main battery reload"), ("FAST_RUDDERS", "Rudder shift"),
    ("AUX_TORP_BOOSTER", "Torpedo boost"), ("SUBMARINE_LOCATOR", "Sub surveillance"), ("TA_RELOADER", "Torpedo reload"),
    ("AIR_DEFENSE_DISP", "Defensive AA"),
];
const WAYPOINT_LAG: [(&str, f64); 3] = [("rw", 1.3), ("u0", 1.6), ("u1", 2.3)];

pub struct Data {
    pub defs: Definitions,
    pub consts: J,
    pub ships: J,
    pub consumable_ids: J,
    pub drops: J,
}

/// an insertion-ordered map (Python dict)
struct OMap<K, T> {
    keys: Vec<K>,
    vals: Vec<T>,
    at: HashMap<K, usize>,
}
impl<K: std::hash::Hash + Eq + Clone, T> OMap<K, T> {
    fn new() -> Self {
        OMap { keys: Vec::new(), vals: Vec::new(), at: HashMap::new() }
    }
    fn get(&self, k: &K) -> Option<&T> {
        self.at.get(k).map(|&i| &self.vals[i])
    }
    fn get_mut(&mut self, k: &K) -> Option<&mut T> {
        self.at.get(k).map(|&i| &mut self.vals[i])
    }
    fn contains(&self, k: &K) -> bool {
        self.at.contains_key(k)
    }
    fn set(&mut self, k: K, v: T) {
        if let Some(&i) = self.at.get(&k) {
            self.vals[i] = v;
        } else {
            self.at.insert(k.clone(), self.keys.len());
            self.keys.push(k);
            self.vals.push(v);
        }
    }
    fn entry(&mut self, k: K, make: impl FnOnce() -> T) -> &mut T {
        if !self.at.contains_key(&k) {
            self.set(k.clone(), make());
        }
        let i = self.at[&k];
        &mut self.vals[i]
    }
    fn iter(&self) -> impl Iterator<Item = (&K, &T)> {
        self.keys.iter().zip(self.vals.iter())
    }
}

type Series = OMap<i64, Vec<J>>;

fn push(s: &mut Series, k: i64, row: J) {
    s.entry(k, Vec::new).push(row);
}
fn series_json(s: &Series) -> J {
    J::Object(s.iter().map(|(k, v)| (k.to_string(), J::Array(v.clone()))).collect::<Map<_, _>>())
}
fn last(s: &Series, k: i64) -> Option<&J> {
    s.get(&k).and_then(|v| v.last())
}
fn jt(t: f64) -> J {
    jf(r2(t))
}
fn num(j: &J) -> f64 {
    j.as_f64().unwrap_or(0.0)
}

struct Raw {
    t: f64,
    x: f64,
    z: f64,
    y: Option<f64>,
}

struct Plane {
    id: u64,
    team: J,
    params: J,
    n: J,
    losses: Vec<J>,
    start: f64,
    end: Option<f64>,
    raw: HashMap<&'static str, Vec<Raw>>,
}

pub struct Builder<'a> {
    d: &'a Data,
    replay: &'a Replay,
    entities: HashMap<i64, usize>,
    values: HashMap<i64, V>,
    avatar_id: Option<i64>,
    roster: OMap<i64, V>,
    errors: OMap<String, i64>,
    zone_drop: HashMap<i64, V>,
    tracks: Series,
    minimap: Series,
    aoi: Series,
    health: Series,
    detected: Series,
    aim: Series,
    torp_aim: Series,
    deaths: Vec<J>,
    zones: OMap<i64, Map<String, J>>,
    score: Vec<J>,
    clock: Vec<J>,
    camera: Vec<J>,
    view_mode: Vec<J>,
    target_lock: Vec<J>,
    controls: Vec<J>,
    shots: Vec<Vec<J>>,
    torpedoes: Vec<Vec<J>>,
    module_states: Series,
    module_last: HashMap<(i64, i64), (i64, f64)>,
    speed: Series,
    rudder: Series,
    torp: HashMap<(i64, i64), usize>,
    shot: HashMap<(i64, i64), usize>,
    pings: Vec<Vec<J>>,
    ping: HashMap<(i64, i64), usize>,
    own_vis: Vec<Vec<J>>,
    own_spots: Vec<J>,
    chat: Vec<J>,
    consumables: Vec<J>,
    smokes: OMap<i64, Map<String, J>>,
    damage: Vec<J>,
    fires: Series,
    hits: Vec<J>,
    explosions: Vec<J>,
    eid: i64,
    planes: OMap<u64, Plane>,
    planes_done: Vec<Plane>,
    depth: Series,
    battle_result: J,
    win_score: J,
    end_time: f64,
}

pub fn build(replay: &Replay, d: &Data, progress: &mut dyn FnMut(&str, f64)) -> J {
    let mut b = Builder {
        d, replay, entities: HashMap::new(), values: HashMap::new(), avatar_id: None, roster: OMap::new(),
        errors: OMap::new(), zone_drop: HashMap::new(), tracks: OMap::new(), minimap: OMap::new(), aoi: OMap::new(),
        health: OMap::new(), detected: OMap::new(), aim: OMap::new(), torp_aim: OMap::new(), deaths: vec![],
        zones: OMap::new(), score: vec![], clock: vec![], camera: vec![], view_mode: vec![], target_lock: vec![],
        controls: vec![], shots: vec![], torpedoes: vec![], module_states: OMap::new(), module_last: HashMap::new(),
        speed: OMap::new(), rudder: OMap::new(), torp: HashMap::new(), shot: HashMap::new(), pings: vec![],
        ping: HashMap::new(), own_vis: vec![], own_spots: vec![], chat: vec![], consumables: vec![],
        smokes: OMap::new(), damage: vec![], fires: OMap::new(), hits: vec![], explosions: vec![], eid: 0,
        planes: OMap::new(), planes_done: vec![], depth: OMap::new(), battle_result: J::Null, win_score: J::Null,
        end_time: 0.0,
    };
    b.run(progress);
    b.result()
}

impl Builder<'_> {
    fn run(&mut self, progress: &mut dyn FnMut(&str, f64)) {
        let stream = &self.replay.stream;
        let total = stream.len().max(1);
        let step = (stream.len() / 50).max(1);
        let (mut pos, mut next_report, mut last_t) = (0usize, 0usize, 0.0f64);
        while pos + 12 <= stream.len() {
            let size = u32::from_le_bytes(stream[pos..pos + 4].try_into().unwrap()) as usize;
            let ptype = u32::from_le_bytes(stream[pos + 4..pos + 8].try_into().unwrap());
            let t = f32::from_le_bytes(stream[pos + 8..pos + 12].try_into().unwrap()) as f64;
            pos += 12;
            let body = &stream[pos..(pos + size).min(stream.len())];
            pos += size;
            if pos >= next_report {
                progress("packets", pos.min(total) as f64 / total as f64);
                next_report = pos + step;
            }
            if ptype != END_OF_STREAM {
                last_t = last_t.max(t);
            }
            let res = match ptype {
                0x00 => self.on_base_player(body),
                0x01 => self.on_cell_player(body),
                0x05 => self.on_entity_create(t, body),
                0x04 => self.on_entity_leave(t, body),
                0x07 => self.on_property(t, body),
                0x08 => self.on_method(t, body),
                0x23 => self.on_nested(t, body),
                0x0A => self.on_position(t, body),
                0x2C => self.on_player_position(t, body),
                0x25 => self.on_camera(t, body),
                0x27 => le_i32(body, 0).map(|m| self.view_mode.push(json!([jt(t), m]))),
                0x30 => self.on_target_lock(t, body),
                0x32 => self.on_steering(t, body),
                _ => Ok(()),
            };
            if let Err(e) = res {
                let key: String = format!("{:#x} {}", ptype, e).chars().take(160).collect();
                *self.errors.entry(key, || 0) += 1;
            }
        }
        self.end_time = last_t;
    }

    // ------------------------------------------------------------ entities
    fn edef(&self, eid: i64) -> Option<&crate::defs::EntityDef> {
        self.entities.get(&eid).map(|&k| self.d.defs.get(k))
    }
    fn ename(&self, eid: i64) -> &str {
        self.edef(eid).map(|e| e.name.as_str()).unwrap_or("")
    }
    fn vals(&mut self, eid: i64) -> &mut V {
        self.values.entry(eid).or_insert_with(|| V::Dict(vec![]))
    }

    fn on_base_player(&mut self, b: &[u8]) -> Result<(), String> {
        let eid = le_i32(b, 0)? as i64;
        let k = self.d.defs.by_index(le_i16(b, 4)? as i64).ok_or("list index out of range")?;
        self.entities.insert(eid, k);
        self.avatar_id = Some(eid);
        Ok(())
    }
    fn on_cell_player(&mut self, b: &[u8]) -> Result<(), String> {
        let eid = le_i32(b, 0)? as i64;
        if !self.entities.contains_key(&eid) {
            let k = self.d.defs.by_name("Avatar").ok_or("no Avatar entity")?;
            self.entities.insert(eid, k);
        }
        Ok(())
    }
    fn on_entity_create(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let eid = le_i32(b, 0)? as i64;
        let k = self.d.defs.by_index(le_i16(b, 4)? as i64).ok_or("list index out of range")?;
        let (x, z) = (le_f32(b, 14)?, le_f32(b, 22)?);
        self.entities.insert(eid, k);
        let edef = self.d.defs.get(k);
        let mut rd = Reader::new(b, 38);
        let n = rd.u32()? as usize;
        let state = rd.read(n)?;
        let mut st = Reader::new(state, 0);
        let count = st.u8()?;
        for _ in 0..count {
            let (name, typ) = edef.props.get(st.u8()? as usize).ok_or("list index out of range")?;
            let v = typ.decode(&mut st)?;
            self.vals(eid).set(name, v);
        }
        let vals = self.vals(eid).clone();
        match edef.name.as_str() {
            "Vehicle" => {
                push(&mut self.aoi, eid, json!([jt(t), null]));
                if let Some(h) = vals.get("health") {
                    push(&mut self.health, eid, json!([jt(t), jf(r(h.fz(), 0))]));
                }
                let vf = vals.get("visibilityFlags").map(V::to_json).unwrap_or(json!(0));
                push(&mut self.detected, eid, json!([jt(t), vf]));
            }
            "InteractiveZone" => {
                let cs = vals.g("componentsState").or(&V::None);
                let cap = cs.g("captureLogic");
                let cp = cs.g("controlPoint");
                let ztype = vals.g("type");
                let kind = if cp.truthy() || ztype.i() == Some(9) { "capture" } else if ztype.i() == Some(6) { "buff" }
                           else if ztype.i() == Some(12) { "fighters" } else { "zone" };
                let eff = vals.g("effectState");
                let left = eff.g("leftTime");
                let mut zone = Map::new();
                zone.insert("id".into(), json!(eid));
                zone.insert("x".into(), jt(x));
                zone.insert("z".into(), jt(z));
                zone.insert("radius".into(), jt(vals.get("radius").map(V::fz).unwrap_or(0.0)));
                zone.insert("index".into(), cp.g("index").to_json());
                zone.insert("kind".into(), json!(kind));
                zone.insert("type".into(), ztype.to_json());
                zone.insert("start".into(), jt(t));
                zone.insert("end".into(), if left.truthy() { jt(t + left.fz()) } else { J::Null });
                zone.insert("owner".into(), if vals.g("ownerId").truthy() { vals.g("ownerId").to_json() } else { J::Null });
                zone.insert("buff".into(), J::Null);
                zone.insert("team".into(), json!([[jt(t), vals.g("teamId").to_json()]]));
                let prog = cap.get("progress").map(V::fz).unwrap_or(0.0);
                let inv = cap.get("invaderTeam").map(V::to_json).unwrap_or(json!(-1));
                zone.insert("capture".into(), json!([[jt(t), jf(r(prog, 3)), inv]]));
                self.zones.set(eid, zone);
                self.zone_buff(eid);
            }
            "BattleLogic" => self.record_battle_logic(t, &vals),
            "SmokeScreen" => {
                let mut s = Map::new();
                s.insert("id".into(), json!(eid));
                s.insert("radius".into(), jt(vals.get("radius").map(V::fz).unwrap_or(15.0)));
                s.insert("frames".into(), json!([]));
                s.insert("end".into(), J::Null);
                self.smokes.set(eid, s);
                self.smoke_frame(t, eid);
            }
            _ => {}
        }
        if edef.name == "Vehicle" && vals.g("burningFlags").truthy() {
            push(&mut self.fires, eid, json!([jt(t), vals.g("burningFlags").to_json()]));
        }
        Ok(())
    }

    fn on_entity_leave(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let eid = le_i32(b, 0)? as i64;
        if let Some(spans) = self.aoi.get_mut(&eid) {
            if let Some(last) = spans.last_mut() {
                if last[1].is_null() {
                    last[1] = jt(t);
                }
            }
        }
        if let Some(s) = self.smokes.get_mut(&eid) {
            if s["end"].is_null() {
                s.insert("end".into(), jt(t));
            }
        }
        if let Some(z) = self.zones.get_mut(&eid) {
            if z["kind"] != "capture" && (z["end"].is_null() || t < num(&z["end"])) {
                z.insert("end".into(), jt(t));
            }
        }
        Ok(())
    }

    fn zone_buff(&mut self, eid: i64) {
        let Some(drop) = self.zone_drop.get(&eid).cloned() else { return };
        let Some(z) = self.zones.get_mut(&eid) else { return };
        if z["kind"] == "buff" && z["buff"].is_null() {
            let key = match &drop { V::Int(i) => i.to_string(), V::UInt(u) => u.to_string(), o => format!("{}", o.fz()) };
            let info = &self.d.drops[&key];
            let pick = |k: &str| info.get(k).filter(|v| v.as_str().map(|s| !s.is_empty()).unwrap_or(false)).cloned();
            z.insert("buff".into(), pick("marker").or_else(|| pick("name")).unwrap_or(J::String(key)));
        }
    }

    fn on_property(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let eid = le_u32(b, 0)? as i64;
        let idx = le_u32(b, 4)? as usize;
        let Some(&k) = self.entities.get(&eid) else { return Ok(()) };
        let edef = self.d.defs.get(k);
        let (pname, typ) = edef.props.get(idx).ok_or("list index out of range")?;
        let value = typ.decode(&mut Reader::new(b, 12))?;
        self.vals(eid).set(pname, value.clone());
        let ename = edef.name.as_str();
        match (ename, pname.as_str()) {
            ("Vehicle", "health") => push(&mut self.health, eid, json!([jt(t), jf(r(value.fz(), 0))])),
            ("Avatar", "visibilityDistances") if matches!(value, V::Dict(_)) => {
                let row: Vec<J> = std::iter::once(jt(t))
                    .chain(["byShip", "byPlane", "bySmoke"].iter().map(|k| jt(value.g(k).or(&V::Int(0)).fz())))
                    .collect();
                if self.own_vis.last().map(|l| l[1..] != row[1..]).unwrap_or(true) {
                    self.own_vis.push(row);
                }
            }
            ("Avatar", "spottedEntities") => {
                let mut ids: Vec<i64> = value.list().iter().filter_map(V::i).collect();
                ids.sort();
                self.own_spots.push(json!([jt(t), ids]));
            }
            ("Vehicle", "visibilityFlags") => push(&mut self.detected, eid, json!([jt(t), value.to_json()])),
            ("Vehicle", "buoyancyCurrentWaterline") => {
                let v = value.fz();
                let add = match last(&self.depth, eid) {
                    None => true,
                    Some(l) => (num(&l[1]) - v).abs() >= 0.05 || t - num(&l[0]) > 5.0,
                };
                self.depth.entry(eid, Vec::new);
                if add {
                    push(&mut self.depth, eid, json!([jt(t), jf(r(v, 3))]));
                }
            }
            ("Vehicle", "serverSpeedRaw") => {
                let v = self.vals(eid).clone();
                let sign = if v.g("speedSignDir").or(&V::Int(1)).fz() < 0.0 { -1.0 } else { 1.0 };
                let kn = r(v.get("serverSpeedRaw").map(V::fz).unwrap_or(0.0) / SPEED_RAW_PER_KNOT * sign, 1);
                let add = match last(&self.speed, eid) {
                    None => true,
                    Some(l) => (num(&l[1]) - kn).abs() >= 0.5 || (t - num(&l[0]) > 5.0 && num(&l[1]) != kn),
                };
                self.speed.entry(eid, Vec::new);
                if add {
                    push(&mut self.speed, eid, json!([jt(t), jf(kn)]));
                }
            }
            ("Vehicle", "ruddersAngle") => {
                let rv = r2(value.fz() / RUDDER_FULL);
                if last(&self.rudder, eid).map(|l| num(&l[1]) != rv).unwrap_or(true) {
                    push(&mut self.rudder, eid, json!([jt(t), jf(rv)]));
                }
            }
            ("Vehicle", "burningFlags") => {
                if last(&self.fires, eid).map(|l| l[1] != value.to_json()).unwrap_or(true) {
                    push(&mut self.fires, eid, json!([jt(t), value.to_json()]));
                }
            }
            ("SmokeScreen", "points") | ("SmokeScreen", "radius") if self.smokes.contains(&eid) => {
                if pname == "radius" {
                    self.smokes.get_mut(&eid).unwrap().insert("radius".into(), jt(value.fz()));
                }
                self.smoke_frame(t, eid);
            }
            ("Vehicle", "targetLocalPos") | ("Vehicle", "torpedoLocalPos") => {
                let s = if pname == "targetLocalPos" { &mut self.aim } else { &mut self.torp_aim };
                if last(s, eid).map(|l| l[1] != value.to_json()).unwrap_or(true) {
                    push(s, eid, json!([jt(t), value.to_json()]));
                }
            }
            ("BattleLogic", "timeLeft") => self.clock.push(json!([jt(t), value.to_json()])),
            ("BattleLogic", "battleResult") => self.battle_result = value.to_json(),
            ("InteractiveZone", "teamId") if self.zones.contains(&eid) => {
                if let Some(J::Array(team)) = self.zones.get_mut(&eid).unwrap().get_mut("team") {
                    team.push(json!([jt(t), value.to_json()]));
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn smoke_frame(&mut self, t: f64, eid: i64) {
        let pts: Vec<J> = self.vals(eid).g("points").list().iter()
            .flat_map(|p| [jt(p.idx(0).fz()), jt(p.idx(2).fz())]).collect();
        let s = self.smokes.get_mut(&eid).unwrap();
        if let Some(J::Array(frames)) = s.get_mut("frames") {
            if frames.last().map(|f| f[1] != J::Array(pts.clone())).unwrap_or(true) {
                frames.push(json!([jt(t), pts]));
            }
        }
    }

    fn on_nested(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let eid = le_u32(b, 0)? as i64;
        let Some(&k) = self.entities.get(&eid) else { return Ok(()) };
        let edef = self.d.defs.get(k);
        if edef.name == "Avatar" {
            return Ok(());
        }
        let path = {
            let vals = self.values.entry(eid).or_insert_with(|| V::Dict(vec![]));
            nested::apply(b, edef, vals)?
        };
        let p = |n: usize, want: &[&str]| path.len() >= n && path[..n].iter().zip(want).all(|(a, b)| a == b);
        let ename = edef.name.as_str();
        if ename == "BattleLogic" && p(2, &["state", "drop"]) {
            let data = self.vals(eid).g("state").g("drop").g("data").clone();
            for dr in data.list() {
                if let Some(z) = dr.g("zoneId").i() {
                    self.zone_drop.insert(z, dr.g("paramsId").clone());
                    self.zone_buff(z);
                }
            }
        }
        if ename == "BattleLogic" && p(3, &["state", "missions", "teamsScore"]) {
            let vals = self.vals(eid).clone();
            self.record_battle_logic(t, &vals);
        } else if ename == "SmokeScreen" && p(1, &["points"]) && self.smokes.contains(&eid) {
            self.smoke_frame(t, eid);
        } else if ename == "InteractiveZone" && p(2, &["componentsState", "captureLogic"]) && self.zones.contains(&eid) {
            let cap = self.vals(eid).g("componentsState").g("captureLogic").clone();
            let row = json!([jt(t), jf(r(cap.g("progress").fz(), 3)), cap.g("invaderTeam").to_json()]);
            if let Some(J::Array(c)) = self.zones.get_mut(&eid).unwrap().get_mut("capture") {
                c.push(row);
            }
        }
        Ok(())
    }

    fn record_battle_logic(&mut self, t: f64, vals: &V) {
        let missions = vals.g("state").g("missions");
        let mut scores: Vec<(i64, J)> = Vec::new();
        for s in missions.g("teamsScore").list() {
            let team = s.g("teamId").i().unwrap_or(-1);
            if let Some(slot) = scores.iter_mut().find(|x| x.0 == team) {
                slot.1 = s.g("score").to_json();
            } else {
                scores.push((team, s.g("score").to_json()));
            }
        }
        if !scores.is_empty() {
            let get = |k: i64| scores.iter().find(|x| x.0 == k).map(|x| x.1.clone()).unwrap_or(json!(0));
            self.score.push(json!([jt(t), get(0), get(1)]));
        }
        if missions.g("teamWinScore").truthy() {
            self.win_score = missions.g("teamWinScore").to_json();
        }
        if vals.get("timeLeft").is_some() && self.clock.is_empty() {
            self.clock.push(json!([jt(t), vals.g("timeLeft").to_json()]));
        }
    }

    // ------------------------------------------------------------ methods
    fn on_method(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let eid = le_u32(b, 0)? as i64;
        let idx = le_u32(b, 4)? as usize;
        let Some(&k) = self.entities.get(&eid) else { return Ok(()) };
        let edef = self.d.defs.get(k);
        let m = edef.methods.get(idx).ok_or("list index out of range")?;
        let name = format!("{}.{}", edef.name, m.name);
        const HANDLED: &[&str] = &[
            "Avatar.onArenaStateReceived", "Avatar.onGameRoomStateChanged", "Avatar.onNewPlayerSpawnedInBattle",
            "Avatar.receiveVehicleDeath", "Avatar.receiveArtilleryShots", "Vehicle.onConsumableUsed",
            "Vehicle.receiveHitLocationStateChange", "Vehicle.receiveDamagesOnShip", "Avatar.receiveShotKills",
            "Avatar.receiveExplosions", "Avatar.receive_addMinimapSquadron", "Avatar.receive_updateMinimapSquadron",
            "Avatar.receive_removeMinimapSquadron", "Avatar.receive_addSquadron", "Avatar.receive_updateSquadron",
            "Avatar.receive_resetWaypoints", "Avatar.receive_removeSquadron", "Avatar.receive_planeDeath",
            "Avatar.receiveTorpedoes", "Avatar.receivePingerShots", "Avatar.receivePingerShotKills",
            "Avatar.receiveTorpedoDirection", "Avatar.receiveTorpedoManeuverEnd", "Avatar.receiveTorpedoSynchronization",
            "Avatar.updateMinimapVisionInfo", "Avatar.onChatMessage",
        ];
        if !HANDLED.contains(&name.as_str()) {
            return Ok(());
        }
        self.eid = eid;
        let a = m.decode(&mut Reader::new(b, 12))?;
        match name.as_str() {
            "Avatar.onArenaStateReceived" => {
                self.merge_roster(a.g("playersStates"), "id_property_map", false)?;
                self.merge_roster(a.g("botsStates"), "id_property_map_bots", true)?;
            }
            "Avatar.onGameRoomStateChanged" | "Avatar.onNewPlayerSpawnedInBattle" => {
                let (p, bots) = if matches!(a, V::Dict(_)) { (a.g("playersData"), a.g("botsData")) } else { (a.idx(0), a.idx(1)) };
                self.merge_roster(p, "id_property_map", false)?;
                self.merge_roster(bots, "id_property_map_bots", true)?;
            }
            "Avatar.receiveVehicleDeath" => {
                let reason = a.idx(2).i().map(|i| i.to_string()).unwrap_or_default();
                let why = self.d.consts["DEATH_TYPES"].get(&reason).cloned().unwrap_or(J::String(reason));
                self.deaths.push(json!([jt(t), a.idx(0).to_json(), a.idx(1).to_json(), why]));
            }
            "Avatar.receiveArtilleryShots" => {
                for pack in a.idx(0).list() {
                    let owner = pack.g("ownerID").i().unwrap_or(0);
                    for s in pack.g("shots").list() {
                        let (p0, p1) = (s.g("pos"), s.g("tarPos"));
                        let stl = s.g("serverTimeLeft").fz();
                        let flight = if stl > 0.0 { stl * SHELL_TIME_FACTOR } else { 1.0 };
                        let gun = s.get("gunBarrelID").and_then(V::i).unwrap_or(0) & 0x801F;
                        let row = vec![jt(t), json!(owner), jt(p0.idx(0).fz()), jt(p0.idx(2).fz()), jt(p1.idx(0).fz()),
                                       jt(p1.idx(2).fz()), jt(flight), json!(gun), jf(r(s.get("pitch").map(V::fz).unwrap_or(0.0), 3)),
                                       J::Null, J::Null, J::Null, J::Null];
                        self.shots.push(row);
                        self.shot.insert((owner, s.g("shotID").i().unwrap_or(0)), self.shots.len() - 1);
                    }
                }
            }
            "Vehicle.onConsumableUsed" => self.consumable_used(t, &a),
            "Vehicle.receiveHitLocationStateChange" => {
                let hl = a.idx(0).i().unwrap_or(0);
                let packed = a.idx(1).i().unwrap_or(0);
                if !MODULE_TYPES.contains(&hl.div_euclid(1000)) {
                    return Ok(());
                }
                let flags = (packed >> 10) & 3;
                let timer = (packed >> 12) as f64 / 1000.0;
                if self.module_last.get(&(eid, hl)) != Some(&(flags, timer)) {
                    self.module_last.insert((eid, hl), (flags, timer));
                    push(&mut self.module_states, eid, json!([jt(t), hl, flags, jf(r(timer, 1))]));
                }
            }
            "Vehicle.receiveDamagesOnShip" => {
                for dmg in a.idx(0).list() {
                    let amount = dmg.g("damage").fz().round_ties_even() as i64;
                    self.damage.push(json!([jt(t), eid, dmg.g("vehicleID").to_json(), amount]));
                }
            }
            "Avatar.receiveShotKills" => {
                for pack in a.idx(0).list() {
                    let owner = pack.g("ownerID").i().unwrap_or(0);
                    let ht = pack.g("hitType").to_json();
                    for kk in pack.g("kills").list() {
                        let (x, z) = (kk.g("pos").idx(0).fz(), kk.g("pos").idx(2).fz());
                        self.hits.push(json!([jt(t), owner, ht, jt(x), jt(z)]));
                        let key = (owner, kk.g("shotID").i().unwrap_or(0));
                        if let Some(i) = self.torp.remove(&key) {
                            let row = &mut self.torpedoes[i];
                            row[6] = jt(t); row[7] = jt(x); row[8] = jt(z); row[9] = ht.clone();
                        }
                        if let Some(i) = self.shot.remove(&key) {
                            let row = &mut self.shots[i];
                            row[9] = jt(t); row[10] = jt(x); row[11] = jt(z); row[12] = ht.clone();
                            row[6] = jt((t - num(&row[0])).max(0.05));
                        }
                    }
                }
            }
            "Avatar.receiveExplosions" => {
                for e in a.idx(0).list() {
                    self.explosions.push(json!([jt(t), jt(e.g("pos").idx(0).fz()), jt(e.g("pos").idx(2).fz())]));
                }
            }
            "Avatar.receive_addMinimapSquadron" => {
                let pid = pid_of(a.idx(0));
                self.plane(t, pid, Some(a.idx(1).to_json()), Some(a.idx(2).to_json()));
                self.plane_pos(t, pid, a.idx(3).idx(0).fz(), a.idx(3).idx(1).fz(), None, "mm");
            }
            "Avatar.receive_updateMinimapSquadron" => {
                let pid = pid_of(a.idx(0));
                self.plane_pos(t, pid, a.idx(1).idx(0).fz(), a.idx(1).idx(1).fz(), None, "mm");
            }
            "Avatar.receive_removeMinimapSquadron" | "Avatar.receive_removeSquadron" => {
                if let Some(sq) = self.planes.get_mut(&pid_of(a.idx(0))) {
                    if sq.end.is_none() {
                        sq.end = Some(r2(t));
                    }
                }
            }
            "Avatar.receive_addSquadron" => {
                let st = a.g("squadronState");
                let pid = pid_of(st.g("planeID"));
                self.plane(t, pid, None, Some(a.g("paramsID").to_json()));
                let n = st.get("numPlanes").map(V::to_json).unwrap_or(J::Null);
                self.planes.get_mut(&pid).unwrap().n = n;
                let p = st.g("position");
                self.plane_pos(t, pid, p.idx(0).fz(), p.idx(2).fz(), Some(p.idx(1).fz()), "mm");
            }
            "Avatar.receive_updateSquadron" => {
                let pid = pid_of(a.idx(0));
                let mut ahead = 0;
                for w in a.idx(2).list() {
                    let p = w.g("position");
                    if w.g("time").fz() < 200.0 {
                        self.plane_pos(t, pid, p.idx(0).fz(), p.idx(2).fz(), Some(p.idx(1).fz()), "mm");
                    } else if ahead < 2 {
                        self.plane_pos(t, pid, p.idx(0).fz(), p.idx(2).fz(), Some(p.idx(1).fz()), ["u0", "u1"][ahead]);
                        ahead += 1;
                    }
                }
            }
            "Avatar.receive_resetWaypoints" => {
                if let Some(w) = a.idx(1).list().first() {
                    let p = w.g("position");
                    let src = if w.g("time").fz() < 200.0 { "mm" } else { "rw" };
                    self.plane_pos(t, pid_of(a.idx(0)), p.idx(0).fz(), p.idx(2).fz(), Some(p.idx(1).fz()), src);
                }
            }
            "Avatar.receive_planeDeath" => {
                if let Some(sq) = self.planes.get_mut(&pid_of(a.idx(0))) {
                    sq.losses.push(json!([jt(t), a.idx(1).list().len()]));
                }
            }
            "Avatar.receiveTorpedoes" => {
                for pack in a.idx(0).list() {
                    let owner = pack.g("ownerID").i().unwrap_or(0);
                    for tp in pack.g("torpedoes").list() {
                        let (p, dr) = (tp.g("pos"), tp.g("dir"));
                        let row = vec![jt(t), json!(owner), jt(p.idx(0).fz()), jt(p.idx(2).fz()), jf(r(dr.idx(0).fz(), 3)),
                                       jf(r(dr.idx(2).fz(), 3)), J::Null, J::Null, J::Null, J::Null, json!([])];
                        self.torpedoes.push(row);
                        self.torp.insert((owner, tp.g("shotID").i().unwrap_or(0)), self.torpedoes.len() - 1);
                    }
                }
            }
            "Avatar.receivePingerShots" => {
                for p in a.idx(0).list() {
                    let owner = p.g("ownerID").i().unwrap_or(0);
                    let row = vec![jt(t), json!(owner), jt(p.g("pos").idx(0).fz()), jt(p.g("pos").idx(2).fz()),
                                   jf(r(p.g("yaw").fz(), 3)), jt(p.g("speed").fz() / SHELL_TIME_FACTOR), jt(p.g("distance").fz()),
                                   J::Null, J::Null, J::Null, J::Null];
                    self.pings.push(row);
                    self.ping.insert((owner, p.g("shotID").i().unwrap_or(0)), self.pings.len() - 1);
                }
            }
            "Avatar.receivePingerShotKills" => {
                for kk in a.idx(0).list() {
                    if let Some(i) = self.ping.remove(&(kk.g("ownerID").i().unwrap_or(0), kk.g("shotID").i().unwrap_or(0))) {
                        let row = &mut self.pings[i];
                        row[7] = jt(t); row[8] = jt(kk.g("pos").idx(0).fz()); row[9] = jt(kk.g("pos").idx(2).fz());
                        row[10] = kk.g("hitType").to_json();
                    }
                }
            }
            "Avatar.receiveTorpedoDirection" | "Avatar.receiveTorpedoManeuverEnd" => {
                self.torp_point(t, a.idx(0).i().unwrap_or(0), a.idx(1).i().unwrap_or(0), a.idx(2));
            }
            "Avatar.receiveTorpedoSynchronization" => {
                self.torp_point(t, a.g("ownerID").i().unwrap_or(0), a.g("shotID").i().unwrap_or(0), a.g("serverPos"));
            }
            "Avatar.updateMinimapVisionInfo" => {
                for item in a.idx(0).list() {
                    let packed = item.g("packedData").i().unwrap_or(0) as u64;
                    if packed >> 31 != 0 {
                        continue;
                    }
                    let x = (packed & 0x7FF) as f64 * MINIMAP_STEP - 2500.0;
                    let z = ((packed >> 11) & 0x7FF) as f64 * MINIMAP_STEP - 2500.0;
                    let yaw = ((packed >> 22) & 0xFF) as f64 / 255.0 * 2.0 * PI - PI;
                    let vid = item.g("vehicleID").i().unwrap_or(0);
                    push(&mut self.minimap, vid, json!([jt(t), jf(r(x, 1)), jf(r(z, 1)), jf(r(yaw, 3))]));
                }
            }
            "Avatar.onChatMessage" => {
                self.chat.push(json!([jt(t), a.idx(0).to_json(), a.idx(1).text(), a.idx(2).text()]));
            }
            _ => {}
        }
        Ok(())
    }

    fn merge_roster(&mut self, blob: &V, map_name: &str, is_bot: bool) -> Result<(), String> {
        let keys = self.d.consts[map_name].as_object().cloned().unwrap_or_default();
        let raw = match blob {
            V::Bytes(b) => b.clone(),
            V::Str(s) => s.clone().into_bytes(),
            _ => return Err("roster is not a pickle".into()),
        };
        for entry in pickle::loads(&raw)?.list() {
            let pairs = entry.list();
            if !pairs.is_empty() {
                let mx = pairs.iter().filter_map(|p| p.idx(0).i()).max().unwrap_or(0);
                if mx >= keys.len() as i64 {
                    let msg = format!("{map_name}: record has key {mx}, map has {}", keys.len());
                    *self.errors.entry(msg, || 0) += 1;
                    continue;
                }
            }
            let mut info: Vec<(String, V)> = Vec::new();
            for p in pairs {
                let k = p.idx(0).i().map(|i| i.to_string()).unwrap_or_default();
                if let Some(name) = keys.get(&k).and_then(J::as_str) {
                    let v = p.idx(1);
                    let v = match v { V::Bytes(b) => V::Str(String::from_utf8_lossy(b).into_owned()), o => o.clone() };
                    info.retain(|(n, _)| n != name);
                    info.push((name.to_string(), v));
                }
            }
            let get = |n: &str| info.iter().find(|(k, _)| k == n).map(|(_, v)| v);
            let Some(vid) = get("shipId").filter(|v| v.truthy()).and_then(V::i) else { continue };
            let row = self.roster.entry(vid, || V::Dict(vec![("isBot".into(), V::Bool(is_bot))]));
            for f in ROSTER_FIELDS {
                if let Some(v) = get(f) {
                    row.set(f, v.clone());
                }
            }
        }
        Ok(())
    }

    fn consumable_used(&mut self, t: f64, a: &V) {
        let eid = self.eid;
        let usage = a.g("consumableUsageParams");
        let last_id = match usage {
            V::Bytes(b) => b.last().map(|&x| x as i64),
            V::List(l) => l.last().and_then(V::i),
            _ => None,
        }
        .unwrap_or(0);
        let full = self.d.consumable_ids.get(last_id.to_string()).and_then(J::as_str).map(str::to_string)
            .unwrap_or_else(|| format!("CONSUMABLE_{last_id}"));
        let name = full.get("CONSUMABLE_".len()..).unwrap_or("").to_string();
        let params_id = self.roster.get(&eid).map(|r| r.g("shipParamsId").clone()).unwrap_or(V::None);
        let pkey = match params_id { V::Int(i) => i.to_string(), V::UInt(u) => u.to_string(), V::None => "None".into(), o => format!("{}", o.fz()) };
        let params = &self.d.ships[&pkey];
        let norm = |k: &str| k.to_lowercase().replace('_', "").trim_end_matches('s').to_string();
        let lowered = name.to_lowercase().replace('_', "");
        let aliased = CONSUMABLE_ALIASES.iter().find(|(k, _)| *k == lowered).map(|(_, v)| v.to_string()).unwrap_or(name.clone());
        let want = norm(&aliased);
        let range = params["consumables"].as_object()
            .and_then(|c| c.iter().find(|(k, _)| norm(k) == want).map(|(_, v)| v["range"].clone()))
            .unwrap_or(J::Null);
        let label = CONSUMABLE_LABELS.iter().find(|(k, _)| *k == name).map(|(_, v)| v.to_string())
            .unwrap_or_else(|| capitalize(&name.replace('_', " ")));
        self.consumables.push(json!([jt(t), eid, label, jt(a.g("workTimeLeft").fz()), range]));
    }

    fn torp_point(&mut self, t: f64, owner: i64, shot: i64, pos: &V) {
        if let Some(&i) = self.torp.get(&(owner, shot)) {
            if let J::Array(path) = &mut self.torpedoes[i][10] {
                path.push(json!([jt(t), jt(pos.idx(0).fz()), jt(pos.idx(2).fz())]));
            }
        }
    }

    // ------------------------------------------------------------ squadrons
    fn plane(&mut self, t: f64, pid: u64, team: Option<J>, params: Option<J>) {
        let fresh = match self.planes.get(&pid) {
            None => true,
            Some(sq) => sq.end.map(|e| t > e + 1.0).unwrap_or(false),
        };
        if fresh {
            let new = Plane { id: pid, team: J::Null, params: J::Null, n: J::Null, losses: vec![], start: r2(t), end: None,
                              raw: HashMap::new() };
            if let Some(&i) = self.planes.at.get(&pid) {
                let old = std::mem::replace(&mut self.planes.vals[i], new);   // the id came back: a new flight
                self.planes_done.push(old);
            } else {
                self.planes.set(pid, new);
            }
        }
        let sq = self.planes.get_mut(&pid).unwrap();
        if let Some(tm) = team.filter(|j| !j.is_null()) {
            sq.team = tm;
        }
        if let Some(p) = params.filter(|j| !j.is_null()) {
            sq.params = p;
        }
    }
    fn plane_pos(&mut self, t: f64, pid: u64, x: f64, z: f64, y: Option<f64>, src: &'static str) {
        self.plane(t, pid, None, None);
        self.planes.get_mut(&pid).unwrap().raw.entry(src).or_default().push(Raw { t, x, z, y });
    }

    fn plane_track(&self, sq: &Plane) -> Option<J> {
        let empty = Vec::new();
        let mut mm: Vec<&Raw> = sq.raw.get("mm").unwrap_or(&empty).iter().collect();
        mm.sort_by(|a, b| (a.t, a.x, a.z).partial_cmp(&(b.t, b.x, b.z)).unwrap_or(std::cmp::Ordering::Equal));
        let mut pts: Vec<(f64, f64, f64, Option<f64>)> = Vec::new();
        for (src, default) in WAYPOINT_LAG {
            let raw = sq.raw.get(src).unwrap_or(&empty);
            let lag = fit_lag(raw, &mm, default);
            pts.extend(raw.iter().map(|p| (p.t + lag, p.x, p.z, p.y)));
        }
        let cmp = |a: &(f64, f64, f64, Option<f64>), b: &(f64, f64, f64, Option<f64>)| {
            (a.0, a.1, a.2, a.3.unwrap_or(f64::NEG_INFINITY)).partial_cmp(&(b.0, b.1, b.2, b.3.unwrap_or(f64::NEG_INFINITY)))
                .unwrap_or(std::cmp::Ordering::Equal)
        };
        pts.sort_by(cmp);
        if !pts.is_empty() {   // minimap only where the waypoints leave a gap
            let ts: Vec<f64> = pts.iter().map(|p| p.0).collect();
            for p in &mm {
                let i = ts.partition_point(|&u| u < p.t);
                let near: Vec<f64> = [i.wrapping_sub(1), i].iter().filter(|&&j| j < ts.len()).map(|&j| ts[j]).collect();
                if near.is_empty() || near.iter().map(|u| (p.t - u).abs()).fold(f64::INFINITY, f64::min) > 2.5 {
                    pts.push((p.t, p.x, p.z, p.y));
                }
            }
            pts.sort_by(cmp);
        } else {
            pts = mm.iter().map(|p| (p.t, p.x, p.z, p.y)).collect();
        }
        let mut track: Vec<J> = Vec::new();
        let mut last_t: Option<f64> = None;
        for (t, x, z, y) in pts {
            if let Some(e) = sq.end {
                if t > e + 0.5 {
                    break;
                }
            }
            if let Some(lt) = last_t {
                if t - lt < 0.3 {
                    continue;
                }
            }
            last_t = Some(r2(t));
            track.push(json!([jt(t), jt(x), jt(z), y.map(jt).unwrap_or(J::Null)]));
        }
        if track.is_empty() {
            return None;
        }
        Some(json!({
            "id": sq.id, "owner": sq.id & 0xFFFF_FFFF, "team": sq.team, "params": sq.params, "n": sq.n,
            "track": track, "losses": sq.losses, "start": jf(sq.start), "end": sq.end.map(jf).unwrap_or(J::Null),
        }))
    }

    // ------------------------------------------------------------ movement / input
    fn is_vehicle(&self, eid: i64) -> bool {
        self.ename(eid) == "Vehicle"
    }
    fn on_position(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let eid = le_i32(b, 0)? as i64;
        let (x, z, yaw) = (le_f32(b, 8)?, le_f32(b, 16)?, le_f32(b, 32)?);
        if self.is_vehicle(eid) {
            push(&mut self.tracks, eid, json!([jt(t), jt(x), jt(z), jf(r(yaw, 3))]));
        }
        Ok(())
    }
    fn on_player_position(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let (e1, e2) = (le_i32(b, 0)? as i64, le_i32(b, 4)?);
        let (x, z, yaw) = (le_f32(b, 8)?, le_f32(b, 16)?, le_f32(b, 20)?);
        if e2 == 0 && self.is_vehicle(e1) {
            push(&mut self.tracks, e1, json!([jt(t), jt(x), jt(z), jf(r(yaw, 3))]));
        }
        Ok(())
    }
    fn on_camera(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let f = |i: usize| le_f32(b, i * 4);
        self.camera.push(json!([jt(t), jt(f(4)?), jt(f(5)?), jt(f(6)?), jf(r(f(12)?, 3)), jf(r(f(13)?, 3)),
                                jf(r(f(14)?, 3)), jf(r(f(7)?, 3))]));
        Ok(())
    }
    fn on_target_lock(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        let (state, vid) = (le_i32(b, 4)?, le_i32(b, 8)?);
        self.target_lock.push(json!([jt(t), if state != 0 { vid } else { 0 }]));
        Ok(())
    }
    fn on_steering(&mut self, t: f64, b: &[u8]) -> Result<(), String> {
        self.controls.push(json!([jt(t), le_i32(b, 0)?, le_i32(b, 4)?]));
        Ok(())
    }

    // ------------------------------------------------------------ output
    fn result(&self) -> J {
        let h = &self.replay.header;
        let mut own_vid = self.roster.iter().find(|(_, r)| self.avatar_id.is_some() && r.g("avatarId").i() == self.avatar_id)
            .map(|(k, _)| *k);
        if own_vid.is_none() {
            own_vid = self.tracks.keys.first().copied();
        }
        let team_of = |vid: i64| -> J {
            match self.values.get(&vid).and_then(|v| v.get("teamId")) {
                Some(v) => v.to_json(),
                None => self.roster.get(&vid).map(|r| r.g("teamId").to_json()).unwrap_or(J::Null),
            }
        };
        let own_team = own_vid.map(team_of).unwrap_or(J::Null);

        let mut vids: Vec<i64> = self.roster.keys.clone();
        for (e, &k) in &self.entities {
            if self.d.defs.get(k).name == "Vehicle" && !vids.contains(e) {
                vids.push(*e);
            }
        }
        vids.sort();
        let ships: Vec<J> = vids.iter().map(|&vid| {
            let rr = self.roster.get(&vid).cloned().unwrap_or(V::Dict(vec![]));
            let team = team_of(vid);
            let spid = rr.get("shipParamsId");
            let pkey = match spid { Some(V::Int(i)) => i.to_string(), Some(V::UInt(u)) => u.to_string(), _ => "None".into() };
            let params = &self.d.ships[&pkey];
            let ship = params.get("index").filter(|v| v.as_str().map(|s| !s.is_empty()).unwrap_or(false)).cloned()
                .unwrap_or_else(|| J::String(match spid { Some(v) => v.i().map(|i| i.to_string()).unwrap_or("None".into()), None => "?".into() }));
            let p = |k: &str| params.get(k).cloned().unwrap_or(J::Null);
            json!({
                "id": vid, "name": rr.get("name").and_then(V::text).unwrap_or_else(|| format!("#{vid}")),
                "clan": rr.g("clanTag").text().filter(|s| !s.is_empty()).unwrap_or_default(),
                "team": team, "relation": if Some(vid) == own_vid { "self" } else if team == own_team { "ally" } else { "enemy" },
                "maxHealth": rr.g("maxHealth").to_json(), "isBot": rr.get("isBot").map(V::to_json).unwrap_or(J::Bool(false)),
                "ship": ship, "species": p("species"), "level": p("level"), "length": p("length"), "modules": p("modules"),
                "mainRange": p("mainRange"), "concealment": p("concealment"),
            })
        }).collect();

        let (mut xs, mut zs): (Vec<f64>, Vec<f64>) = (vec![], vec![]);
        for s in [&self.tracks, &self.minimap] {
            for (_, rows) in s.iter() {
                for p in rows {
                    xs.push(num(&p[1]));
                    zs.push(num(&p[2]));
                }
            }
        }
        for (_, z) in self.zones.iter() {
            let (x, zz, rad) = (num(&z["x"]), num(&z["z"]), num(&z["radius"]));
            xs.extend([x - rad, x + rad]);
            zs.extend([zz - rad, zz + rad]);
        }
        let bounds = if xs.is_empty() { J::Null } else {
            let mn = |v: &Vec<f64>| v.iter().cloned().fold(f64::INFINITY, f64::min);
            let mx = |v: &Vec<f64>| v.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let (cx, cz) = ((mn(&xs) + mx(&xs)) / 2.0, (mn(&zs) + mx(&zs)) / 2.0);
            let half = (mx(&xs) - mn(&xs)).max(mx(&zs) - mn(&zs)) / 2.0 * 1.1 + 20.0;
            json!([jt(cx - half), jt(cz - half), jt(cx + half), jt(cz + half)])
        };
        let hv = |k: &str| h.get(k).cloned().unwrap_or(J::Null);
        let version = self.replay.version().iter().map(|v| v.to_string()).collect::<Vec<_>>().join(".");

        let mut zones: Vec<&Map<String, J>> = self.zones.vals.iter().collect();
        zones.sort_by_key(|z| (z["index"].is_null(), z["index"].as_i64().unwrap_or(0)));
        let mut planes: Vec<J> = Vec::new();
        for sq in self.planes_done.iter().chain(self.planes.vals.iter()) {
            if let Some(p) = self.plane_track(sq) {
                planes.push(p);
            }
        }
        let depth = J::Object(self.depth.iter().filter(|(_, v)| v.iter().any(|d| num(&d[1]) < -0.5))
            .map(|(k, v)| (k.to_string(), J::Array(v.clone()))).collect());
        json!({
            "meta": {
                "map": hv("mapDisplayName"), "mapId": hv("mapId"), "version": version, "dateTime": hv("dateTime"),
                "gameType": hv("gameType"), "scenario": hv("scenario"), "player": hv("playerName"),
                "playerVehicle": hv("playerVehicle"), "battleDuration": hv("battleDuration"), "end": jt(self.end_time),
                "ownVehicle": own_vid, "ownTeam": own_team, "winScore": self.win_score, "bounds": bounds,
                "aimRangeStep": AIM_RANGE_STEP, "result": self.battle_result,
            },
            "ships": ships,
            "tracks": series_json(&self.tracks), "minimap": series_json(&self.minimap), "aoi": series_json(&self.aoi),
            "health": series_json(&self.health), "detected": series_json(&self.detected), "aim": series_json(&self.aim),
            "consumables": self.consumables,
            "smokes": self.smokes.vals.iter().filter(|s| s["frames"].as_array().map(|f| !f.is_empty()).unwrap_or(false)).cloned().collect::<Vec<_>>(),
            "damage": self.damage, "fires": series_json(&self.fires), "hits": self.hits, "explosions": self.explosions,
            "planes": planes, "depth": depth, "torpAim": series_json(&self.torp_aim), "speed": series_json(&self.speed),
            "modules": series_json(&self.module_states), "rudder": series_json(&self.rudder), "deaths": self.deaths,
            "zones": zones, "score": self.score, "clock": self.clock, "camera": self.camera, "viewMode": self.view_mode,
            "targetLock": self.target_lock, "controls": self.controls, "shots": self.shots, "torpedoes": self.torpedoes,
            "pings": self.pings, "ownVis": self.own_vis, "ownSpots": self.own_spots, "chat": self.chat,
            "parseErrors": J::Object(self.errors.iter().map(|(k, v)| (k.clone(), json!(v))).collect()),
        })
    }
}

fn fit_lag(points: &[Raw], mm: &[&Raw], default: f64) -> f64 {
    if points.len() < 5 || mm.len() < 3 {
        return default;
    }
    let ts: Vec<f64> = mm.iter().map(|p| p.t).collect();
    let stride = (points.len() / 150).max(1);
    let mut best: Option<(f64, f64)> = None;
    for k in 0..36 {
        let lag = k as f64 / 10.0;
        let mut errs = Vec::new();
        for p in points.iter().step_by(stride) {
            let i = ts.partition_point(|&u| u < p.t + lag);
            if i > 0 && i < mm.len() && mm[i].t - mm[i - 1].t < 2.5 {
                let (a, c) = (mm[i - 1], mm[i]);
                let f = (p.t + lag - a.t) / (c.t - a.t);
                errs.push((p.x - a.x - (c.x - a.x) * f).hypot(p.z - a.z - (c.z - a.z) * f));
            }
        }
        if errs.len() >= 5 {
            errs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let e = errs[errs.len() / 2];
            if best.map(|b| e < b.0).unwrap_or(true) {
                best = Some((e, lag));
            }
        }
    }
    best.map(|b| b.1).unwrap_or(default)
}

fn pid_of(v: &V) -> u64 {
    match v { V::UInt(u) => *u, V::Int(i) => *i as u64, V::Float(f) => *f as u64, _ => 0 }
}

fn capitalize(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().collect::<String>() + &c.as_str().to_lowercase(),
        None => String::new(),
    }
}

