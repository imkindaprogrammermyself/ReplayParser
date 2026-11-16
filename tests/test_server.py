"""Web app round trip: upload a replay, wait for it, open the page, list it, re-upload, reject junk.

    python -m unittest tests.test_server
"""
import glob
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wowsreplay.server import make_server  # noqa: E402

REPLAYS = sorted(glob.glob(os.path.join(ROOT, '*.wowsreplay')), key=os.path.getsize)


@unittest.skipUnless(REPLAYS, 'no replays in the project folder')
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = make_server('127.0.0.1', 0, cls.tmp.name, workers=1)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.srv.RequestHandlerClass.library.pool.shutdown(wait=True)
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def upload(self, body, name='a.wowsreplay'):
        return self.req('POST', '/api/upload', body, {'X-Filename': name, 'Content-Type': 'application/octet-stream'})

    def test_round_trip(self):
        with open(REPLAYS[0], 'rb') as f:
            body = f.read()
        st, data = self.upload(body, 'my%20battle.wowsreplay')
        self.assertIn(st, (200, 202), data)
        rid = json.loads(data)['id']
        for _ in range(240):
            st, data = self.req('GET', f'/api/jobs/{rid}')
            job = json.loads(data)
            if job['status'] in ('done', 'error'):
                break
            time.sleep(.5)
        self.assertEqual(job['status'], 'done', job)
        self.assertEqual(job['meta']['file'], 'my battle.wowsreplay')

        st, page = self.req('GET', f'/r/{rid}')
        self.assertEqual(st, 200)
        self.assertIn(b'<canvas id="map">', page)
        self.assertNotIn(b'/*__REPLAY_DATA__*/null', page)
        self.assertIn(b'<script src="/static/three-0.158.0.min.js">', page)   # lazy: files by URL, not inlined
        self.assertIn(b'"/assets/', page)
        self.assertLess(len(page), 4 << 20)

        st, data = self.req('GET', '/api/replays')
        self.assertEqual([e['id'] for e in json.loads(data)], [rid])

        st, data = self.upload(body)                       # same file again: already there
        self.assertEqual((st, json.loads(data)['status']), (200, 'done'))

        lib = self.srv.RequestHandlerClass.library        # page older than the game data: rebuilt when opened
        with lib.lock:
            lib.index[rid]['stamp'] = 0
            lib.index[rid]['processed'] = 'old'
        st, page = self.req('GET', f'/r/{rid}')
        self.assertEqual(st, 200)
        self.assertNotEqual(lib.index[rid]['processed'], 'old')
        self.assertFalse(lib.stale(rid))

        st, data = self.req('POST', f'/api/replays/{rid}/rebuild', b'')   # rebuild on request
        self.assertEqual(st, 202, data)
        for _ in range(240):
            if json.loads(self.req('GET', f'/api/jobs/{rid}')[1])['status'] in ('done', 'error'):
                break
            time.sleep(.5)
        self.assertEqual(json.loads(self.req('GET', f'/api/jobs/{rid}')[1])['status'], 'done')
        self.assertIsInstance(lib.index[rid]['warnings'], list)

        self.assertEqual(self.req('DELETE', '/api/replays/' + 'f' * 64)[0], 404)   # delete
        self.assertEqual(self.req('DELETE', f'/api/replays/{rid}')[0], 200)
        self.assertEqual(json.loads(self.req('GET', '/api/replays')[1]), [])
        self.assertEqual(self.req('GET', f'/r/{rid}')[0], 404)

    def test_rejects(self):
        self.assertEqual(self.upload(b'not a replay at all')[0], 415)
        self.assertEqual(self.req('GET', '/r/' + '0' * 64)[0], 404)
        self.assertEqual(self.req('GET', '/r/../../etc/passwd')[0], 404)
        self.assertEqual(self.req('GET', '/api/jobs/xyz')[0], 404)
        self.assertEqual(self.req('POST', '/api/upload', b'', {'Content-Length': '0'})[0], 413)

    def test_assets(self):
        """Lazy pages' files: allowlisted, ETag / 304, nothing else under data/ and no path tricks."""
        models = glob.glob(os.path.join(ROOT, 'wowsreplay', 'data', 'models', '*.json'))
        if models:
            url = '/assets/models/' + os.path.basename(models[0])
            c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
            c.request('GET', url)
            r = c.getresponse(); r.read()
            self.assertEqual(r.status, 200)
            etag = r.getheader('ETag')
            c.request('GET', url, headers={'If-None-Match': etag})
            r = c.getresponse(); r.read()
            self.assertEqual(r.status, 304)
            c.close()
        self.assertEqual(self.req('GET', '/static/three-0.158.0.min.js')[0], 200)
        for bad in ('/assets/15_10_0/ships.json', '/assets/../README.md', '/assets/models/../../README.md',
                    '/assets/maps/x/../../../../README.md', '/assets/consumables.json', '/static/../README.md'):
            self.assertEqual(self.req('GET', bad)[0], 404, bad)

    def test_index_page(self):
        st, page = self.req('GET', '/')
        self.assertEqual(st, 200)
        self.assertIn(b'Replay Library', page)
