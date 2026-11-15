"""Unpickle replay blobs without allowing code execution.

Several Avatar methods carry Python 2 pickles. A normal pickle.loads on a
replay you did not record can run arbitrary code, so only plain containers,
primitives and a short allowlist of game data classes are accepted. The
allowed classes are replaced by inert stand-ins that just keep their
constructor args and state; any other class or function reference fails.
"""
import io
import pickle


class Opaque:
    """Stand-in for a game class: records args/state, runs no game code."""

    def __new__(cls, *args, **kwargs):
        obj = object.__new__(cls)
        obj.args = args
        obj.state = None
        return obj

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        self.state = state

    def __repr__(self):
        return f'{type(self).__name__}{self.args or ""}{self.state or ""}'


ALLOWED = {
    ('CamouflageInfo', 'CamouflageInfo'),
    ('PlayerModeDef', 'PlayerMode'),
    ('GameParams', 'GPData'),      # content/GameParams.data (tools/import_gameparams.py)
    ('GameParams', 'TypeInfo'),
}
_STANDINS = {key: type(key[1], (Opaque,), {}) for key in ALLOWED}


class _Restricted(pickle.Unpickler):
    def find_class(self, module, name):
        try:
            return _STANDINS[(module, name)]
        except KeyError:
            raise pickle.UnpicklingError(f'blocked global {module}.{name} in replay pickle') from None


def loads(data: bytes, encoding: str = 'bytes'):
    return _Restricted(io.BytesIO(data), encoding=encoding).load()
