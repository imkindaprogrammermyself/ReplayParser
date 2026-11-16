//! Decoded BigWorld / pickle values, with the Python-ish accessors the timeline code needs.
use serde_json::{Map, Number, Value as J};

#[derive(Clone, Debug, PartialEq)]
pub enum V {
    None,
    Bool(bool),
    Int(i64),
    UInt(u64),
    Float(f64),
    Str(String),
    Bytes(Vec<u8>),
    List(Vec<V>),
    Dict(Vec<(String, V)>),
    /// an allowlisted pickled game class: constructor args and state, nothing callable
    Opaque(String, Box<V>, Box<V>),
}

pub static NONE: V = V::None;

impl V {
    pub fn get(&self, key: &str) -> Option<&V> {
        match self {
            V::Dict(d) => d.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    /// dict.get(key) treating a missing key and a non-dict alike: &V::None
    pub fn g(&self, key: &str) -> &V {
        self.get(key).unwrap_or(&NONE)
    }
    pub fn get_mut(&mut self, key: &str) -> Option<&mut V> {
        match self {
            V::Dict(d) => d.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn set(&mut self, key: &str, val: V) {
        if let V::Dict(d) = self {
            if let Some(slot) = d.iter_mut().find(|(k, _)| k == key) {
                slot.1 = val;
            } else {
                d.push((key.to_string(), val));
            }
        }
    }
    pub fn idx(&self, i: usize) -> &V {
        match self {
            V::List(l) => l.get(i).unwrap_or(&NONE),
            _ => &NONE,
        }
    }
    pub fn list(&self) -> &[V] {
        match self {
            V::List(l) => l,
            _ => &[],
        }
    }
    pub fn f(&self) -> Option<f64> {
        match *self {
            V::Int(i) => Some(i as f64),
            V::UInt(u) => Some(u as f64),
            V::Float(x) => Some(x),
            V::Bool(b) => Some(b as i64 as f64),
            _ => None,
        }
    }
    pub fn fz(&self) -> f64 {
        self.f().unwrap_or(0.0)
    }
    pub fn i(&self) -> Option<i64> {
        match *self {
            V::Int(i) => Some(i),
            V::UInt(u) => Some(u as i64),
            V::Bool(b) => Some(b as i64),
            V::Float(x) if x.fract() == 0.0 => Some(x as i64),
            _ => None,
        }
    }
    pub fn is_none(&self) -> bool {
        matches!(self, V::None)
    }
    /// Python truthiness
    pub fn truthy(&self) -> bool {
        match self {
            V::None => false,
            V::Bool(b) => *b,
            V::Int(i) => *i != 0,
            V::UInt(u) => *u != 0,
            V::Float(x) => *x != 0.0,
            V::Str(s) => !s.is_empty(),
            V::Bytes(b) => !b.is_empty(),
            V::List(l) => !l.is_empty(),
            V::Dict(d) => !d.is_empty(),
            V::Opaque(..) => true,
        }
    }
    /// `x or default`
    pub fn or<'a>(&'a self, other: &'a V) -> &'a V {
        if self.truthy() { self } else { other }
    }
    /// str / bytes as text (bytes decoded as UTF-8 with replacement, like the Python `_str`)
    pub fn text(&self) -> Option<String> {
        match self {
            V::Str(s) => Some(s.clone()),
            V::Bytes(b) => Some(String::from_utf8_lossy(b).into_owned()),
            _ => None,
        }
    }
    pub fn to_json(&self) -> J {
        match self {
            V::None => J::Null,
            V::Bool(b) => J::Bool(*b),
            V::Int(i) => J::from(*i),
            V::UInt(u) => J::from(*u),
            V::Float(x) => Number::from_f64(*x).map(J::Number).unwrap_or(J::Null),
            V::Str(s) => J::String(s.clone()),
            V::Bytes(b) => J::String(String::from_utf8_lossy(b).into_owned()),
            V::List(l) => J::Array(l.iter().map(V::to_json).collect()),
            V::Dict(d) => J::Object(d.iter().map(|(k, v)| (k.clone(), v.to_json())).collect::<Map<_, _>>()),
            V::Opaque(..) => J::Null,
        }
    }
}

/// Python's round(x, nd): correctly rounded to nd decimals (ties to even on the exact binary value)
pub fn r(x: f64, nd: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{:.*}", nd, x).parse().unwrap_or(x)
}
pub fn r2(x: f64) -> f64 {
    r(x, 2)
}

/// a JSON number from an f64 (NaN / inf -> null, as nothing valid can carry them)
pub fn jf(x: f64) -> J {
    Number::from_f64(x).map(J::Number).unwrap_or(J::Null)
}
pub fn jo(x: Option<f64>) -> J {
    x.map(jf).unwrap_or(J::Null)
}
