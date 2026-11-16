//! World of Warships replay parser: replay -> timeline + analytics JSON for the viewer.
//! Built for the browser (wasm32, raw exports below) and natively (the wowsreplay-cli binary, used by the tests).
pub mod analytics;
pub mod assets;
pub mod container;
pub mod defs;
pub mod nested;
pub mod pickle;
pub mod timeline;
pub mod value;

use serde_json::Value as J;

/// Parse a data bundle (wowsreplay/bundle.py): entity defs, constants, ships, consumables, drops.
pub fn load_data(bundle: &J) -> Result<timeline::Data, String> {
    Ok(timeline::Data {
        defs: defs::Definitions::from_json(&bundle["defs"])?,
        consts: bundle["constants"].clone(),
        ships: bundle["ships"].clone(),
        consumable_ids: bundle["consumables"].clone(),
        drops: bundle.get("drops").cloned().unwrap_or(J::Null),
    })
}

/// replay bytes + bundle -> the timeline (what wowsreplay.timeline.build_timeline returns)
pub fn build_timeline(replay: &[u8], data: &timeline::Data, progress: &mut dyn FnMut(&str, f64)) -> Result<J, String> {
    progress("decrypt", 0.0);
    let rp = container::read_replay(replay)?;
    progress("defs", 0.0);
    Ok(timeline::build(&rp, data, progress))
}

// ---------------------------------------------------------------- WebAssembly interface (no wasm-bindgen)
// JS: p = alloc(n) and copy bytes in; parse(replay, n1, bundle, n2) -> result pointer; result_len(); dealloc.
// The result is UTF-8 JSON: {"ok": data} or {"error": "..."}. Progress goes to the imported env.progress(stage, f).
#[cfg(target_arch = "wasm32")]
mod wasm {
    use super::*;
    use std::cell::RefCell;

    #[link(wasm_import_module = "env")]
    extern "C" {
        fn progress(stage: u32, fraction: f64);
    }
    thread_local! { static OUT: RefCell<Vec<u8>> = RefCell::new(Vec::new()); }

    #[no_mangle]
    pub extern "C" fn alloc(n: usize) -> *mut u8 {
        let mut v = Vec::<u8>::with_capacity(n);
        let p = v.as_mut_ptr();
        std::mem::forget(v);
        p
    }
    #[no_mangle]
    pub unsafe extern "C" fn dealloc(p: *mut u8, n: usize) {
        drop(Vec::from_raw_parts(p, 0, n));
    }
    #[no_mangle]
    pub unsafe extern "C" fn parse(rp: *const u8, rn: usize, bp: *const u8, bn: usize) -> *const u8 {
        let replay = std::slice::from_raw_parts(rp, rn);
        let bundle = std::slice::from_raw_parts(bp, bn);
        let mut report = |stage: &str, f: f64| {
            let code = match stage { "decrypt" => 1, "defs" => 2, "packets" => 3, "analytics" => 4, "assets" => 5, _ => 0 };
            progress(code, f);
        };
        let out = match serde_json::from_slice::<J>(bundle).map_err(|e| e.to_string())
            .and_then(|b| crate::build_data(replay, &b, &mut report)) {
            Ok(d) => serde_json::json!({ "ok": d }),
            Err(e) => serde_json::json!({ "error": e }),
        };
        OUT.with(|o| {
            *o.borrow_mut() = serde_json::to_vec(&out).unwrap_or_default();
            o.borrow().as_ptr()
        })
    }
    #[no_mangle]
    pub extern "C" fn result_len() -> usize {
        OUT.with(|o| o.borrow().len())
    }
}

/// replay + bundle (with the asset index) -> everything the viewer page needs (wowsreplay.__main__.build_data)
/// The bundle may carry the server's asset index ("index") and its URL prefix ("assetBase", default /assets/).
pub fn build_data(replay: &[u8], bundle: &J, progress: &mut dyn FnMut(&str, f64)) -> Result<J, String> {
    let data = load_data(bundle)?;
    let mut out = build_timeline(replay, &data, progress)?;
    progress("analytics", 0.0);
    out["analytics"] = analytics::analyze(&out);
    progress("assets", 0.0);
    let base = bundle.get("assetBase").and_then(J::as_str).unwrap_or("/assets/");
    let index = assets::Index::new(bundle.get("index").unwrap_or(&J::Null), base);
    assets::attach(&mut out, &index, bundle.get("aircraft").unwrap_or(&J::Null));
    Ok(out)
}
