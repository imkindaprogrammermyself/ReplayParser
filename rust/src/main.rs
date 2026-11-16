//! wowsreplay-cli REPLAY BUNDLE.json [--timeline] : the page data as JSON on stdout (or only the timeline)
use std::io::Write;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: wowsreplay-cli REPLAY BUNDLE.json [--timeline]");
        std::process::exit(2);
    }
    let replay = std::fs::read(&args[1]).expect("read replay");
    let bundle: serde_json::Value = serde_json::from_slice(&std::fs::read(&args[2]).expect("read bundle")).expect("bundle JSON");
    let mut quiet = |_: &str, _: f64| {};
    let out = if args.iter().any(|a| a == "--timeline") {
        wowsreplay::load_data(&bundle).and_then(|d| wowsreplay::build_timeline(&replay, &d, &mut quiet))
    } else {
        wowsreplay::build_data(&replay, &bundle, &mut quiet)
    };
    match out {
        Ok(j) => { std::io::stdout().write_all(&serde_json::to_vec(&j).unwrap()).unwrap(); }
        Err(e) => { eprintln!("error: {e}"); std::process::exit(1); }
    }
}
