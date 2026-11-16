//! Replay file: magic, JSON header blocks, then a Blowfish-ECB (chained by XOR with the previous plain block)
//! zlib packet stream. Same limits as wowsreplay/container.py.
use blowfish::Blowfish;
use cipher::{generic_array::GenericArray, BlockDecrypt, KeyInit};
use serde_json::Value as J;

pub const MAGIC: [u8; 4] = [0x12, 0x32, 0x34, 0x11];
const KEY: [u8; 16] = [0x29, 0xB7, 0xC9, 0x09, 0x38, 0x3F, 0x84, 0x88, 0xFA, 0x98, 0xEC, 0x4E, 0x13, 0x19, 0x79, 0xFB];
pub const MAX_STREAM: usize = 256 << 20;

pub struct Replay {
    pub header: J,
    pub stream: Vec<u8>,
}

impl Replay {
    /// client version as ints, e.g. [15, 9, 0, 13357625]
    pub fn version(&self) -> Vec<i64> {
        self.header["clientVersionFromXml"].as_str().unwrap_or("").replace(' ', "")
            .split(',').filter(|p| !p.is_empty()).filter_map(|p| p.parse().ok()).collect()
    }
}

pub fn read_header(data: &[u8]) -> Result<J, String> {
    if data.len() < 12 || data[..4] != MAGIC {
        return Err("not a replay file (bad magic)".into());
    }
    let size = u32::from_le_bytes(data[8..12].try_into().unwrap()) as usize;
    let raw = data.get(12..12 + size).ok_or("truncated header")?;
    serde_json::from_slice(raw).map_err(|e| format!("bad header JSON: {e}"))
}

pub fn read_replay(data: &[u8]) -> Result<Replay, String> {
    if data.len() < 8 || data[..4] != MAGIC {
        return Err("not a replay file (bad magic)".into());
    }
    let count = u32::from_le_bytes(data[4..8].try_into().unwrap());
    if !(1..=16).contains(&count) {
        return Err(format!("not a replay file ({count} header blocks)"));
    }
    let mut pos = 8;
    let mut header = J::Null;
    for i in 0..count {
        let size = u32::from_le_bytes(data.get(pos..pos + 4).ok_or("truncated")?.try_into().unwrap()) as usize;
        pos += 4;
        let raw = data.get(pos..pos + size).ok_or("truncated header block")?;
        pos += size;
        if i == 0 {
            header = serde_json::from_slice(raw).map_err(|e| format!("bad header JSON: {e}"))?;
        }
    }
    let plain = decrypt(&data[pos..]);
    let stream = miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(&plain, MAX_STREAM)
        .map_err(|e| format!("packet stream: {:?} (larger than {} MB or corrupt)", e.status, MAX_STREAM >> 20))?;
    Ok(Replay { header, stream })
}

fn decrypt(data: &[u8]) -> Vec<u8> {
    let end = data.len() - data.len() % 8;
    if end <= 8 {
        return Vec::new();
    }
    let bf: Blowfish = Blowfish::new_from_slice(&KEY).unwrap();
    let mut out = data[8..end].to_vec();
    let mut prev = [0u8; 8];
    for chunk in out.chunks_exact_mut(8) {
        bf.decrypt_block(GenericArray::from_mut_slice(chunk));
        for k in 0..8 {
            chunk[k] ^= prev[k];
        }
        prev.copy_from_slice(chunk);
    }
    out
}
