//! Which imported maps / models / icons a battle uses, as URLs under the server's asset path: the lazy-page half of
//! wowsreplay/__main__.py build_data, answered from the server's asset index instead of the disk.
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeSet, HashMap, HashSet};

pub struct Index {
    files: HashSet<String>,
    json: Map<String, J>,
    dirs: HashMap<String, Vec<String>>,
    base: String,
}

impl Index {
    pub fn new(index: &J, base: &str) -> Index {
        let files: HashSet<String> = index["files"].as_array().map(|a| a.iter().filter_map(|f| f.as_str().map(String::from)).collect()).unwrap_or_default();
        let mut dirs: HashMap<String, Vec<String>> = HashMap::new();
        for f in &files {
            let (d, name) = f.rsplit_once('/').unwrap_or(("", f));
            dirs.entry(d.to_string()).or_default().push(name.to_string());
        }
        Index { files, json: index["json"].as_object().cloned().unwrap_or_default(), dirs, base: base.to_string() }
    }
    fn isfile(&self, rel: &str) -> bool {
        self.files.contains(rel) || self.json.contains_key(rel)
    }
    fn url(&self, rel: &str) -> J {
        J::String(format!("{}{}", self.base, rel))
    }

    fn load_map(&self, name: &J) -> J {
        let Some(name) = name.as_str().filter(|s| !s.is_empty()) else { return J::Null };
        let Some(m) = self.json.get(&format!("maps/{name}/map.json")) else { return J::Null };
        let mut out = json!({ "name": name, "minimapBounds": m.get("minimap").cloned().unwrap_or(J::Null), "terrain": null, "minimap": null });
        let png = format!("maps/{name}/minimap.png");
        if truthy(m.get("minimap")) && self.isfile(&png) {
            out["minimap"] = self.url(&png);
        }
        let ter = format!("maps/{name}/terrain.i16");
        if truthy(m.get("terrain")) && self.isfile(&ter) {
            let mut t = m["terrain"].clone();
            t["url"] = self.url(&ter);
            out["terrain"] = t;
        }
        out
    }
}

fn truthy(v: Option<&J>) -> bool {
    match v {
        None | Some(J::Null) => false,
        Some(J::Bool(b)) => *b,
        Some(J::Number(n)) => n.as_f64() != Some(0.0),
        Some(J::String(s)) => !s.is_empty(),
        Some(J::Array(a)) => !a.is_empty(),
        Some(J::Object(o)) => !o.is_empty(),
    }
}

/// add map / modelUrls / icons / classIcons / planeIcons to the timeline, like build_data(asset_base=...)
pub fn attach(data: &mut J, index: &Index, aircraft: &J) {
    let keys: BTreeSet<String> = data["ships"].as_array().map(|a| a.iter().filter_map(|s| s["ship"].as_str().filter(|x| !x.is_empty()).map(String::from)).collect()).unwrap_or_default();
    data["map"] = index.load_map(&data["meta"]["map"]);
    data["models"] = json!({});
    data["modelUrls"] = J::Object(keys.iter().filter(|k| index.isfile(&format!("models/{k}.json")))
        .map(|k| (k.clone(), index.url(&format!("models/{k}.json")))).collect());
    let mut icons = Map::new();
    for k in &keys {
        let mut one = Map::new();
        for (kind, folder) in [("alive", "ship_icons"), ("dead", "ship_dead_icons")] {
            let p = format!("gui/{folder}/{}.png", k.split('_').next().unwrap_or(k));
            if index.isfile(&p) {
                one.insert(kind.into(), index.url(&p));
            }
        }
        if !one.is_empty() {
            icons.insert(k.clone(), J::Object(one));
        }
    }
    data["icons"] = J::Object(icons);
    let mut cls = Map::new();
    for folder in ["ship_icons", "ship_icons_self"] {
        let d = format!("gui/fla/minimap/{folder}");
        if let Some(names) = index.dirs.get(&d) {
            let mut names = names.clone();
            names.sort();
            for n in names.iter().filter(|n| n.ends_with(".svg")) {
                cls.insert(n[..n.len() - 4].to_string(), index.url(&format!("{d}/{n}")));
            }
        }
    }
    data["classIcons"] = J::Object(cls);
    data["lazyClassIcons"] = J::Bool(true);
    let mut plane_icons = Map::new();
    if let Some(planes) = data["planes"].as_array_mut() {
        for sq in planes.iter_mut() {
            let key = match &sq["params"] { J::Number(n) => n.to_string(), J::String(s) => s.clone(), _ => "None".into() };
            let Some(icon) = aircraft.get(&key).and_then(J::as_str).filter(|s| !s.is_empty()) else { continue };
            for side in ["ally", "enemy", "own"] {
                let k = format!("{icon}_{side}");
                let p = format!("gui/battle_hud/markers_minimap/plane/{icon}_{side}.png");
                if !plane_icons.contains_key(&k) && index.isfile(&p) {
                    plane_icons.insert(k, index.url(&p));
                }
            }
            if plane_icons.contains_key(&format!("{icon}_ally")) {
                sq["icon"] = J::String(icon.to_string());
            }
        }
    }
    data["planeIcons"] = J::Object(plane_icons);
    let bounds = data["map"]["minimapBounds"].clone();
    if truthy(Some(&bounds)) && !data["map"].is_null() {
        data["meta"]["bounds"] = bounds;
    }
}
