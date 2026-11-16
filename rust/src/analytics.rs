//! Battle analytics from the timeline: a port of wowsreplay/analytics.py (same output; tests/test_rust.py).
use crate::value::{jf, jo, r};
use serde_json::{json, Map, Value as J};
use std::collections::HashMap;
use std::f64::consts::PI;

const KM: f64 = 0.03;
const SHIP_HIT: i64 = 0x60;
const RANGE_BINS: [f64; 6] = [0.0, 4.0, 8.0, 12.0, 16.0, 20.0];
const ANGLE_HI: [f64; 5] = [30.0, 60.0, 120.0, 150.0, 181.0];
const SOURCES: [(&str, char); 8] = [("main", 'm'), ("secondary", 's'), ("torpedo", 't'), ("aircraft", 'a'), ("fire", 'f'),
                                    ("flood", 'w'), ("ram", 'r'), ("other", 'o')];

fn r1(x: f64) -> f64 { r(x, 1) }
fn f(j: &J) -> f64 { j.as_f64().unwrap_or(0.0) }
fn fo(j: &J) -> Option<f64> { j.as_f64() }
fn i(j: &J) -> i64 { j.as_i64().or_else(|| j.as_f64().map(|x| x as i64)).unwrap_or(0) }
fn io(j: &J) -> Option<i64> { j.as_i64().or_else(|| j.as_f64().map(|x| x as i64)) }
fn wrap(a: f64) -> f64 { (a + PI).rem_euclid(2.0 * PI) - PI }
fn dist(a: (f64, f64), b: (f64, f64)) -> f64 { (a.0 - b.0).hypot(a.1 - b.1) }
fn hit(ht: Option<i64>) -> bool { ht.map(|h| h & SHIP_HIT == SHIP_HIT).unwrap_or(false) }
fn round_even(x: f64) -> i64 { x.round_ties_even() as i64 }
fn median(mut v: Vec<f64>) -> Option<f64> {
    if v.is_empty() { return None; }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    Some(if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 })
}

/// rows [t, ...] sorted by t
struct Series { rows: Vec<Vec<f64>>, ts: Vec<f64> }
impl Series {
    fn new(j: &J, cols: usize) -> Series {
        let rows: Vec<Vec<f64>> = j.as_array().map(|a| a.iter().map(|row| (0..cols).map(|k| f(&row[k])).collect()).collect()).unwrap_or_default();
        let ts = rows.iter().map(|r| r[0]).collect();
        Series { rows, ts }
    }
    fn at(&self, t: f64) -> Option<&Vec<f64>> {
        let k = self.ts.partition_point(|&u| u <= t);
        if k == 0 { None } else { Some(&self.rows[k - 1]) }
    }
    fn fresh(&self, t: f64, max_age: f64) -> Option<&Vec<f64>> {
        self.at(t).filter(|r| t - r[0] <= max_age)
    }
}

struct Ship { id: i64, team: J, max_health: Option<f64>, j: J }
struct Shot { t: f64, owner: i64, x0: f64, z0: f64, x1: f64, z1: f64, flight: Option<f64>, gun: i64,
              end_t: Option<f64>, end_x: f64, end_z: f64, ht: Option<i64> }
struct Torp { t: f64, owner: i64, end_t: Option<f64>, end_x: f64, end_z: f64, ht: Option<i64> }
struct Death { t: f64, victim: i64, killer: J, cause: J }
struct Dmg { t: f64, victim: i64, attacker: i64, amount: i64, src: &'static str }
struct Squadron { owner: i64, team: J, n: Option<f64>, losses: Vec<(f64, Option<f64>)>, start: f64, end: Option<f64>, track: Series }

struct A<'a> {
    d: &'a J,
    end: f64,
    ships: Vec<Ship>,
    sidx: HashMap<i64, usize>,
    side: HashMap<i64, bool>,          // true = ally (own team)
    tracks: HashMap<i64, Series>,
    minimap: HashMap<i64, Series>,
    health: HashMap<i64, Series>,
    fires: HashMap<i64, Series>,
    detected: HashMap<i64, Series>,
    death: HashMap<i64, usize>,
    deaths: Vec<Death>,
    half_len: HashMap<i64, f64>,
    max_hp: HashMap<i64, f64>,
    shots: Vec<Shot>,
    torps: Vec<Torp>,
    squadrons: Vec<(i64, Vec<usize>)>,
    sq: Vec<Squadron>,
    shell_ends: Vec<(i64, Vec<(f64, Option<i64>, i64, bool)>)>,
    shell_hits_on: HashMap<i64, Vec<(f64, i64, i64, f64, f64, bool)>>,
    torp_ends: Vec<(i64, Vec<(f64, Option<i64>)>)>,
    dmg: Vec<Dmg>,
}

fn series_map(j: &J, cols: usize) -> HashMap<i64, Series> {
    j.as_object().map(|o| o.iter().filter_map(|(k, v)| Some((k.parse().ok()?, Series::new(v, cols)))).collect()).unwrap_or_default()
}

pub fn analyze(d: &J) -> J {
    let end = d["meta"]["end"].as_f64().filter(|x| *x != 0.0).unwrap_or(0.0);
    let own_team = d["meta"].get("ownTeam").cloned().unwrap_or(J::Null);
    let ships: Vec<Ship> = d["ships"].as_array().unwrap_or(&vec![]).iter()
        .map(|s| Ship { id: i(&s["id"]), team: s["team"].clone(), max_health: fo(&s["maxHealth"]), j: s.clone() }).collect();
    let sidx = ships.iter().enumerate().map(|(k, s)| (s.id, k)).collect();
    let side = ships.iter().map(|s| (s.id, s.team == own_team)).collect();
    let health = series_map(&d["health"], 2);
    let deaths: Vec<Death> = d["deaths"].as_array().unwrap_or(&vec![]).iter()
        .map(|x| Death { t: f(&x[0]), victim: i(&x[1]), killer: x[2].clone(), cause: x[3].clone() }).collect();
    let mut death = HashMap::new();
    for (k, x) in deaths.iter().enumerate() { death.insert(x.victim, k); }
    let half_len = ships.iter().map(|s| (s.id, s.j["length"].as_f64().filter(|x| *x != 0.0).unwrap_or(150.0) / 2.0 / 30.0)).collect();
    let max_hp = ships.iter().map(|s| {
        let mut m = s.max_health.filter(|x| *x != 0.0).unwrap_or(0.0);
        if let Some(h) = health.get(&s.id) { for row in &h.rows { m = m.max(row[1]); } }
        (s.id, m)
    }).collect();
    let shots = d["shots"].as_array().unwrap_or(&vec![]).iter().map(|s| Shot {
        t: f(&s[0]), owner: i(&s[1]), x0: f(&s[2]), z0: f(&s[3]), x1: f(&s[4]), z1: f(&s[5]), flight: fo(&s[6]), gun: i(&s[7]),
        end_t: fo(&s[9]), end_x: f(&s[10]), end_z: f(&s[11]), ht: io(&s[12]) }).collect();
    let torps = d["torpedoes"].as_array().unwrap_or(&vec![]).iter().map(|s| Torp {
        t: f(&s[0]), owner: i(&s[1]), end_t: fo(&s[6]), end_x: f(&s[7]), end_z: f(&s[8]), ht: io(&s[9]) }).collect();
    let mut a = A {
        d, end, ships, sidx, side, tracks: series_map(&d["tracks"], 4), minimap: series_map(&d["minimap"], 4), health,
        fires: series_map(&d["fires"], 2), detected: series_map(&d["detected"], 2), death, deaths, half_len, max_hp,
        shots, torps, squadrons: vec![], sq: vec![], shell_ends: vec![], shell_hits_on: HashMap::new(), torp_ends: vec![], dmg: vec![],
    };
    a.run()
}

fn group_push<K: PartialEq, T>(v: &mut Vec<(K, Vec<T>)>, k: K, item: T) {
    if let Some(slot) = v.iter_mut().find(|(kk, _)| *kk == k) { slot.1.push(item); } else { v.push((k, vec![item])); }
}

/// Counter in insertion order
#[derive(Default)]
struct Counter<K: PartialEq + Clone> { items: Vec<(K, i64)> }
impl<K: PartialEq + Clone> Counter<K> {
    fn add(&mut self, k: K, n: i64) {
        if let Some(s) = self.items.iter_mut().find(|(kk, _)| *kk == k) { s.1 += n; } else { self.items.push((k, n)); }
    }
    fn total(&self) -> i64 { self.items.iter().map(|x| x.1).sum() }
    fn get(&self, k: &K) -> i64 { self.items.iter().find(|(kk, _)| kk == k).map(|x| x.1).unwrap_or(0) }
    /// most_common(n): by count, ties in insertion order
    fn most_common(&self, n: usize) -> Vec<(K, i64)> {
        let mut v = self.items.clone();
        v.sort_by(|a, b| b.1.cmp(&a.1));
        v.truncate(n);
        v
    }
}

impl A<'_> {
    fn pos(&self, vid: i64, t: f64, max_age: f64) -> Option<(f64, f64, f64)> {
        let r = self.tracks.get(&vid).and_then(|s| s.fresh(t, max_age))
            .or_else(|| self.minimap.get(&vid).and_then(|s| s.fresh(t, max_age)));
        r.map(|r| (r[1], r[2], r[3]))
    }
    fn alive_until(&self, vid: i64) -> f64 {
        self.death.get(&vid).map(|&k| self.deaths[k].t).unwrap_or(self.end)
    }
    fn is_ally(&self, vid: i64) -> Option<bool> { self.side.get(&vid).copied() }
    fn nearest_ship(&self, x: f64, z: f64, t: f64, exclude: i64) -> Option<i64> {
        let mut best: Option<(f64, i64)> = None;
        for s in &self.ships {
            if s.id == exclude || t > self.alive_until(s.id) + 1.0 { continue; }
            if let Some(p) = self.pos(s.id, t, 5.0) {
                let dd = (p.0 - x).hypot(p.1 - z) - self.half_len[&s.id];
                if best.map(|b| dd < b.0).unwrap_or(true) { best = Some((dd, s.id)); }
            }
        }
        best.filter(|b| b.0 < 4.0).map(|b| b.1)
    }

    fn run(&mut self) -> J {
        self.index_events();
        self.attribute_damage();
        let mut reps = Map::new();
        for k in 0..self.ships.len() {
            let id = self.ships[k].id;
            reps.insert(id.to_string(), self.ship_report(id));
        }
        let codes: String = self.dmg.iter().map(|x| SOURCES.iter().find(|s| s.0 == x.src).unwrap().1).collect();
        let mut out = json!({ "ships": reps, "flow": self.flow(5.0), "fights": self.fight_list(), "highlights": self.highlight_list(),
                              "aircraft": self.aircraft(), "damageSource": codes });
        out["personal"] = self.personal(&out);
        out
    }

    fn index_events(&mut self) {
        let mut shell_ends: Vec<(i64, Vec<(f64, Option<i64>, i64, bool)>)> = vec![];
        let mut hits_on: HashMap<i64, Vec<(f64, i64, i64, f64, f64, bool)>> = HashMap::new();
        for s in &self.shots {
            let Some(ht) = s.ht else { continue };
            if ht & SHIP_HIT != SHIP_HIT { continue; }
            let et = s.end_t.unwrap_or(0.0);
            let victim = self.nearest_ship(s.end_x, s.end_z, et, s.owner);
            let sec = s.gun & 0x8000 != 0;
            group_push(&mut shell_ends, s.owner, (et, victim, ht, sec));
            if let Some(v) = victim { hits_on.entry(v).or_default().push((et, s.owner, ht, s.x0, s.z0, sec)); }
        }
        let mut torp_ends: Vec<(i64, Vec<(f64, Option<i64>)>)> = vec![];
        for tp in &self.torps {
            if hit(tp.ht) {
                let et = tp.end_t.unwrap_or(0.0);
                let victim = self.nearest_ship(tp.end_x, tp.end_z, et, tp.owner);
                group_push(&mut torp_ends, tp.owner, (et, victim));
            }
        }
        for (_, v) in shell_ends.iter_mut() { v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap()); }
        for (_, v) in torp_ends.iter_mut() { v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap()); }
        self.shell_ends = shell_ends;
        self.shell_hits_on = hits_on;
        self.torp_ends = torp_ends;
        for sq in self.d["planes"].as_array().unwrap_or(&vec![]) {
            let k = self.sq.len();
            self.sq.push(Squadron {
                owner: i(&sq["owner"]), team: sq["team"].clone(), n: fo(&sq["n"]),
                losses: sq["losses"].as_array().unwrap_or(&vec![]).iter().map(|l| (f(&l[0]), fo(&l[1]))).collect(),
                start: f(&sq["start"]), end: fo(&sq["end"]), track: Series::new(&sq["track"], 3),
            });
            let owner = self.sq[k].owner;
            group_push(&mut self.squadrons, owner, k);
        }
    }

    fn near_in_time<T: Copy>(events: &[(f64, Option<i64>, T)], t: f64, victim: i64, before: f64, after: f64) -> Vec<T> {
        let lo = events.partition_point(|e| e.0 < t - before);
        events[lo..].iter().take_while(|e| e.0 <= t + after).filter(|e| e.1.is_none() || e.1 == Some(victim)).map(|e| e.2).collect()
    }

    fn attribute_damage(&mut self) {
        let rams: Vec<(i64, i64)> = self.deaths.iter().filter(|x| x.cause == "RAM")
            .flat_map(|x| { let k = i(&x.killer); [(x.victim, k), (k, x.victim)] }).collect();
        let mut out = Vec::new();
        for row in self.d["damage"].as_array().unwrap_or(&vec![]) {
            let (t, victim, attacker, amount) = (f(&row[0]), i(&row[1]), i(&row[2]), i(&row[3]));
            let empty = vec![];
            let ends = self.shell_ends.iter().find(|x| x.0 == attacker).map(|x| &x.1).unwrap_or(&empty);
            let ev: Vec<(f64, Option<i64>, bool)> = ends.iter().map(|e| (e.0, e.1, e.3)).collect();
            let shells = Self::near_in_time(&ev, t, victim, 1.6, 0.4);
            let src: &'static str = if !shells.is_empty() {
                if shells.iter().all(|&s| s) { "secondary" } else { "main" }
            } else {
                let empty_t = vec![];
                let tends = self.torp_ends.iter().find(|x| x.0 == attacker).map(|x| &x.1).unwrap_or(&empty_t);
                let tev: Vec<(f64, Option<i64>, ())> = tends.iter().map(|e| (e.0, e.1, ())).collect();
                if !Self::near_in_time(&tev, t, victim, 2.5, 0.5).is_empty() {
                    "torpedo"
                } else {
                    let vp = self.pos(victim, t, 5.0);
                    let mut src = None;
                    if let Some(vp) = vp {
                        if let Some((_, list)) = self.squadrons.iter().find(|x| x.0 == attacker) {
                            for &k in list {
                                let sq = &self.sq[k];
                                if sq.start - 1.0 <= t && t <= sq.end.filter(|e| *e != 0.0).unwrap_or(self.end) + 3.0 {
                                    if let Some(p) = sq.track.at(t) {
                                        if dist((p[1], p[2]), (vp.0, vp.1)) < 40.0 { src = Some("aircraft"); break; }
                                    }
                                }
                            }
                        }
                    }
                    src.unwrap_or_else(|| {
                        let flags = self.fires.get(&victim).and_then(|s| s.at(t)).map(|r| r[1] as i64).unwrap_or(0);
                        if flags & 0xF != 0 { "fire" } else if flags & 0x30 != 0 { "flood" }
                        else if rams.contains(&(victim, attacker)) { "ram" } else { "other" }
                    })
                }
            };
            out.push(Dmg { t, victim, attacker, amount, src });
        }
        self.dmg = out;
    }

    fn ship_report(&self, vid: i64) -> J {
        let s = &self.ships[self.sidx[&vid]];
        let alive = self.alive_until(vid);
        let (mut dealt, mut taken, mut to, mut frm) = (Counter::default(), Counter::default(), Counter::default(), Counter::default());
        for x in &self.dmg {
            if x.attacker == vid && x.victim != vid { dealt.add(x.src, x.amount); to.add(x.victim, x.amount); }
            if x.victim == vid { taken.add(x.src, x.amount); frm.add(x.attacker, x.amount); }
        }
        let cmap = |c: &Counter<&'static str>| J::Object(c.items.iter().map(|(k, v)| (k.to_string(), json!(v))).collect());
        let pairs = |c: &Counter<i64>| J::Array(c.most_common(6).iter().map(|(k, v)| json!([k, v])).collect());
        let death = self.death.get(&vid).map(|&k| {
            let dd = &self.deaths[k];
            let p = self.pos(vid, dd.t, 60.0);
            let mut team_deaths: Vec<f64> = self.deaths.iter()
                .filter(|x| self.sidx.get(&x.victim).map(|&k| self.ships[k].team == s.team).unwrap_or(s.team.is_null())).map(|x| x.t).collect();
            team_deaths.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let order = team_deaths.iter().position(|&x| x == dd.t).map(|k| k + 1).unwrap_or(0);
            json!({ "t": jf(dd.t), "killer": dd.killer, "cause": dd.cause, "x": jo(p.map(|p| r1(p.0))), "z": jo(p.map(|p| r1(p.1))),
                    "order": order, "of": self.ships.iter().filter(|x| x.team == s.team).count() })
        }).unwrap_or(J::Null);
        json!({
            "alive": jf(r1(alive)), "survived": !self.death.contains_key(&vid),
            "damage": { "dealt": dealt.total(), "bySource": cmap(&dealt), "taken": taken.total(), "takenBySource": cmap(&taken),
                        "to": pairs(&to), "from": pairs(&frm) },
            "main": self.gunnery(vid, alive), "secondary": self.secondaries(vid), "torpedo": self.torpedo(vid),
            "tank": self.tanking(vid, alive),
            "spot": if self.side[&vid] { self.spotting(vid, alive) } else { J::Null },
            "react": self.reactions(vid), "pos": self.positioning(vid, alive), "death": death,
            "kills": self.deaths.iter().filter(|x| io(&x.killer) == Some(vid) && x.victim != vid).count(),
        })
    }

    fn gunnery(&self, vid: i64, alive: f64) -> J {
        let shots: Vec<&Shot> = self.shots.iter().filter(|s| s.owner == vid && s.gun & 0x8000 == 0).collect();
        if shots.is_empty() { return J::Null; }
        let mut kinds: Counter<&'static str> = Counter::default();
        let mut bins: Vec<(f64, Option<f64>, i64, i64)> = RANGE_BINS.iter().enumerate()
            .map(|(k, &lo)| (lo, RANGE_BINS.get(k + 1).copied(), 0, 0)).collect();
        let (mut tracked, mut hits) = (0, 0);
        for s in &shots {
            let rng = (s.x1 - s.x0).hypot(s.z1 - s.z0) * KM;
            let b = bins.iter_mut().rev().find(|b| rng >= b.0).unwrap();
            b.2 += 1;
            if let Some(ht) = s.ht {
                tracked += 1;
                if ht & SHIP_HIT == SHIP_HIT {
                    hits += 1;
                    b.3 += 1;
                    kinds.add(match ht { 0x62 => "cit", 0x63 => "pen", 0x64 => "over", 0x61 => "rico", _ => "shatter" }, 1);
                }
            }
        }
        let mut times: Vec<f64> = shots.iter().map(|s| s.t).collect();
        times.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let salvos = 1 + times.windows(2).filter(|w| w[1] - w[0] > 1.5).count();
        let mut per_gun: Vec<(i64, Vec<f64>)> = vec![];
        for s in &shots {
            let t1 = r1(s.t);
            let g = s.gun & 31;
            if let Some(slot) = per_gun.iter_mut().find(|x| x.0 == g) {
                if !slot.1.contains(&t1) { slot.1.push(t1); }
            } else { per_gun.push((g, vec![t1])); }
        }
        let mut gaps = vec![];
        for (_, ts) in per_gun.iter_mut() {
            ts.sort_by(|a, b| a.partial_cmp(b).unwrap());
            gaps.extend(ts.windows(2).map(|w| w[1] - w[0]).filter(|g| *g > 2.0));
        }
        let mut reload = None;
        if gaps.len() >= 4 {
            gaps.sort_by(|a, b| a.partial_cmp(b).unwrap());
            reload = Some(gaps[gaps.len() / 5]);
        }
        let mut gun_use = None;
        if let Some(rl) = reload.filter(|x| *x != 0.0) {
            if alive > 0.0 {
                let fired: usize = per_gun.iter().map(|x| x.1.len()).sum();
                gun_use = Some((fired as f64 / (per_gun.len() as f64 * ((alive - times[0] + rl) / rl).max(1.0))).min(1.0));
            }
        }
        json!({ "shells": shots.len(), "salvos": salvos, "tracked": tracked, "hits": hits,
                "kinds": J::Object(kinds.items.iter().map(|(k, v)| (k.to_string(), json!(v))).collect()),
                "byRange": bins.iter().filter(|b| b.2 > 0).map(|b| json!([b.0, b.1, b.2, b.3])).collect::<Vec<_>>(),
                "reload": jo(reload.map(r1)), "gunUse": jo(gun_use.map(|x| r(x, 3))), "firstShot": jf(r1(times[0])) })
    }

    fn secondaries(&self, vid: i64) -> J {
        let shots: Vec<&Shot> = self.shots.iter().filter(|s| s.owner == vid && s.gun & 0x8000 != 0).collect();
        if shots.is_empty() { return J::Null; }
        json!({ "shells": shots.len(), "tracked": shots.iter().filter(|s| s.ht.is_some()).count(),
                "hits": shots.iter().filter(|s| hit(s.ht)).count() })
    }

    fn torpedo(&self, vid: i64) -> J {
        let ts: Vec<&Torp> = self.torps.iter().filter(|x| x.owner == vid).collect();
        if ts.is_empty() { return J::Null; }
        json!({ "launched": ts.len(), "tracked": ts.iter().filter(|x| x.ht.is_some()).count(),
                "hits": ts.iter().filter(|x| hit(x.ht)).count() })
    }

    fn tanking(&self, vid: i64, alive: f64) -> J {
        let mine = self.side[&vid];
        let mut aimed = 0;
        for s in &self.shots {
            if s.gun & 0x8000 != 0 || self.is_ally(s.owner) == Some(mine) || s.t > alive { continue; }
            let land = s.t + s.flight.filter(|x| *x != 0.0).unwrap_or(0.0);
            if let Some(p) = self.pos(vid, land, 5.0) {
                if (s.x1 - p.0).hypot(s.z1 - p.1) < 10.0 + self.half_len[&vid] { aimed += 1; }
            }
        }
        let (mut angles, mut cits, mut hits) = ([0i64; 5], [0i64; 5], 0i64);
        for &(et, shooter, ht, x0, z0, sec) in self.shell_hits_on.get(&vid).map(|v| v.as_slice()).unwrap_or(&[]) {
            if sec || self.is_ally(shooter) == Some(mine) { continue; }
            let Some(p) = self.pos(vid, et, 5.0) else { continue };
            hits += 1;
            let rel = wrap((x0 - p.0).atan2(z0 - p.1) - p.2).to_degrees().abs();
            let k = ANGLE_HI.iter().position(|&hi| rel < hi).unwrap_or(4);
            angles[k] += 1;
            if ht == 0x62 { cits[k] += 1; }
        }
        json!({ "shellsAimed": aimed, "hits": hits, "angles": angles, "citAngles": cits,
                "broadside": if hits > 0 { jf(r(angles[2] as f64 / hits as f64, 3)) } else { J::Null } })
    }

    fn spotting(&self, vid: i64, alive: f64) -> J {
        let Some(det) = self.detected.get(&vid) else { return J::Null };
        let rows: Vec<&Vec<f64>> = det.rows.iter().filter(|r| r[0] <= alive).collect();
        let (mut spotted, mut first) = (0.0f64, None);
        for (k, row) in rows.iter().enumerate() {
            let nxt = rows.get(k + 1).map(|r| r[0]).unwrap_or(alive);
            if row[1] != 0.0 {
                spotted += (nxt - row[0]).max(0.0);
                if first.is_none() { first = Some(row[0]); }
            }
        }
        let mut fire: Vec<i64> = self.shots.iter().filter(|s| s.owner == vid && s.gun & 0x8000 == 0).map(|s| round_even(s.t))
            .chain(self.torps.iter().filter(|x| x.owner == vid).map(|x| round_even(x.t))).collect();
        fire.sort();
        fire.dedup();
        let starts: Vec<i64> = fire.iter().enumerate().filter(|(k, t)| *k == 0 || **t - fire[k - 1] > 3).map(|(_, t)| *t).collect();
        let flag = |t: f64| det.at(t).map(|r| r[1]).unwrap_or(0.0) != 0.0;
        let hidden = starts.iter().filter(|&&t| !flag(t as f64 - 0.5)).count();
        let mut idle = 0.0;
        let mut t = 0i64;
        while (t as f64) < (alive as i64) as f64 {
            if flag(t as f64) {
                let k = fire.partition_point(|&u| u <= t);
                if k == 0 || t - fire[k - 1] > 20 { idle += 2.0; }
            }
            t += 2;
        }
        json!({ "spotted": round_even(spotted), "share": if alive != 0.0 { jf(r(spotted / alive, 3)) } else { J::Null },
                "firstSpotted": jo(first.map(r1)), "openedHidden": hidden, "openings": starts.len(), "idleSpotted": round_even(idle) })
    }

    fn reactions(&self, vid: i64) -> J {
        let cons: Vec<&J> = self.d["consumables"].as_array().map(|a| a.iter().filter(|c| i(&c[1]) == vid).collect()).unwrap_or_default();
        let heals = cons.iter().filter(|c| c[2] == "Repair party").count();
        let Some(fs) = self.fires.get(&vid) else {
            return json!({ "fires": 0, "floods": 0, "dcp": [], "dcpMedian": null, "heals": heals });
        };
        let (mut fires, mut floods, mut prev) = (0u32, 0u32, 0i64);
        for row in &fs.rows {
            let flags = row[1] as i64;
            let new = flags & !prev;
            fires += (new & 0xF).count_ones();
            floods += (new & 0x30).count_ones();
            prev = flags;
        }
        let mut dcp: Vec<(f64, f64)> = vec![];
        for c in &cons {
            if c[2] != "Damage control" { continue; }
            let ct = f(&c[0]);
            let flags = fs.at(ct - 0.01).map(|r| r[1]).unwrap_or(0.0);
            if flags != 0.0 {
                let mut start = ct;
                let upto = fs.ts.partition_point(|&u| u <= ct - 0.01);
                for row in fs.rows[..upto].iter().rev() {
                    if row[1] == 0.0 { break; }
                    start = row[0];
                }
                dcp.push((ct, r1(ct - start)));
            }
        }
        let med = median(dcp.iter().map(|x| x.1).collect());
        json!({ "fires": fires, "floods": floods, "dcp": dcp.iter().map(|x| json!([jf(x.0), jf(x.1)])).collect::<Vec<_>>(),
                "dcpMedian": jo(med.map(r1)), "heals": heals })
    }

    fn positioning(&self, vid: i64, alive: f64) -> J {
        let (mut near, mut centre) = (vec![], vec![]);
        let mine = self.side[&vid];
        let mut t = 5i64;
        while (t as f64) < (alive as i64) as f64 {
            let tf = t as f64;
            t += 5;
            let Some(me) = self.pos(vid, tf, 10.0) else { continue };
            let enemies: Vec<(f64, f64, f64)> = self.ships.iter().filter(|s| self.side[&s.id] != mine && tf <= self.alive_until(s.id))
                .filter_map(|s| self.pos(s.id, tf, 10.0)).collect();
            if !enemies.is_empty() {
                near.push(enemies.iter().map(|p| dist((me.0, me.1), (p.0, p.1))).fold(f64::INFINITY, f64::min) * KM);
            }
            let mates: Vec<(f64, f64, f64)> = self.ships.iter().filter(|s| s.id != vid && self.side[&s.id] == mine && tf <= self.alive_until(s.id))
                .filter_map(|s| self.pos(s.id, tf, 10.0)).collect();
            if !mates.is_empty() {
                let n = mates.len() as f64;
                let (cx, cz) = (mates.iter().map(|p| p.0).sum::<f64>() / n, mates.iter().map(|p| p.1).sum::<f64>() / n);
                centre.push((me.0 - cx).hypot(me.1 - cz) * KM);
            }
        }
        let avg = |v: &Vec<f64>| if v.is_empty() { J::Null } else { jf(r(v.iter().sum::<f64>() / v.len() as f64, 2)) };
        json!({ "nearestEnemy": avg(&near), "teamCentre": avg(&centre) })
    }

    fn flow(&self, step: f64) -> J {
        let ally: Vec<i64> = self.ships.iter().filter(|s| self.side[&s.id]).map(|s| s.id).collect();
        let enemy: Vec<i64> = self.ships.iter().filter(|s| !self.side[&s.id]).map(|s| s.id).collect();
        let total_of = |vs: &Vec<i64>| { let s: f64 = vs.iter().map(|v| self.max_hp[v]).sum(); if s == 0.0 { 1.0 } else { s } };
        let total = [total_of(&ally), total_of(&enemy)];
        let (mut ts, mut hp, mut alive) = (vec![], vec![], vec![]);
        let mut t = 0.0f64;
        loop {
            let (mut rh, mut ra) = (vec![], vec![]);
            for (k, side) in [&ally, &enemy].iter().enumerate() {
                let (mut s, mut n) = (0.0, 0);
                for &v in side.iter() {
                    if t >= self.alive_until(v) && self.death.contains_key(&v) { continue; }
                    n += 1;
                    s += self.health.get(&v).and_then(|h| h.at(t)).map(|r| r[1])
                        .unwrap_or_else(|| self.ships[self.sidx[&v]].max_health.unwrap_or(0.0));
                }
                rh.push(r(s / total[k], 4));
                ra.push(n);
            }
            ts.push(r1(t));
            hp.push(rh);
            alive.push(ra);
            if t >= self.end { break; }
            t = (t + step).min(self.end);
        }
        let bal: Vec<f64> = hp.iter().map(|h| h[0] - h[1]).collect();
        let w = ((60.0 / step) as usize).max(1);
        let mut swings: Vec<(f64, usize)> = (0..bal.len().saturating_sub(w)).map(|k| (bal[k + w] - bal[k], k)).collect();
        swings.sort_by(|a, b| b.0.abs().partial_cmp(&a.0.abs()).unwrap());
        let (mut turns, mut used): (Vec<J>, Vec<usize>) = (vec![], vec![]);
        for (sw, k) in swings {
            if sw.abs() < 0.08 || turns.len() >= 4 { break; }
            if used.iter().any(|&j| (k as i64 - j as i64).unsigned_abs() < w as u64) { continue; }
            used.push(k);
            let (t0, t1) = (ts[k], ts[k + w]);
            let kills: Vec<J> = self.deaths.iter().filter(|x| t0 <= x.t && x.t <= t1).map(|x| json!([jf(x.t), x.victim, x.killer])).collect();
            turns.push(json!({ "t": jf(t0), "end": jf(t1), "swing": jf(r(sw, 3)), "for": if sw > 0.0 { "ally" } else { "enemy" }, "kills": kills }));
        }
        turns.sort_by(|a, b| f(&a["t"]).partial_cmp(&f(&b["t"])).unwrap());
        let lead: Vec<J> = (1..bal.len()).filter(|&k| (bal[k] > 0.0) != (bal[k - 1] > 0.0) && bal[k].abs() > 0.01)
            .map(|k| json!({ "t": jf(ts[k]), "to": if bal[k] > 0.0 { "ally" } else { "enemy" } })).collect();
        json!({ "t": ts.iter().map(|x| jf(*x)).collect::<Vec<_>>(), "hp": hp, "alive": alive,
                "total": { "ally": jf(total[0]), "enemy": jf(total[1]) }, "turns": turns, "leadChanges": lead })
    }

    fn fight_list(&self) -> J {
        struct F { start: f64, end: f64, x: f64, z: f64, n: f64, dealt: [i64; 2], ships: Vec<i64> }
        let mut fights: Vec<F> = vec![];
        for x in &self.dmg {
            if x.src == "fire" || x.src == "flood" || x.attacker == x.victim { continue; }
            let (v, a) = (self.pos(x.victim, x.t, 15.0), self.pos(x.attacker, x.t, 15.0));
            let p = match (v, a) {
                (Some(v), Some(a)) => ((v.0 + a.0) / 2.0, (v.1 + a.1) / 2.0),
                (Some(v), None) => (v.0, v.1),
                (None, Some(a)) => (a.0, a.1),
                (None, None) => continue,
            };
            let k = fights.iter().rposition(|f| x.t - f.end < 30.0 && dist(p, (f.x, f.z)) < 150.0);
            let k = k.unwrap_or_else(|| {
                fights.push(F { start: x.t, end: x.t, x: p.0, z: p.1, n: 0.0, dealt: [0, 0], ships: vec![] });
                fights.len() - 1
            });
            let f = &mut fights[k];
            f.end = x.t;
            f.n += 1.0;
            f.x += (p.0 - f.x) / f.n;
            f.z += (p.1 - f.z) / f.n;
            if let Some(ally) = self.is_ally(x.attacker) {
                f.dealt[if ally { 0 } else { 1 }] += x.amount;
                if !f.ships.contains(&x.attacker) { f.ships.push(x.attacker); }
            }
            if !f.ships.contains(&x.victim) { f.ships.push(x.victim); }
        }
        let caps: Vec<&J> = self.d["zones"].as_array().map(|z| z.iter().filter(|z| z["kind"] == "capture" && !z["index"].is_null()).collect()).unwrap_or_default();
        let mut out = vec![];
        for fi in fights {
            if fi.dealt[0] + fi.dealt[1] < 10000 { continue; }
            let kills: Vec<&Death> = self.deaths.iter().filter(|d| fi.start - 5.0 <= d.t && d.t <= fi.end + 5.0 && fi.ships.contains(&d.victim)).collect();
            let mut near: Option<(&J, f64)> = None;
            for z in &caps {
                let dd = (f(&z["x"]) - fi.x).hypot(f(&z["z"]) - fi.z);
                if near.map(|n| dd < n.1).unwrap_or(true) { near = Some((z, dd)); }
            }
            let cap = near.filter(|(z, dd)| *dd < f(&z["radius"]) + 100.0)
                .map(|(z, _)| J::String(char::from(65 + i(&z["index"]) as u8).to_string())).unwrap_or(J::Null);
            let mut ships = fi.ships.clone();
            ships.sort();
            out.push(json!({ "start": jf(fi.start), "end": jf(fi.end), "x": jf(r1(fi.x)), "z": jf(r1(fi.z)), "cap": cap,
                             "dealt": { "ally": fi.dealt[0], "enemy": fi.dealt[1] }, "ships": ships,
                             "lost": { "ally": kills.iter().filter(|d| self.is_ally(d.victim) == Some(true)).count(),
                                       "enemy": kills.iter().filter(|d| self.is_ally(d.victim) == Some(false)).count() } }));
        }
        J::Array(out)
    }

    fn highlight_list(&self) -> J {
        let mut out: Vec<(f64, &str, J, J, J)> = vec![];
        let groups = |ev: Vec<(f64, Option<i64>)>, gap: f64| -> Vec<Vec<(f64, Option<i64>)>> {
            let mut ev = ev;
            ev.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
            let mut g: Vec<Vec<(f64, Option<i64>)>> = vec![];
            for e in ev {
                match g.last_mut() { Some(last) if e.0 - last.last().unwrap().0 <= gap => last.push(e), _ => g.push(vec![e]) }
            }
            g
        };
        let vj = |v: Option<i64>| v.map(J::from).unwrap_or(J::Null);
        for (shooter, ends) in &self.shell_ends {
            for g in groups(ends.iter().filter(|e| e.2 == 0x62).map(|e| (e.0, e.1)).collect(), 3.0) {
                out.push((g[0].0, "citadel", json!(shooter), vj(g[0].1), json!(g.len())));
            }
        }
        for (shooter, ends) in &self.torp_ends {
            for g in groups(ends.clone(), 5.0) {
                out.push((g[0].0, "torpedo", json!(shooter), vj(g[0].1), json!(g.len())));
            }
        }
        for x in &self.dmg {
            let mx = self.sidx.get(&x.victim).and_then(|&k| self.ships[k].max_health).unwrap_or(0.0);
            if mx != 0.0 && x.amount as f64 >= 0.3 * mx && x.attacker != x.victim {
                out.push((x.t, "big hit", json!(x.attacker), json!(x.victim), json!(x.amount)));
            }
        }
        let mut kills: Vec<&Death> = self.deaths.iter().filter(|d| io(&d.killer).map(|k| k != 0 && k != d.victim).unwrap_or(false)).collect();
        kills.sort_by(|a, b| a.t.partial_cmp(&b.t).unwrap());
        let mut by_killer: Vec<(i64, Vec<(f64, Option<i64>)>)> = vec![];
        for d in &kills { group_push(&mut by_killer, i(&d.killer), (d.t, Some(d.victim))); }
        for (killer, ds) in by_killer {
            for g in groups(ds, 60.0) {
                if g.len() >= 2 { out.push((g[g.len() - 1].0, "multi-kill", json!(killer), vj(g[g.len() - 1].1), json!(g.len()))); }
            }
        }
        if let (Some(first), Some(last)) = (kills.first(), kills.last()) {
            out.push((first.t, "first blood", first.killer.clone(), json!(first.victim), json!(1)));
            if kills.len() > 1 { out.push((last.t, "last kill", last.killer.clone(), json!(last.victim), json!(1))); }
        }
        out.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
        J::Array(out.into_iter().map(|h| json!([jf(r1(h.0)), h.1, h.2, h.3, h.4])).collect())
    }

    fn aircraft(&self) -> J {
        if self.squadrons.is_empty() { return J::Null; }
        let mut dmg_air: Counter<i64> = Counter::default();
        for x in &self.dmg { if x.src == "aircraft" { dmg_air.add(x.attacker, x.amount); } }
        let mut owners = Map::new();
        let mut aa: Counter<i64> = Counter::default();
        for (owner, list) in &self.squadrons {
            let sqs: Vec<&Squadron> = list.iter().map(|&k| &self.sq[k]).collect();
            let lost: f64 = sqs.iter().flat_map(|s| s.losses.iter()).map(|l| l.1.unwrap_or(0.0)).sum();
            let planes: f64 = sqs.iter().map(|s| s.n.unwrap_or(0.0)).sum();
            owners.insert(owner.to_string(), json!({ "squadrons": sqs.len(), "planes": jf(planes), "lost": jf(lost), "damage": dmg_air.get(owner) }));
            for sq in sqs {
                for &(t, c) in &sq.losses {
                    let Some(p) = sq.track.at(t) else { continue };
                    let mut best: Option<(f64, i64)> = None;
                    for s in &self.ships {
                        if s.team == sq.team || t > self.alive_until(s.id) { continue; }
                        if let Some(q) = self.pos(s.id, t, 10.0) {
                            let dd = dist((p[1], p[2]), (q.0, q.1));
                            if dd < 200.0 && best.map(|b| dd < b.0).unwrap_or(true) { best = Some((dd, s.id)); }
                        }
                    }
                    if let Some(b) = best { aa.add(b.1, c.unwrap_or(0.0) as i64); }
                }
            }
        }
        json!({ "owners": owners, "aa": J::Object(aa.items.iter().map(|(k, v)| (k.to_string(), json!(v))).collect()) })
    }

    fn personal(&self, out: &J) -> J {
        let own = io(&self.d["meta"]["ownVehicle"]);
        let Some(own) = own.filter(|o| self.sidx.contains_key(o)) else { return J::Null };
        let s = &self.ships[self.sidx[&own]].j;
        let rp = &out["ships"][own.to_string()];
        let res = &self.d["meta"]["result"];
        let has_res = res.as_object().map(|o| !o.is_empty()).unwrap_or(false);
        let win = if !has_res { J::Null } else if res.get("winnerTeamId").cloned().unwrap_or(J::Null) == self.d["meta"]["ownTeam"] { json!(1) }
                  else if res["winnerTeamId"].as_f64().unwrap_or(0.0) < 0.0 { json!(-1) } else { json!(0) };
        let g = |k: &str| rp["main"].get(k).cloned().unwrap_or(json!(0));
        let death = if rp["death"].is_null() { J::Null } else {
            let d = &rp["death"];
            json!({ "t": d["t"], "x": d["x"], "z": d["z"], "cause": d["cause"], "order": d["order"], "of": d["of"] })
        };
        json!({
            "ship": s["ship"], "species": s["species"], "level": s["level"], "map": self.d["meta"]["map"], "win": win,
            "dealt": rp["damage"]["dealt"], "taken": rp["damage"]["taken"], "kills": rp["kills"],
            "shells": g("shells"), "tracked": g("tracked"), "hits": g("hits"),
            "cit": rp["main"]["kinds"].get("cit").cloned().unwrap_or(json!(0)),
            "torps": rp["torpedo"].get("launched").cloned().unwrap_or(json!(0)), "torpHits": rp["torpedo"].get("hits").cloned().unwrap_or(json!(0)),
            "spotted": rp["spot"].get("share").cloned().unwrap_or(J::Null), "broadside": rp["tank"]["broadside"],
            "nearestEnemy": rp["pos"]["nearestEnemy"], "teamCentre": rp["pos"]["teamCentre"],
            "survived": rp["survived"], "alive": rp["alive"], "end": jf(r1(self.end)), "death": death,
        })
    }
}
