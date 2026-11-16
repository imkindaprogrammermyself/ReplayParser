//! Restricted unpickler for the Python 2 pickles inside replays (the roster). Nothing can be called: globals other
//! than four allowlisted game classes are an error, and those load as inert stand-ins (constructor args + state),
//! like wowsreplay/safe_pickle.py. Containers live in an arena so memo references see later appends.
use crate::value::V;

const ALLOWED: [(&str, &str); 4] = [
    ("CamouflageInfo", "CamouflageInfo"),
    ("PlayerModeDef", "PlayerMode"),
    ("GameParams", "GPData"),
    ("GameParams", "TypeInfo"),
];
const MAX_DEPTH: usize = 200;

#[derive(Clone)]
enum O {
    Val(V),
    List(Vec<usize>),
    Tuple(Vec<usize>),
    Dict(Vec<(usize, usize)>),
    Class(String),
    Obj(String, Option<usize>, Option<usize>),
}

#[derive(Clone, Copy)]
enum S {
    Mark,
    R(usize),
}

pub fn loads(data: &[u8]) -> Result<V, String> {
    let mut u = Unpickler { data, pos: 0, arena: Vec::new(), stack: Vec::new(), memo: Default::default() };
    let top = u.run()?;
    u.to_v(top, 0)
}

struct Unpickler<'a> {
    data: &'a [u8],
    pos: usize,
    arena: Vec<O>,
    stack: Vec<S>,
    memo: std::collections::HashMap<u64, usize>,
}

impl Unpickler<'_> {
    fn take(&mut self, n: usize) -> Result<&[u8], String> {
        let b = self.data.get(self.pos..self.pos + n).ok_or("pickle data was truncated")?;
        self.pos += n;
        Ok(b)
    }
    fn byte(&mut self) -> Result<u8, String> {
        Ok(self.take(1)?[0])
    }
    fn u32le(&mut self) -> Result<u32, String> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }
    fn line(&mut self) -> Result<String, String> {
        let start = self.pos;
        let end = self.data[start..].iter().position(|&c| c == b'\n').ok_or("pickle line not terminated")? + start;
        self.pos = end + 1;
        Ok(String::from_utf8_lossy(&self.data[start..end]).into_owned())
    }
    fn new(&mut self, o: O) -> usize {
        self.arena.push(o);
        self.arena.len() - 1
    }
    fn push(&mut self, o: O) {
        let i = self.new(o);
        self.stack.push(S::R(i));
    }
    fn pop(&mut self) -> Result<usize, String> {
        match self.stack.pop() {
            Some(S::R(i)) => Ok(i),
            _ => Err("unpickling stack underflow".into()),
        }
    }
    fn top(&self) -> Result<usize, String> {
        match self.stack.last() {
            Some(S::R(i)) => Ok(*i),
            _ => Err("unpickling stack underflow".into()),
        }
    }
    fn pop_mark(&mut self) -> Result<Vec<usize>, String> {
        let k = self.stack.iter().rposition(|s| matches!(s, S::Mark)).ok_or("could not find MARK")?;
        let items = self.stack.split_off(k + 1);
        self.stack.pop();
        Ok(items.into_iter().map(|s| if let S::R(i) = s { i } else { 0 }).collect())
    }
    fn class(&mut self, module: &str, name: &str) -> Result<O, String> {
        if ALLOWED.iter().any(|(m, n)| *m == module && *n == name) {
            Ok(O::Class(name.to_string()))
        } else {
            Err(format!("blocked global {module}.{name} in replay pickle"))
        }
    }
    fn instantiate(&mut self, cls: usize, args: Option<usize>) -> Result<O, String> {
        match &self.arena[cls] {
            O::Class(n) => Ok(O::Obj(n.clone(), args, None)),
            _ => Err("only allowlisted classes can be called".into()),
        }
    }
    fn int(&mut self, v: i64) {
        self.push(O::Val(V::Int(v)));
    }

    fn run(&mut self) -> Result<usize, String> {
        loop {
            let op = self.byte()?;
            match op {
                0x80 => { self.byte()?; }                                  // PROTO
                0x95 => { self.take(8)?; }                                 // FRAME
                b'.' => return self.pop(),                                 // STOP
                b'(' => self.stack.push(S::Mark),
                b'0' => { self.stack.pop().ok_or("stack underflow")?; }    // POP
                b'1' => { self.pop_mark()?; }                              // POP_MARK
                b'2' => { let t = self.top()?; self.stack.push(S::R(t)); } // DUP
                b'N' => self.push(O::Val(V::None)),
                0x88 => self.push(O::Val(V::Bool(true))),
                0x89 => self.push(O::Val(V::Bool(false))),
                b'I' => {
                    let l = self.line()?;
                    match l.as_str() {
                        "01" => self.push(O::Val(V::Bool(true))),
                        "00" => self.push(O::Val(V::Bool(false))),
                        s => { let v = s.parse().map_err(|_| "bad INT")?; self.int(v) }
                    }
                }
                b'L' => { let l = self.line()?; let v = l.trim_end_matches('L').parse().map_err(|_| "bad LONG")?; self.int(v) }
                b'J' => { let v = i32::from_le_bytes(self.take(4)?.try_into().unwrap()) as i64; self.int(v) }
                b'K' => { let v = self.byte()? as i64; self.int(v) }
                b'M' => { let v = u16::from_le_bytes(self.take(2)?.try_into().unwrap()) as i64; self.int(v) }
                0x8a | 0x8b => {                                           // LONG1 / LONG4
                    let n = if op == 0x8a { self.byte()? as usize } else { self.u32le()? as usize };
                    if n > 8 { return Err("long too big".into()); }
                    let b = self.take(n)?.to_vec();
                    let mut v: i64 = 0;
                    for (k, x) in b.iter().enumerate() { v |= (*x as i64) << (8 * k); }
                    if n > 0 && n < 8 && b[n - 1] & 0x80 != 0 { v -= 1i64 << (8 * n); }
                    self.int(v)
                }
                b'F' => { let l = self.line()?; let v = l.parse().map_err(|_| "bad FLOAT")?; self.push(O::Val(V::Float(v))) }
                b'G' => { let v = f64::from_be_bytes(self.take(8)?.try_into().unwrap()); self.push(O::Val(V::Float(v))) }
                b'S' => { let l = self.line()?; let v = unescape(&l)?; self.push(O::Val(V::Bytes(v))) }
                b'T' => { let n = self.u32le()? as usize; let v = self.take(n)?.to_vec(); self.push(O::Val(V::Bytes(v))) }
                b'U' => { let n = self.byte()? as usize; let v = self.take(n)?.to_vec(); self.push(O::Val(V::Bytes(v))) }
                b'B' => { let n = self.u32le()? as usize; let v = self.take(n)?.to_vec(); self.push(O::Val(V::Bytes(v))) }
                b'C' => { let n = self.byte()? as usize; let v = self.take(n)?.to_vec(); self.push(O::Val(V::Bytes(v))) }
                0x8e => { let n = u64::from_le_bytes(self.take(8)?.try_into().unwrap()) as usize; let v = self.take(n)?.to_vec(); self.push(O::Val(V::Bytes(v))) }
                b'V' => { let l = self.line()?; self.push(O::Val(V::Str(l))) }
                b'X' => { let n = self.u32le()? as usize; let v = String::from_utf8_lossy(self.take(n)?).into_owned(); self.push(O::Val(V::Str(v))) }
                0x8c => { let n = self.byte()? as usize; let v = String::from_utf8_lossy(self.take(n)?).into_owned(); self.push(O::Val(V::Str(v))) }
                0x8d => { let n = u64::from_le_bytes(self.take(8)?.try_into().unwrap()) as usize; let v = String::from_utf8_lossy(self.take(n)?).into_owned(); self.push(O::Val(V::Str(v))) }
                b']' => self.push(O::List(vec![])),
                b'l' => { let items = self.pop_mark()?; self.push(O::List(items)) }
                b'a' => { let v = self.pop()?; let l = self.top()?; self.append(l, vec![v])? }
                b'e' => { let items = self.pop_mark()?; let l = self.top()?; self.append(l, items)? }
                b'}' => self.push(O::Dict(vec![])),
                b'd' => { let items = self.pop_mark()?; let d = items.chunks(2).filter(|c| c.len() == 2).map(|c| (c[0], c[1])).collect(); self.push(O::Dict(d)) }
                b's' => { let v = self.pop()?; let k = self.pop()?; let d = self.top()?; self.setitems(d, vec![k, v])? }
                b'u' => { let items = self.pop_mark()?; let d = self.top()?; self.setitems(d, items)? }
                b')' => self.push(O::Tuple(vec![])),
                b't' => { let items = self.pop_mark()?; self.push(O::Tuple(items)) }
                0x85 => { let a = self.pop()?; self.push(O::Tuple(vec![a])) }
                0x86 => { let b = self.pop()?; let a = self.pop()?; self.push(O::Tuple(vec![a, b])) }
                0x87 => { let c = self.pop()?; let b = self.pop()?; let a = self.pop()?; self.push(O::Tuple(vec![a, b, c])) }
                0x8f => self.push(O::List(vec![])),                         // EMPTY_SET (as a list)
                0x90 => { let items = self.pop_mark()?; let l = self.top()?; self.append(l, items)? }
                0x91 => { let items = self.pop_mark()?; self.push(O::Tuple(items)) }
                b'p' => { let k = self.line()?.parse().map_err(|_| "bad PUT")?; let t = self.top()?; self.memo.insert(k, t); }
                b'q' => { let k = self.byte()? as u64; let t = self.top()?; self.memo.insert(k, t); }
                b'r' => { let k = self.u32le()? as u64; let t = self.top()?; self.memo.insert(k, t); }
                0x94 => { let k = self.memo.len() as u64; let t = self.top()?; self.memo.insert(k, t); }
                b'g' => { let k: u64 = self.line()?.parse().map_err(|_| "bad GET")?; self.get(k)? }
                b'h' => { let k = self.byte()? as u64; self.get(k)? }
                b'j' => { let k = self.u32le()? as u64; self.get(k)? }
                b'c' => { let m = self.line()?; let n = self.line()?; let o = self.class(&m, &n)?; self.push(o) }
                0x93 => {
                    let n = self.pop()?; let m = self.pop()?;
                    let (m, n) = (self.text(m)?, self.text(n)?);
                    let o = self.class(&m, &n)?; self.push(o)
                }
                b'i' => { let m = self.line()?; let n = self.line()?; let c = self.class(&m, &n)?; let args = self.pop_mark()?;
                          let ci = self.new(c); let a = self.new(O::Tuple(args)); let o = self.instantiate(ci, Some(a))?; self.push(o) }
                b'o' => { let items = self.pop_mark()?; let (c, rest) = items.split_first().ok_or("OBJ without a class")?;
                          let a = self.new(O::Tuple(rest.to_vec())); let o = self.instantiate(*c, Some(a))?; self.push(o) }
                b'R' | 0x81 => { let args = self.pop()?; let c = self.pop()?; let o = self.instantiate(c, Some(args))?; self.push(o) }
                0x92 => { let _kw = self.pop()?; let args = self.pop()?; let c = self.pop()?; let o = self.instantiate(c, Some(args))?; self.push(o) }
                b'b' => {
                    let state = self.pop()?; let obj = self.top()?;
                    match &mut self.arena[obj] {
                        O::Obj(_, _, st) => *st = Some(state),
                        _ => return Err("BUILD on something that is not an allowlisted object".into()),
                    }
                }
                b'P' | b'Q' => return Err("A load persistent id instruction was encountered".into()),
                0x82..=0x84 => return Err("blocked global from the extension registry in replay pickle".into()),
                _ => return Err(format!("unsupported pickle opcode {op:#x}")),
            }
        }
    }

    fn get(&mut self, k: u64) -> Result<(), String> {
        let i = *self.memo.get(&k).ok_or("memo key not found")?;
        self.stack.push(S::R(i));
        Ok(())
    }
    fn text(&self, i: usize) -> Result<String, String> {
        match &self.arena[i] {
            O::Val(v) => v.text().ok_or_else(|| "global name is not a string".into()),
            _ => Err("global name is not a string".into()),
        }
    }
    fn append(&mut self, l: usize, items: Vec<usize>) -> Result<(), String> {
        match &mut self.arena[l] {
            O::List(v) => { v.extend(items); Ok(()) }
            _ => Err("append to a non-list".into()),
        }
    }
    fn setitems(&mut self, d: usize, items: Vec<usize>) -> Result<(), String> {
        match &mut self.arena[d] {
            O::Dict(v) => { v.extend(items.chunks(2).filter(|c| c.len() == 2).map(|c| (c[0], c[1]))); Ok(()) }
            _ => Err("setitem on a non-dict".into()),
        }
    }
    fn to_v(&self, i: usize, depth: usize) -> Result<V, String> {
        if depth > MAX_DEPTH {
            return Err("pickle nested too deep".into());
        }
        Ok(match &self.arena[i] {
            O::Val(v) => v.clone(),
            O::List(l) | O::Tuple(l) => V::List(l.iter().map(|&x| self.to_v(x, depth + 1)).collect::<Result<_, _>>()?),
            O::Dict(d) => V::Dict(d.iter().map(|&(k, v)| {
                let key = match self.to_v(k, depth + 1)? {
                    V::Str(s) => s,
                    V::Bytes(b) => String::from_utf8_lossy(&b).into_owned(),
                    V::Int(n) => n.to_string(),
                    other => format!("{other:?}"),
                };
                Ok((key, self.to_v(v, depth + 1)?))
            }).collect::<Result<_, String>>()?),
            O::Class(n) => V::Str(format!("<class {n}>")),
            O::Obj(n, a, s) => V::Opaque(n.clone(),
                Box::new(a.map(|x| self.to_v(x, depth + 1)).transpose()?.unwrap_or(V::None)),
                Box::new(s.map(|x| self.to_v(x, depth + 1)).transpose()?.unwrap_or(V::None))),
        })
    }
}

/// the Python 2 'S' opcode's repr string ('...' or "..." with backslash escapes) -> bytes
fn unescape(s: &str) -> Result<Vec<u8>, String> {
    let b = s.as_bytes();
    if b.len() < 2 || !(b[0] == b'\'' || b[0] == b'"') || b[b.len() - 1] != b[0] {
        return Err("the STRING opcode argument must be quoted".into());
    }
    let inner = &b[1..b.len() - 1];
    let mut out = Vec::with_capacity(inner.len());
    let mut i = 0;
    while i < inner.len() {
        if inner[i] != b'\\' || i + 1 >= inner.len() {
            out.push(inner[i]);
            i += 1;
            continue;
        }
        let c = inner[i + 1];
        i += 2;
        match c {
            b'n' => out.push(b'\n'),
            b't' => out.push(b'\t'),
            b'r' => out.push(b'\r'),
            b'\\' => out.push(b'\\'),
            b'\'' => out.push(b'\''),
            b'"' => out.push(b'"'),
            b'x' => {
                let h = std::str::from_utf8(inner.get(i..i + 2).ok_or("bad \\x escape")?).map_err(|_| "bad \\x")?;
                out.push(u8::from_str_radix(h, 16).map_err(|_| "bad \\x escape")?);
                i += 2;
            }
            b'0'..=b'7' => {
                let mut v = (c - b'0') as u32;
                for _ in 0..2 {
                    if i < inner.len() && (b'0'..=b'7').contains(&inner[i]) {
                        v = v * 8 + (inner[i] - b'0') as u32;
                        i += 1;
                    }
                }
                out.push(v as u8);
            }
            other => { out.push(b'\\'); out.push(other); }
        }
    }
    Ok(out)
}
