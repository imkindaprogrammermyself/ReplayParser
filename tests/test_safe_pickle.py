"""The replay unpickler must never run code from a pickle: every global is blocked except the allowlist, and even
those load as inert stand-ins.

    python -m unittest tests.test_safe_pickle
"""
import copyreg
import os
import pickle
import pickletools
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wowsreplay import safe_pickle  # noqa: E402

FIRED = []


def _fire(*args):
    """What an attack would run (os.system, eval, ...); records the call instead."""
    FIRED.append(args)
    return 'pwned'


class Evil:
    def __init__(self, func=_fire, args=('payload',)):
        self.func, self.args = func, args

    def __reduce__(self):
        return self.func, self.args


def global_op(module, name, proto0=True):
    """A pickle that only pushes module.name and calls it - hand-built, so nothing here runs it."""
    if proto0:   # GLOBAL opcode (protocol 0-2, what Python 2 pickles in replays use)
        return f'c{module}\n{name}\n(S\'x\'\ntR.'.encode()
    # STACK_GLOBAL (protocol 4)
    m, n = module.encode(), name.encode()
    return (b'\x80\x04' + b'\x8c' + bytes([len(m)]) + m + b'\x8c' + bytes([len(n)]) + n + b'\x93'
            + b'\x8c\x01x' + b'\x85R.')


class SafePickleTests(unittest.TestCase):
    def setUp(self):
        FIRED.clear()

    def assertBlocked(self, data, why='blocked global'):
        with self.assertRaisesRegex(pickle.UnpicklingError, why):
            safe_pickle.loads(data)
        self.assertEqual(FIRED, [])

    def test_plain_data_loads(self):
        value = {'a': [1, 2.5, ('x', None, True)], 3: {'nested': [-7, 2 ** 70]}}
        for proto in (0, 2, 4, 5):
            self.assertEqual(safe_pickle.loads(pickle.dumps(value, proto)), value)
        # bytes: Python 3 pickles them at protocol 0-2 via _codecs.encode (blocked, fine - replays are Python 2
        # pickles, whose strings are plain S opcodes); protocol 3+ has a bytes opcode
        self.assertEqual(safe_pickle.loads(pickle.dumps({b'k': b'v'}, 4)), {b'k': b'v'})
        self.assertEqual(safe_pickle.loads(b"(dS'k'\nS'v'\ns."), {b'k': b'v'})

    def test_reduce_to_any_function_blocked(self):
        for proto in range(0, pickle.HIGHEST_PROTOCOL + 1):
            self.assertBlocked(pickle.dumps(Evil(), proto))

    def test_classic_payloads_blocked(self):
        for module, name in [('os', 'system'), ('posix', 'system'), ('nt', 'system'), ('subprocess', 'Popen'),
                             ('builtins', 'eval'), ('builtins', 'exec'), ('__builtin__', 'eval'),
                             ('builtins', 'getattr'), ('builtins', '__import__'), ('importlib', 'import_module'),
                             ('copy_reg', '_reconstructor'), ('copyreg', '_reconstructor'), ('pickle', 'loads'),
                             ('wowsreplay.safe_pickle', 'loads'), (__name__, '_fire')]:
            for proto0 in (True, False):
                self.assertBlocked(global_op(module, name, proto0))

    def test_inst_and_obj_opcodes_blocked(self):
        self.assertBlocked(f'(S\'payload\'\ni{__name__}\n_fire\n.'.encode())          # INST
        self.assertBlocked(f'(c{__name__}\n_fire\nS\'payload\'\no.'.encode())         # OBJ

    def test_extension_registry_blocked(self):
        copyreg.add_extension(__name__, '_fire', 0x7ffffff0)   # EXT4 resolves through find_class too
        try:
            self.assertBlocked(b'\x80\x02\x84\xf0\xff\xff\x7fU\x01x\x85R.')
        finally:
            copyreg.remove_extension(__name__, '_fire', 0x7ffffff0)

    def test_persistent_id_blocked(self):
        self.assertBlocked(b'Pfoo\n.', 'persistent')

    def test_allowlisted_names_only_in_their_module(self):
        self.assertBlocked(global_op('os', 'CamouflageInfo'))
        self.assertBlocked(global_op('CamouflageInfo', 'system'))

    def test_allowed_classes_are_inert(self):
        """An allowlisted class loads as a stand-in that only keeps its args / state - no game code, no attributes
        that could be called."""
        data = (b'(cPlayerModeDef\nPlayerMode\n(S\'arg\'\ntR(dS\'k\'\nI1\nsb.')
        obj = safe_pickle.loads(data)
        self.assertIsInstance(obj, safe_pickle.Opaque)
        self.assertEqual(type(obj).__name__, 'PlayerMode')
        self.assertEqual(obj.args, (b'arg',))
        self.assertEqual(obj.state, {b'k': 1})
        # BUILD with a state that names a function can't make it run either
        self.assertBlocked(f'cPlayerModeDef\nPlayerMode\n)R(dS\'k\'\nc{__name__}\n_fire\nsb.'.encode())

    def test_payloads_do_run_code_with_plain_pickle(self):
        """The hand-built payloads are real attacks: plain pickle.loads runs them (aimed at the harmless _fire)."""
        payloads = [global_op(__name__, '_fire'), global_op(__name__, '_fire', False), pickle.dumps(Evil(), 2),
                    f'(S\'payload\'\ni{__name__}\n_fire\n.'.encode(),       # INST
                    f'(c{__name__}\n_fire\nS\'payload\'\no.'.encode()]      # OBJ
        for data in payloads:
            with open(os.devnull, 'w') as null:
                pickletools.dis(data, out=null)   # well-formed
            FIRED.clear()
            pickle.loads(data)          # the point of the test; the target only appends to a list
            self.assertEqual(len(FIRED), 1, data)
            FIRED.clear()
            self.assertBlocked(data)


if __name__ == '__main__':
    unittest.main()
