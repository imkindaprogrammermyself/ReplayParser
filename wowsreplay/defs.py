"""Entity definitions (.def files) and BigWorld value decoding.

Wire IDs of client methods and properties are not stored anywhere: the client
derives them from the .def files. Rules (same as the game client):

* An entity's .def is read after the interfaces it `Implements` (depth first).
  A property declared again later replaces the earlier one; a method keeps
  its first declaration.
* Exposed properties are those with a client-visible flag. Their wire index is
  the position in the list stably sorted by fixed payload size (variable-size
  types count as INFINITY and sort last).
* Client methods are sorted the same way by the summed size of their args.
"""
import os
import re
import struct
import xml.etree.ElementTree as ET

INFINITY = 0xFFFF
EXPOSED_FLAGS = {'ALL_CLIENTS', 'BASE_AND_CLIENT', 'OTHER_CLIENTS', 'OWN_CLIENT', 'CELL_PUBLIC_AND_OWN'}


class Reader:
    __slots__ = ('data', 'pos')

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise EOFError(f'need {n} bytes at {self.pos}, have {len(self.data) - self.pos}')
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise EOFError(f'need {size} bytes at {self.pos}')
        values = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return values

    def packed_len(self) -> int:
        """BigWorld length prefix: u8, or 0xFF followed by a 24-bit length."""
        n = self.data[self.pos]
        self.pos += 1
        if n == 0xFF:
            lo, hi = struct.unpack_from('<HB', self.data, self.pos)
            self.pos += 3
            n = lo | (hi << 16)
        return n

    def remaining(self) -> int:
        return len(self.data) - self.pos


# ---------------------------------------------------------------- data types

class DataType:
    size = INFINITY

    def decode(self, r: Reader):
        raise NotImplementedError


class Number(DataType):
    def __init__(self, fmt: str):
        self.fmt = '<' + fmt
        self.size = struct.calcsize(self.fmt)

    def decode(self, r):
        return r.unpack(self.fmt)[0]


class Vector(DataType):
    def __init__(self, n: int):
        self.fmt = f'<{n}f'
        self.size = 4 * n

    def decode(self, r):
        return r.unpack(self.fmt)


class Bytes(DataType):
    """BLOB / PYTHON: length-prefixed raw bytes."""

    def decode(self, r):
        return r.read(r.packed_len())


class String(DataType):
    def decode(self, r):
        raw = r.read(r.packed_len())
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return raw


class Mailbox(DataType):
    def decode(self, r):
        return r.read(6)


class FixedDict(DataType):
    def __init__(self, fields: list, allow_none: bool):
        self.fields = fields          # [(name, DataType)]
        self.allow_none = allow_none
        self.size = INFINITY if allow_none else sum(t.size for _, t in fields)

    def decode(self, r):
        if self.allow_none:
            flag = r.data[r.pos]
            if flag == 0:
                r.pos += 1
                return None
            if flag == 1:
                r.pos += 1
        return {name: t.decode(r) for name, t in self.fields}


class Array(DataType):
    def __init__(self, item: DataType, fixed_len):
        self.item = item
        self.fixed_len = fixed_len
        self.size = item.size * fixed_len if fixed_len is not None else INFINITY

    def decode(self, r):
        n = self.fixed_len if self.fixed_len is not None else r.packed_len()
        return [self.item.decode(r) for _ in range(n)]


class UserType(DataType):
    def __init__(self, inner):
        self.inner = inner

    def decode(self, r):
        if self.inner is None or isinstance(self.inner, Bytes):
            return r.read(r.packed_len())
        r.read(1)  # header byte precedes non-blob user types
        return self.inner.decode(r)


SIMPLE = {
    'INT8': lambda: Number('b'), 'UINT8': lambda: Number('B'),
    'INT16': lambda: Number('h'), 'UINT16': lambda: Number('H'),
    'INT32': lambda: Number('i'), 'UINT32': lambda: Number('I'),
    'INT64': lambda: Number('q'), 'UINT64': lambda: Number('Q'),
    'FLOAT': lambda: Number('f'), 'FLOAT32': lambda: Number('f'), 'FLOAT64': lambda: Number('d'),
    'VECTOR2': lambda: Vector(2), 'VECTOR3': lambda: Vector(3), 'VECTOR4': lambda: Vector(4),
    'STRING': String, 'UNICODE_STRING': String,
    'BLOB': Bytes, 'PYTHON': Bytes, 'MAILBOX': Mailbox,
}


# ---------------------------------------------------------------- xml

_XSI = re.compile(rb'\s[\w.-]+:[\w.-]+="[^"]*"')


def _parse_xml(path):
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        # some files carry namespaced attributes without declaring the namespace
        return ET.fromstring(_XSI.sub(b'', raw))


def _text(el) -> str:
    return (el.text or '').strip()


# ---------------------------------------------------------------- definitions

class Method:
    def __init__(self, name, args, header_size):
        self.name = name
        self.args = args              # [(name or None, DataType)]
        total = sum(t.size for _, t in args)
        self.size = (INFINITY if total >= INFINITY else total) + header_size

    def decode(self, r: Reader):
        """Return positional args as a list, or named args as a dict."""
        if self.args and self.args[0][0] is not None:
            return {name: t.decode(r) for name, t in self.args}
        return [t.decode(r) for _, t in self.args]


class Property:
    def __init__(self, name, type_, flags):
        self.name = name
        self.type = type_
        self.flags = flags

    @property
    def size(self):
        return min(self.type.size, INFINITY)


class EntityDef:
    def __init__(self, name, props, methods):
        self.name = name
        exposed = [p for p in props if p.flags in EXPOSED_FLAGS]
        self.client_props = sorted(exposed, key=lambda p: p.size)          # wire index order
        self.client_methods = sorted(methods, key=lambda m: m.size)        # wire index order

    def prop_by_name(self, name):
        return next(p for p in self.client_props if p.name == name)


class Definitions:
    def __init__(self, scripts_dir: str):
        self.dir = scripts_dir
        self._alias_xml = {}
        for fn in ('alias.xml', 'alias_ext.xml'):
            path = os.path.join(scripts_dir, 'entity_defs', fn)
            if os.path.exists(path):
                for el in _parse_xml(path):
                    self._alias_xml[el.tag] = el
        self._alias_cache = {}
        root = _parse_xml(os.path.join(scripts_dir, 'entities.xml'))
        section = root.find('ClientServerEntities')
        self.entity_names = [el.tag for el in (section if section is not None else root)]
        self._entities = {}

    # types
    def type_from(self, el) -> DataType:
        name = _text(el)
        if name in self._alias_xml:
            if name not in self._alias_cache:
                self._alias_cache[name] = self.type_from(self._alias_xml[name])
            return self._alias_cache[name]
        if name in SIMPLE:
            return SIMPLE[name]()
        if name == 'FIXED_DICT':
            props = el.find('Properties')
            fields = [(p.tag, self.type_from(p.find('Type'))) for p in (props if props is not None else [])]
            none = el.find('AllowNone')
            return FixedDict(fields, none is not None and _text(none).lower() == 'true')
        if name in ('ARRAY', 'TUPLE'):
            size = el.find('size')
            return Array(self.type_from(el.find('of')), int(_text(size)) if size is not None else None)
        if name == 'USER_TYPE':
            inner = el.find('Type')
            return UserType(self.type_from(inner) if inner is not None else None)
        raise ValueError(f'unknown data type {name!r}')

    # entities
    def entity_by_index(self, type_index: int) -> EntityDef:
        """Entity type index from the wire is 1-based into entities.xml."""
        return self.entity(self.entity_names[type_index - 1])

    def entity(self, name: str) -> EntityDef:
        if name not in self._entities:
            props, methods, seen = [], [], set()
            self._collect(os.path.join(self.dir, 'entity_defs', name + '.def'), props, methods, seen)
            self._entities[name] = EntityDef(name, props, methods)
        return self._entities[name]

    def _collect(self, path, props, methods, seen):
        root = _parse_xml(path)
        implements = root.find('Implements')
        if implements is not None:
            for it in implements:
                self._collect(os.path.join(self.dir, 'entity_defs', 'interfaces', _text(it) + '.def'),
                              props, methods, seen)
        section = root.find('Properties')
        if section is not None:
            for p in section:
                props[:] = [x for x in props if x.name != p.tag]
                props.append(Property(p.tag, self.type_from(p.find('Type')), _text(p.find('Flags'))))
        section = root.find('ClientMethods')
        if section is not None:
            for m in section:
                if m.tag in seen:
                    continue
                seen.add(m.tag)
                named = m.find('Args')
                if named is not None:
                    args = [(a.tag, self.type_from(a)) for a in named]
                else:
                    args = [(None, self.type_from(a)) for a in m.findall('Arg')]
                header = m.find('VariableLengthHeaderSize')
                try:
                    header_size = int(_text(header))
                except (AttributeError, ValueError):
                    header_size = 1
                methods.append(Method(m.tag, args, header_size))


# ---------------------------------------------------------------- export (the Rust / WebAssembly parser)

def type_spec(t):
    """A DataType as compact JSON: ['n', fmt] number, ['v', n] vector, ['s'] string, ['b'] blob, ['m'] mailbox,
    ['d', allowNone, [[name, spec], ...]] fixed dict, ['a', spec, fixedLen | None] array, ['u', spec | None] user type."""
    if isinstance(t, Number):
        return ['n', t.fmt[1:]]
    if isinstance(t, Vector):
        return ['v', t.size // 4]
    if isinstance(t, String):
        return ['s']
    if isinstance(t, Mailbox):
        return ['m']
    if isinstance(t, Bytes):
        return ['b']
    if isinstance(t, FixedDict):
        return ['d', int(t.allow_none), [[n, type_spec(ft)] for n, ft in t.fields]]
    if isinstance(t, Array):
        return ['a', type_spec(t.item), t.fixed_len]
    if isinstance(t, UserType):
        return ['u', None if t.inner is None else type_spec(t.inner)]
    raise ValueError(f'cannot export {type(t).__name__}')


def export(defs: 'Definitions') -> list:
    """Every entity of entities.xml (wire type index - 1) with its client properties and methods in wire order."""
    out = []
    for name in defs.entity_names:
        try:
            e = defs.entity(name)
        except (OSError, ValueError, ET.ParseError):
            out.append(None)
            continue
        out.append({'name': name,
                    'props': [[p.name, type_spec(p.type)] for p in e.client_props],
                    'methods': [[m.name, int(bool(m.args) and m.args[0][0] is not None),
                                 [[a or '', type_spec(t)] for a, t in m.args]] for m in e.client_methods]})
    return out
