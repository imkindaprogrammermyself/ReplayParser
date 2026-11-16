//! Entity definitions (exported from the .def files by wowsreplay/defs.py `export`) and BigWorld value decoding.
use crate::value::V;
use serde_json::Value as J;

pub struct Reader<'a> {
    pub data: &'a [u8],
    pub pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(data: &'a [u8], pos: usize) -> Self {
        Reader { data, pos }
    }
    pub fn read(&mut self, n: usize) -> Result<&'a [u8], String> {
        if self.pos + n > self.data.len() {
            return Err(format!("need {} bytes at {}, have {}", n, self.pos, self.data.len().saturating_sub(self.pos)));
        }
        let b = &self.data[self.pos..self.pos + n];
        self.pos += n;
        Ok(b)
    }
    pub fn u8(&mut self) -> Result<u8, String> {
        Ok(self.read(1)?[0])
    }
    pub fn u16(&mut self) -> Result<u16, String> {
        Ok(u16::from_le_bytes(self.read(2)?.try_into().unwrap()))
    }
    pub fn u32(&mut self) -> Result<u32, String> {
        Ok(u32::from_le_bytes(self.read(4)?.try_into().unwrap()))
    }
    pub fn i32(&mut self) -> Result<i32, String> {
        Ok(i32::from_le_bytes(self.read(4)?.try_into().unwrap()))
    }
    pub fn f32(&mut self) -> Result<f32, String> {
        Ok(f32::from_le_bytes(self.read(4)?.try_into().unwrap()))
    }
    /// BigWorld length prefix: u8, or 0xFF followed by a 24-bit length
    pub fn packed_len(&mut self) -> Result<usize, String> {
        let n = self.u8()?;
        if n == 0xFF {
            let lo = self.u16()? as usize;
            let hi = self.u8()? as usize;
            return Ok(lo | (hi << 16));
        }
        Ok(n as usize)
    }
    pub fn remaining(&self) -> usize {
        self.data.len().saturating_sub(self.pos)
    }
}

/// little-endian field readers at a fixed offset (struct.unpack_from)
pub fn le_i32(b: &[u8], o: usize) -> Result<i32, String> {
    b.get(o..o + 4).map(|s| i32::from_le_bytes(s.try_into().unwrap())).ok_or_else(|| "short packet".into())
}
pub fn le_u32(b: &[u8], o: usize) -> Result<u32, String> {
    b.get(o..o + 4).map(|s| u32::from_le_bytes(s.try_into().unwrap())).ok_or_else(|| "short packet".into())
}
pub fn le_i16(b: &[u8], o: usize) -> Result<i16, String> {
    b.get(o..o + 2).map(|s| i16::from_le_bytes(s.try_into().unwrap())).ok_or_else(|| "short packet".into())
}
pub fn le_f32(b: &[u8], o: usize) -> Result<f64, String> {
    b.get(o..o + 4).map(|s| f32::from_le_bytes(s.try_into().unwrap()) as f64).ok_or_else(|| "short packet".into())
}

#[derive(Debug, Clone)]
pub enum T {
    Num(u8),
    Vector(usize),
    Str,
    Bytes,
    Mailbox,
    Dict { none: bool, fields: Vec<(String, T)> },
    Arr { of: Box<T>, n: Option<usize> },
    User(Option<Box<T>>),
}

impl T {
    pub fn from_json(j: &J) -> Result<T, String> {
        let a = j.as_array().ok_or("type spec is not a list")?;
        let tag = a.first().and_then(J::as_str).ok_or("type spec without a tag")?;
        Ok(match tag {
            "n" => T::Num(a[1].as_str().and_then(|s| s.bytes().next()).ok_or("bad number type")?),
            "v" => T::Vector(a[1].as_u64().ok_or("bad vector")? as usize),
            "s" => T::Str,
            "b" => T::Bytes,
            "m" => T::Mailbox,
            "d" => T::Dict {
                none: a[1].as_u64() == Some(1),
                fields: a[2].as_array().ok_or("bad dict")?.iter()
                    .map(|f| Ok((f[0].as_str().ok_or("bad field")?.to_string(), T::from_json(&f[1])?)))
                    .collect::<Result<_, String>>()?,
            },
            "a" => T::Arr { of: Box::new(T::from_json(&a[1])?), n: a[2].as_u64().map(|n| n as usize) },
            "u" => T::User(if a[1].is_null() { None } else { Some(Box::new(T::from_json(&a[1])?)) }),
            other => return Err(format!("unknown type tag {other}")),
        })
    }

    pub fn decode(&self, r: &mut Reader) -> Result<V, String> {
        Ok(match self {
            T::Num(f) => {
                let n = |r: &mut Reader, k: usize| -> Result<Vec<u8>, String> { Ok(r.read(k)?.to_vec()) };
                match f {
                    b'b' => V::Int(n(r, 1)?[0] as i8 as i64),
                    b'B' => V::Int(n(r, 1)?[0] as i64),
                    b'h' => V::Int(i16::from_le_bytes(n(r, 2)?.try_into().unwrap()) as i64),
                    b'H' => V::Int(u16::from_le_bytes(n(r, 2)?.try_into().unwrap()) as i64),
                    b'i' => V::Int(i32::from_le_bytes(n(r, 4)?.try_into().unwrap()) as i64),
                    b'I' => V::Int(u32::from_le_bytes(n(r, 4)?.try_into().unwrap()) as i64),
                    b'q' => V::Int(i64::from_le_bytes(n(r, 8)?.try_into().unwrap())),
                    b'Q' => V::UInt(u64::from_le_bytes(n(r, 8)?.try_into().unwrap())),
                    b'f' => V::Float(f32::from_le_bytes(n(r, 4)?.try_into().unwrap()) as f64),
                    b'd' => V::Float(f64::from_le_bytes(n(r, 8)?.try_into().unwrap())),
                    _ => return Err("bad number format".into()),
                }
            }
            T::Vector(k) => {
                let mut out = Vec::with_capacity(*k);
                for _ in 0..*k {
                    out.push(V::Float(r.f32()? as f64));
                }
                V::List(out)
            }
            T::Str => {
                let n = r.packed_len()?;
                let raw = r.read(n)?;
                match std::str::from_utf8(raw) {
                    Ok(s) => V::Str(s.to_string()),
                    Err(_) => V::Bytes(raw.to_vec()),
                }
            }
            T::Bytes => {
                let n = r.packed_len()?;
                V::Bytes(r.read(n)?.to_vec())
            }
            T::Mailbox => V::Bytes(r.read(6)?.to_vec()),
            T::Dict { none, fields } => {
                if *none {
                    let flag = *r.data.get(r.pos).ok_or("index out of range")?;
                    if flag == 0 {
                        r.pos += 1;
                        return Ok(V::None);
                    }
                    if flag == 1 {
                        r.pos += 1;
                    }
                }
                let mut d = Vec::with_capacity(fields.len());
                for (name, t) in fields {
                    let v = t.decode(r)?;
                    d.push((name.clone(), v));
                }
                V::Dict(d)
            }
            T::Arr { of, n } => {
                let n = match n {
                    Some(n) => *n,
                    None => r.packed_len()?,
                };
                let mut out = Vec::with_capacity(n.min(4096));
                for _ in 0..n {
                    out.push(of.decode(r)?);
                }
                V::List(out)
            }
            T::User(inner) => match inner.as_deref() {
                None | Some(T::Bytes) => {
                    let n = r.packed_len()?;
                    V::Bytes(r.read(n)?.to_vec())
                }
                Some(t) => {
                    r.read(1)?; // header byte precedes non-blob user types
                    t.decode(r)?
                }
            },
        })
    }
}

pub struct Method {
    pub name: String,
    pub named: bool,
    pub args: Vec<(String, T)>,
}

impl Method {
    /// positional args as a list, named args as a dict
    pub fn decode(&self, r: &mut Reader) -> Result<V, String> {
        if self.named {
            let mut d = Vec::new();
            for (n, t) in &self.args {
                let v = t.decode(r)?;
                d.push((n.clone(), v));
            }
            Ok(V::Dict(d))
        } else {
            let mut l = Vec::new();
            for (_, t) in &self.args {
                l.push(t.decode(r)?);
            }
            Ok(V::List(l))
        }
    }
}

pub struct EntityDef {
    pub name: String,
    pub props: Vec<(String, T)>,
    pub methods: Vec<Method>,
}

pub struct Definitions {
    pub entities: Vec<Option<EntityDef>>,
}

impl Definitions {
    pub fn from_json(j: &J) -> Result<Self, String> {
        let list = j.as_array().ok_or("defs is not a list")?;
        let mut entities = Vec::new();
        for e in list {
            if e.is_null() {
                entities.push(None);
                continue;
            }
            let props = e["props"].as_array().ok_or("bad props")?.iter()
                .map(|p| Ok((p[0].as_str().ok_or("bad prop")?.to_string(), T::from_json(&p[1])?)))
                .collect::<Result<Vec<_>, String>>()?;
            let methods = e["methods"].as_array().ok_or("bad methods")?.iter()
                .map(|m| {
                    Ok(Method {
                        name: m[0].as_str().ok_or("bad method")?.to_string(),
                        named: m[1].as_u64() == Some(1),
                        args: m[2].as_array().ok_or("bad args")?.iter()
                            .map(|a| Ok((a[0].as_str().unwrap_or("").to_string(), T::from_json(&a[1])?)))
                            .collect::<Result<Vec<_>, String>>()?,
                    })
                })
                .collect::<Result<Vec<_>, String>>()?;
            entities.push(Some(EntityDef { name: e["name"].as_str().unwrap_or("").to_string(), props, methods }));
        }
        Ok(Definitions { entities })
    }
    /// entity type index from the wire: 1-based into entities.xml
    pub fn by_index(&self, i: i64) -> Option<usize> {
        let k = (i - 1) as usize;
        if i >= 1 && k < self.entities.len() && self.entities[k].is_some() { Some(k) } else { None }
    }
    pub fn by_name(&self, name: &str) -> Option<usize> {
        self.entities.iter().position(|e| e.as_ref().map(|e| e.name == name).unwrap_or(false))
    }
    pub fn get(&self, k: usize) -> &EntityDef {
        self.entities[k].as_ref().unwrap()
    }
}
