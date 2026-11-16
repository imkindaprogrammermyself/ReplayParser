//! NestedProperty (0x23): in-place update of part of an ARRAY / FIXED_DICT property (see wowsreplay/nested.py).
use crate::defs::{EntityDef, Reader, T};
use crate::value::V;

struct Bits<'a> {
    data: &'a [u8],
    bit: usize,
}

impl Bits<'_> {
    fn get(&mut self, n: usize) -> Result<usize, String> {
        let mut v = 0usize;
        for _ in 0..n {
            let byte = *self.data.get(self.bit >> 3).ok_or("index out of range")?;
            v = (v << 1) | ((byte >> (7 - (self.bit & 7))) & 1) as usize;
            self.bit += 1;
        }
        Ok(v)
    }
    fn rest(&self) -> &[u8] {
        let k = (self.bit + 7) >> 3;
        if k <= self.data.len() { &self.data[k..] } else { &[] }
    }
}

fn bits_for(n: usize) -> usize {
    if n <= 1 { 0 } else { (usize::BITS - (n - 1).leading_zeros()) as usize }
}

/// Apply one nested update to `values` (prop name -> value); returns the path of keys that changed.
pub fn apply(body: &[u8], edef: &EntityDef, values: &mut V) -> Result<Vec<String>, String> {
    if body.len() < 9 {
        return Err("short nested packet".into());
    }
    let is_slice = body[4] != 0;
    let size = u32::from_le_bytes(body[5..9].try_into().unwrap()) as usize;
    let payload = &body[9..(9 + size).min(body.len())];
    let mut bits = Bits { data: payload, bit: 0 };
    if bits.get(1)? == 0 {
        return Err("nested update without a property".into());
    }
    let (pname, ptype) = edef.props.get(bits.get(bits_for(edef.props.len()))?).ok_or("index out of range")?;
    let mut path = vec![pname.clone()];
    let mut obj = values.get_mut(pname).ok_or_else(|| format!("KeyError: '{pname}'"))?;
    let mut typ = ptype;
    while bits.get(1)? == 1 {
        match typ {
            T::Dict { fields, .. } => {
                let (name, ft) = fields.get(bits.get(bits_for(fields.len()))?).ok_or("index out of range")?;
                obj = obj.get_mut(name).ok_or_else(|| format!("KeyError: '{name}'"))?;
                typ = ft;
                path.push(name.clone());
            }
            T::Arr { of, .. } => {
                let len = obj.list().len();
                let i = bits.get(bits_for(len))?;
                obj = match obj {
                    V::List(l) => l.get_mut(i).ok_or("list index out of range")?,
                    _ => return Err("not a list".into()),
                };
                typ = of;
                path.push(i.to_string());
            }
            _ => return Err("cannot descend into this type".into()),
        }
    }
    match typ {
        T::Dict { fields, .. } => {
            let (name, ft) = fields.get(bits.get(bits_for(fields.len()))?).ok_or("index out of range")?;
            let v = ft.decode(&mut Reader::new(bits.rest(), 0))?;
            if let V::Dict(_) = obj {
                obj.set(name, v);
            } else {
                return Err("not a dict".into());
            }
            path.push(name.clone());
        }
        T::Arr { of, .. } => {
            let len = obj.list().len();
            let width = if is_slice { bits_for(len + 1) } else { bits_for(len) };
            let a = bits.get(width)?;
            let b = if is_slice { bits.get(width)? } else { a + 1 };
            let rest = bits.rest().to_vec();
            let mut r = Reader::new(&rest, 0);
            let mut new = Vec::new();
            while r.remaining() > 0 {
                new.push(of.decode(&mut r)?);
            }
            match obj {
                V::List(l) => {
                    let a2 = a.min(l.len());
                    let b2 = b.max(a2).min(l.len());
                    l.splice(a2..b2, new);
                }
                _ => return Err("not a list".into()),
            }
            path.push(if is_slice { format!("{a}:{b}") } else { a.to_string() });
        }
        _ => return Err("nested update on a plain value".into()),
    }
    Ok(path)
}
