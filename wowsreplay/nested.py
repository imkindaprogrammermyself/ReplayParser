"""NestedProperty (0x23): in-place update of part of an ARRAY / FIXED_DICT property.

Layout: u32 entityId, u8 isSlice, u32 payloadSize, payload.
The payload starts with a bit stream (MSB first). While a 1-bit "continue"
flag is read, a child index of ceil(log2(n)) bits selects the next step of the
path (entity property, dict field, list item). Then the final container gets
either a field index (dict) or an index / slice [a:b] (list), and the new
value(s) follow as normal bytes from the next byte boundary. An empty value
on a list removes the item(s).
"""
import math
import struct

from .defs import Array, FixedDict, Reader


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.bit = 0

    def get(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.bit >> 3]
            v = (v << 1) | ((byte >> (7 - (self.bit & 7))) & 1)
            self.bit += 1
        return v

    def rest(self) -> bytes:
        return self.data[(self.bit + 7) >> 3:]


def bits_for(n: int) -> int:
    return 0 if n <= 1 else math.ceil(math.log2(n))


def apply(body: bytes, entity_def, values: dict):
    """Apply one nested update to `values` (prop name -> decoded value).

    Returns (entityId, path) where path is the list of keys that changed.
    """
    entity_id, is_slice, size = struct.unpack_from('<IBI', body)
    bits = BitReader(body[9:9 + size])

    props = entity_def.client_props
    prop = props[bits.get(bits_for(len(props)))] if bits.get(1) else None
    if prop is None:
        raise ValueError('nested update without a property')
    path = [prop.name]
    obj, typ = values[prop.name], prop.type
    while bits.get(1):
        if isinstance(typ, FixedDict):
            name, typ = typ.fields[bits.get(bits_for(len(typ.fields)))]
            obj = obj[name]
            path.append(name)
        elif isinstance(typ, Array):
            i = bits.get(bits_for(len(obj)))
            obj, typ = obj[i], typ.item
            path.append(i)
        else:
            raise ValueError(f'cannot descend into {type(typ).__name__}')

    if isinstance(typ, FixedDict):
        name, ftype = typ.fields[bits.get(bits_for(len(typ.fields)))]
        obj[name] = ftype.decode(Reader(bits.rest()))
        path.append(name)
    elif isinstance(typ, Array):
        width = bits_for(len(obj) + 1) if is_slice else bits_for(len(obj))
        a = bits.get(width)
        b = bits.get(width) if is_slice else a + 1
        r = Reader(bits.rest())
        new = []
        while r.remaining():
            new.append(typ.item.decode(r))
        obj[a:b] = new
        path.append(f'{a}:{b}' if is_slice else a)
    else:
        raise ValueError(f'nested update on {type(typ).__name__}')
    return entity_id, path
