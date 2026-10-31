"""Tests for the Polar connector against a fake Polar AccessLink built from the official v3 docs
(https://www.polar.com/accesslink-api/): Basic-auth token endpoint, key test, OAuth state, the one-time user
registration (POST /v3/users, 409 = already registered, 403 = consents missing), windowed date-range paging
(28-day windows, max 365 days back), 429 with RateLimit-Reset, 401 = sign-in ended (Polar has no refresh tokens),
the wizard.health-data/1 contract (normalized days, paging, changed_since cursor, dataset id, box-only reads), the
documented Nightly Recharge and Cardio Load formulas, HTTP/MCP guards and disconnect (DELETE /v3/users/{id}).
Run: python3 -m unittest discover -s tests"""
import base64
import copy
import datetime as dt
import importlib
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
CID, SECRET = 'd52d533a-3c71-4141-aea6-3402519678aa', 'test-secret-value-XYZ-123'
TODAY = dt.datetime.now(dt.timezone.utc).date()
ACT_DAYS = 40

# ---- sample payloads, copied from the AccessLink v3 documentation's example responses ----
DOC_SLEEP = {"polar_user": "https://www.polaraccesslink/v3/users/1", "date": "2020-01-01",
             "sleep_start_time": "2020-01-01T00:39:07+03:00", "sleep_end_time": "2020-01-01T09:19:37+03:00",
             "device_id": "1111AAAA", "continuity": 2.1, "continuity_class": 2, "light_sleep": 1000, "deep_sleep": 1000,
             "rem_sleep": 1000, "unrecognized_sleep_stage": 1000, "sleep_score": 80, "total_interruption_duration": 1000,
             "sleep_charge": 3, "sleep_goal": 28800, "sleep_rating": 3, "short_interruption_duration": 500,
             "long_interruption_duration": 300, "sleep_cycles": 6, "group_duration_score": 100, "group_solidity_score": 75,
             "group_regeneration_score": 54.2, "hypnogram": {"00:39": 2, "00:50": 3, "01:23": 5},
             "heart_rate_samples": {"00:41": 76, "00:46": 77, "00:51": 76}}
DOC_RECHARGE = {"polar_user": "https://www.polaraccesslink/v3/users/1", "date": "2020-01-01", "heart_rate_avg": 70,
                "beat_to_beat_avg": 816, "heart_rate_variability_avg": 28, "breathing_rate_avg": 14.1,
                "nightly_recharge_status": 3, "ans_charge": 0, "ans_charge_status": 3,
                "hrv_samples": {"00:41": 14, "00:46": 14, "00:51": 15}, "breathing_samples": {"00:39": 13.4, "00:44": 13.5, "00:49": 13.5}}
DOC_ACTIVITY = {"start_time": "2025-08-13T08:15:30", "end_time": "2025-08-13T23:59:59", "active_duration": "PT3H11M",
                "inactive_duration": "PT18H23M30S", "daily_activity": 89.1, "calories": 2500, "active_calories": 1500,
                "steps": 8823, "inactivity_alert_count": 1, "distance_from_steps": 4590.53}
DOC_EXERCISE = {"id": "2AC312F", "upload_time": "2008-10-13T10:40:02.000Z", "polar_user": "https://www.polaraccesslink/v3/users/1",
                "device": "Polar M400", "device_id": "1111AAAA", "start_time": "2008-10-13T10:40:02", "start_time_utc_offset": 180,
                "duration": "PT2H44M", "calories": 530, "distance": 1600, "heart_rate": {"average": 129, "maximum": 147},
                "training_load": 143.22, "sport": "OTHER", "has_route": True, "club_id": 999, "club_name": "Polar Club",
                "detailed_sport_info": "WATERSPORTS_WATERSKI", "fat_percentage": 60, "carbohydrate_percentage": 38,
                "protein_percentage": 2, "running-index": 51,
                "heart_rate_zones": [{"index": 1, "lower-limit": 110, "upper-limit": 130, "in-zone": "PT4S"}],
                "training_load_pro": {"date": "2023-01-01", "cardio-load": 1, "muscle-load": 1, "perceived-load": 1,
                                      "cardio-load-interpretation": "UNKNOWN", "muscle-load-interpretation": "UNKNOWN",
                                      "perceived-load-interpretation": "UNKNOWN", "user-rpe": "UNKNOWN"}}
DOC_CARDIO = {"date": "2023-01-01", "cardio_load_status": "LOAD_STATUS_NOT_AVAILABLE", "cardio_load": 0.2, "strain": 0.3,
              "tolerance": 0.4, "cardio_load_ratio": 0.1,
              "cardio_load_level": {"very_low": 0.1, "low": 0.1, "medium": 0.1, "high": 0.1, "very-high": 0.1}}
DOC_USER = {"polar-user-id": 2278512, "member-id": "i09u9ujj", "registration-date": "2011-10-14T12:50:37.000Z",
            "first-name": "Eka", "last-name": "Toka", "birthdate": "1985-09-06", "gender": "MALE", "weight": 66, "height": 170,
            "extra-info": [{"value": "2", "index": 0, "name": "number-of-children"}]}


def day(i):
    """i days before today."""
    return (TODAY - dt.timedelta(days=i)).isoformat()


def make_data():
    sleeps, recharges, acts, cardio, ex = [], [], [], {}, []
    for i in range(ACT_DAYS):
        d = day(i)
        a = dict(DOC_ACTIVITY, start_time=d + 'T00:00:00', end_time=d + 'T23:59:59', steps=6000 + 100 * i, calories=2200 + i)
        acts.append(a)
        load = 0.0 if i % 4 == 3 else 40.0 + 10 * (i % 6)
        cardio[d] = dict(DOC_CARDIO, date=d, cardio_load=load,
                         cardio_load_status='LOAD_STATUS_NOT_AVAILABLE' if load == 0 else 'PRODUCTIVE')
    for i in range(28):
        d = day(i)
        prev = day(i + 1)
        sleeps.append(dict(DOC_SLEEP, date=d, sleep_start_time=prev + 'T23:10:00+07:00', sleep_end_time=d + 'T07:10:00+07:00',
                           light_sleep=14400, deep_sleep=5400, rem_sleep=6300, unrecognized_sleep_stage=0,
                           total_interruption_duration=2400, sleep_score=70 + i % 20, sleep_goal=28800))
        recharges.append(dict(DOC_RECHARGE, date=d, nightly_recharge_status=1 + i % 6, ans_charge=round(-8 + (i % 9) * 2, 1),
                              heart_rate_variability_avg=40 + i % 7, heart_rate_avg=52 + i % 4))
    for i in range(0, 30, 3):
        d = day(i)
        ex.append(dict(DOC_EXERCISE, id=f'EX{i:03d}', start_time=d + 'T18:00:00', start_time_utc_offset=420, duration='PT45M',
                       detailed_sport_info='RUNNING', sport='RUNNING', calories=480, distance=7000,
                       heart_rate={'average': 148, 'maximum': 176}, training_load=70.0,
                       training_load_pro=dict(DOC_EXERCISE['training_load_pro'], **{'cardio-load': 70.0}),
                       heart_rate_zones=[{'index': z, 'in-zone': f'PT{z * 3}M'} for z in range(1, 6)]))
    return {'sleep': sleeps, 'recharge': recharges, 'activity': acts, 'cardio': cardio, 'exercise': ex}


class Fake:
    def __init__(self):
        self.data = make_data()
        self.access = None
        self.registered = False
        self.calls = []
        self.force_401 = 0
        self.force_429 = 0
        self.force_403_register = False
        self.deleted = False


FAKE = Fake()


class FakePolar(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=None, headers=None):
        b = b'' if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def authed(self):
        if self.headers.get('Authorization') != 'Bearer ' + str(FAKE.access):
            self.reply(401)
            return False
        if FAKE.force_401:
            FAKE.force_401 -= 1
            self.reply(401)
            return False
        return True

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n).decode()
        u = urlsplit(self.path)
        if u.path == '/v2/oauth2/token':
            f = {k: v[0] for k, v in parse_qs(raw).items()}
            FAKE.calls.append(('POST', u.path, f.get('grant_type'), self.headers.get('Authorization')))
            want = 'Basic ' + base64.b64encode(f'{CID}:{SECRET}'.encode()).decode()
            if self.headers.get('Authorization') != want or 'client_secret' in f:
                return self.reply(401, {'error': 'unauthorized_client'})       # what the real endpoint answers for unknown clients
            if f.get('grant_type') == 'authorization_code' and f.get('code') == 'good-code' and f.get('redirect_uri'):
                FAKE.access = 'acc-' + os.urandom(6).hex()
                return self.reply(200, {'access_token': FAKE.access, 'token_type': 'bearer', 'expires_in': 31535999, 'x_user_id': 10579})
            return self.reply(400, {'error': 'invalid_grant'})
        if u.path == '/v3/users':
            FAKE.calls.append(('POST', u.path, json.loads(raw or '{}'), None))
            if not self.authed():
                return
            if FAKE.force_403_register:
                return self.reply(403)
            if FAKE.registered:
                return self.reply(409)
            FAKE.registered = True
            return self.reply(200, DOC_USER)
        self.reply(404)

    def do_DELETE(self):
        FAKE.calls.append(('DELETE', self.path, None, None))
        if self.path == '/v3/users/10579' and self.authed():
            FAKE.deleted = True
            FAKE.registered = False
            self.send_response(204)
            self.end_headers()

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FAKE.calls.append(('GET', u.path, q, None))
        if not self.authed():
            return
        if FAKE.force_429:
            FAKE.force_429 -= 1
            return self.reply(429, None, {'RateLimit-Reset': '0, 86000', 'RateLimit-Usage': '520, 900', 'RateLimit-Limit': '520, 5100'})
        if not FAKE.registered:
            return self.reply(403)                      # data needs a registered user
        D = FAKE.data
        if u.path == '/v3/users/10579':
            return self.reply(200, DOC_USER)
        if u.path == '/v3/users/sleep':
            return self.reply(200, {'nights': D['sleep']})
        if u.path == '/v3/users/nightly-recharge':
            return self.reply(200, {'recharges': D['recharge']})
        if u.path == '/v3/exercises':
            return self.reply(200, D['exercise'])
        if u.path in ('/v3/users/activities', '/v3/users/cardio-load/date'):
            try:
                a, b = dt.date.fromisoformat(q['from']), dt.date.fromisoformat(q['to'])
            except (KeyError, ValueError):
                return self.reply(400)
            if (TODAY - a).days > 365 or b < a or (b - a).days > 27 or b > TODAY:
                return self.reply(400)     # too old, too long or in the future (Polar: 'Invalid or too old date')
            span = [(a + dt.timedelta(days=k)).isoformat() for k in range((b - a).days + 1)]
            if u.path == '/v3/users/activities':
                return self.reply(200, [x for x in D['activity'] if x['start_time'][:10] in span])
            # Cardio Load lists every day of the range, with LOAD_STATUS_NOT_AVAILABLE where nothing was calculated
            return self.reply(200, [D['cardio'].get(s) or dict(DOC_CARDIO, date=s, cardio_load=0.0) for s in span])
        self.reply(404)


def http(port, method, path, body=None, headers=None):
    h = {'Host': f'127.0.0.1:{port}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class PolarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakePolar)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fp = cls.fake.server_address[1]
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({'DATA_DIR': cls.tmp + '/private', 'POLAR_TOKEN_URL': f'http://127.0.0.1:{fp}/v2/oauth2/token',
                           'POLAR_AUTH_URL': f'http://127.0.0.1:{fp}/oauth2/authorization', 'POLAR_API_BASE': f'http://127.0.0.1:{fp}',
                           'POLAR_REQUEST_GAP': '0', 'POLAR_BACKOFF': '0.01', 'PORT': '18998', 'WIZARD_TRUSTED_HOSTS': 'box.example.ts.net'})
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = importlib.reload(server)
        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), cls.S.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.port = cls.app.server_address[1]
        cls.S.TRUSTED_HOSTS.add(f'127.0.0.1:{cls.port}')

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.app.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files_text(self):
        return ''.join(p.read_bytes().decode('latin-1') for p in Path(self.tmp).rglob('*') if p.is_file())

    def wait_sync(self, timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            if not self.S.SYNC_LOCK.locked() and self.S._state().get('last_sync'):
                time.sleep(0.05)
                if not self.S.SYNC_LOCK.locked():
                    return
            time.sleep(0.05)
        self.fail('sync did not finish')

    def connect(self):
        start = self.S.login_start()
        state = parse_qs(urlsplit(start['authorize_url']).query)['state'][0]
        return self.S.login_finish(f'http://localhost:3000/callback?code=good-code&state={state}')

    def test_flow(self):
        S = self.S
        # ---- from scratch
        self.assertEqual(S.status_info()['status'], 'not_configured')
        info = S.setup_info()
        self.assertEqual(info['scopes'], ['accesslink.read_all'])
        self.assertEqual(info['data_types'], ['Exercise data', 'Daily activity data', 'Physical information data'])
        self.assertEqual(info['dashboard_url'], 'https://admin.polaraccesslink.com')
        with self.assertRaises(S.Problem):
            S.login_start()

        # ---- step 4: keys through the HTTP API the form uses; secret never echoed; Test uses Basic auth
        code, _ = http(self.port, 'POST', '/api/keys', {'client_id': 'x', 'client_secret': SECRET, 'redirect_uri': 'http://localhost:3000/callback'})
        self.assertEqual(code, 400)
        http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': 'wrong-secret-value', 'redirect_uri': 'http://localhost:3000/callback'})
        code, body = http(self.port, 'POST', '/api/keys/test', {})
        self.assertEqual(json.loads(body)['result'], 'invalid')
        self.assertIn('did not accept', json.loads(body)['message'])
        code, body = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': 'http://localhost:3000/callback'})
        self.assertEqual(code, 200)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(CID.encode(), body)
        code, body = http(self.port, 'POST', '/api/keys/test', {})
        self.assertEqual(json.loads(body)['result'], 'valid', body)
        tok = [c for c in FAKE.calls if c[1] == '/v2/oauth2/token'][-1]
        self.assertTrue(tok[3].startswith('Basic '))
        self.assertEqual(oct(os.stat(S.DATA / 'client.json').st_mode & 0o777), '0o600')
        self.assertEqual(oct(os.stat(S.DATA).st_mode & 0o777), '0o700')
        code, _ = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': 'http://box.example/callback'})
        self.assertEqual(code, 400)

        # ---- step 5: OAuth state handling
        start = S.login_start()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        self.assertEqual((q['response_type'], q['scope'], q['client_id'], q['redirect_uri']), ('code', 'accesslink.read_all', CID, 'http://localhost:3000/callback'))
        self.assertTrue(start['paste_back'])
        with self.assertRaises(S.Problem):
            S.login_finish('http://localhost:3000/callback?code=good-code&state=not-the-state')
        with self.assertRaises(S.Problem) as e:
            S.login_finish(f"http://localhost:3000/callback?error=access_denied&state={q['state']}")
        self.assertIn('not to allow', e.exception.message)
        with self.assertRaises(S.Problem):
            S.login_finish('http://localhost:3000/callback?code=bad-code&state=' + parse_qs(urlsplit(S.login_start()['authorize_url']).query)['state'][0])

        # consents missing at registration -> sign in again after accepting them
        FAKE.force_403_register = True
        with self.assertRaises(S.Problem) as e:
            self.connect()
        self.assertIn('account.polar.com', e.exception.message)
        self.assertEqual(S.status_info()['status'], 'needs_login')
        self.assertFalse((S.DATA / 'tokens.json').exists())
        FAKE.force_403_register = False

        # ---- connect; registration happens once; first data call gets a 429 with RateLimit-Reset
        FAKE.calls.clear()
        FAKE.force_429 = 1
        start = S.login_start()
        state = parse_qs(urlsplit(start['authorize_url']).query)['state'][0]
        code, body = http(self.port, 'POST', '/api/oauth/finish', {'url': f'http://localhost:3000/callback?code=good-code&state={state}'})
        self.assertEqual(code, 200, body)
        self.wait_sync()
        with self.assertRaises(S.Problem):          # state is single-use
            S.login_finish(f'http://localhost:3000/callback?code=good-code&state={state}')
        reg = [c for c in FAKE.calls if c[:2] == ('POST', '/v3/users')]
        self.assertEqual(len(reg), 1)
        self.assertRegex(reg[0][2]['member-id'], r'^wizard-[0-9a-f]{16}$')
        st = S.status_info()
        self.assertEqual(st['status'], 'connected', st)
        self.assertTrue(st['setup']['registered'])
        self.assertEqual(st['account'], {'name': 'Eka'})
        self.assertEqual(st['source'], {'id': 'wizard-polar', 'provider': 'polar', 'name': 'Polar', 'app_path': '/wizard-polar/'})
        self.assertEqual(st['scales']['load']['scale_max'], 21)
        self.assertEqual(st['scales']['readiness']['label'], 'Nightly Recharge')
        self.assertEqual(st['counts'], {'days': ACT_DAYS, 'sleeps': 28, 'workouts': 10}, st)
        self.assertEqual(oct(os.stat(S.DATA / 'tokens.json').st_mode & 0o777), '0o600')
        # date-range paging: 28-day windows from 364 days back to today, oldest first, no gaps
        acts = [c[2] for c in FAKE.calls if c[1] == '/v3/users/activities']
        wins = [(dt.date.fromisoformat(x['from']), dt.date.fromisoformat(x['to'])) for x in acts]
        self.assertEqual(wins[0][0], TODAY - dt.timedelta(days=364))
        self.assertTrue(all((b - a).days <= 27 for a, b in wins))
        self.assertTrue(all(wins[k + 1][0] == wins[k][1] + dt.timedelta(days=1) for k in range(len(wins) - 1)))
        self.assertGreaterEqual(wins[-1][1], TODAY)
        self.assertEqual(len([c for c in FAKE.calls if c[1] == '/v3/users/cardio-load/date']), len(wins))
        self.assertTrue(any(c[1] == '/v3/exercises' and c[2].get('zones') == 'true' for c in FAKE.calls))

        # ---- the contract: a complete day
        d0 = day(0)
        r = S.contract_days({'from': d0, 'to': d0})
        self.assertEqual((r['contract'], r['dataset']), ('wizard.health-data/1', st['dataset']))
        d = r['days'][0]
        check_day(self, d)
        nr = FAKE.data['recharge'][0]
        self.assertEqual(d['readiness']['score'], S.readiness_score(nr['nightly_recharge_status'], nr['ans_charge']))
        self.assertEqual(d['readiness']['state'], 'scored')
        self.assertEqual((d['hrv_ms'], d['resting_hr_bpm'], d['respiratory_rate']), (40, 52, 14.1))
        sl = d['sleep']
        self.assertEqual((sl['asleep_h'], sl['in_bed_h'], sl['deep_h'], sl['rem_h'], sl['light_h'], sl['awake_h']), (7.25, 8.0, 1.5, 1.75, 4.0, 0.67))
        self.assertEqual((sl['need_h'], sl['score'], sl['efficiency_pct']), (8.0, 70, 91))
        self.assertEqual(sl['end'], (TODAY.isoformat() + 'T00:10:00.000Z'))
        self.assertEqual(d['load']['value'], S.load_equivalent(40.0))
        self.assertEqual(d['load']['native'], {'value': 40.0, 'label': 'Cardio load (TRIMP)', 'unit': 'TRIMP'})
        self.assertEqual((d['load']['max'], d['load']['scale_max'], d['load']['label']), (21, 21, 'Cardio load'))
        self.assertEqual((d['steps'], d['calories_kcal']), (6000, 2200))
        w = d['workouts'][0]
        self.assertEqual((w['sport'], w['minutes'], w['avg_hr_bpm'], w['distance_m'], w['calories_kcal']), ('Running', 45, 148, 7000, 480))
        self.assertEqual(w['start'], (TODAY - dt.timedelta(days=0)).isoformat() + 'T11:00:00.000Z')
        self.assertEqual(w['zones_min'], [None, 3.0, 6.0, 9.0, 12.0, 15.0])
        self.assertEqual(w['load']['value'], S.load_equivalent(70.0))
        self.assertEqual(d['extras']['polar']['nightly_recharge_status'], nr['nightly_recharge_status'])
        self.assertEqual(d['extras']['polar']['active_min'], 191)
        # a day with no Cardio Load calculated (NOT_AVAILABLE) and no exercise: load is null, never invented
        d3 = S.contract_days({'from': day(7), 'to': day(7)})['days'][0]
        self.assertIsNone(d3['load'])
        self.assertEqual(d3['workouts'], [])
        # older than 28 days: activity only
        old = S.contract_days({'from': day(35), 'to': day(35)})['days'][0]
        self.assertIsNone(old['readiness'])
        self.assertIsNone(old['sleep'])
        self.assertEqual(old['steps'], 6000 + 3500)
        with self.assertRaises(S.Problem):
            S.contract_days({'from': 'yesterday'})
        with self.assertRaises(S.Problem):
            S.contract_days({'changed_since': 'w1.3'})

        # paging: pages of 15, oldest first, no gaps or repeats, same cursor on every page
        seen, page, cursors = [], None, set()
        while True:
            r = S.contract_days(dict({'limit': 15}, **({'page': page} if page else {})))
            seen += [x['date'] for x in r['days']]
            cursors.add(r['cursor'])
            page = r['next_page']
            if not page:
                break
        self.assertEqual(len(seen), ACT_DAYS)
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(len(cursors), 1)
        cursor = cursors.pop()

        # changed_since: an unchanged overlap re-sync emits nothing; a re-scored night and a new exercise do
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [])
        S.sync(full=True)
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [], 'an unchanged re-sync must not re-emit days')
        FAKE.data['sleep'][5]['sleep_score'] = 41
        FAKE.data['exercise'].append(dict(FAKE.data['exercise'][0], id='EXNEW', start_time=day(1) + 'T07:00:00', detailed_sport_info='CYCLING'))
        S.sync()
        ch = S.contract_days({'changed_since': cursor})
        self.assertEqual([x['date'] for x in ch['days']], [day(5), day(1)])
        self.assertEqual(ch['days'][0]['sleep']['score'], 41)
        self.assertIn('Cycling', [x['sport'] for x in ch['days'][1]['workouts']])
        self.assertEqual(S.contract_days({'changed_since': ch['cursor']})['days'], [])

        # incremental sync asks from last sync minus the overlap, in one window
        FAKE.calls.clear()
        S.sync()
        acts = [c[2] for c in FAKE.calls if c[1] == '/v3/users/activities']
        self.assertEqual(len(acts), 1)
        self.assertEqual(dt.date.fromisoformat(acts[0]['from']), TODAY - dt.timedelta(days=S.OVERLAP_DAYS))
        self.assertFalse([c for c in FAKE.calls if c[:2] == ('POST', '/v3/users')], 'registration happens once')
        self.assertEqual(S.status_info()['counts']['days'], ACT_DAYS)

        # ---- HTTP + MCP
        code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual([t['name'] for t in json.loads(body)['result']['tools']], ['health_data_status', 'health_data_sync', 'health_data_days'])
        for name, args in [('health_data_status', {}), ('health_data_days', {'from': d0}), ('health_data_days', {'changed_since': ''})]:
            code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            res = json.loads(body)['result']
            self.assertFalse(res['isError'], (name, res))
            for secret in (SECRET, FAKE.access, CID):
                self.assertNotIn(secret, body.decode())
        self.assertEqual(http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, {'Tailscale-User-Login': 'a@b'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'evil.example:18998'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8444'})[0], 200)
        self.assertEqual(http(self.port, 'POST', '/api/sync', {}, {'Host': 'box.example.ts.net:8444', 'Origin': 'https://evil.example'})[0], 403)
        for path in ('/api/status', '/api/setup', '/api/days', '/api/days?changed_since=', '/wizard-polar/api/status', '/healthz', '/', '/icon.svg'):
            code, body = http(self.port, 'GET', path)
            self.assertEqual(code, 200, path)
            for secret in (SECRET, FAKE.access):
                self.assertNotIn(secret, body.decode('latin-1'), path)
        for hdr in ('X-Forwarded-For', 'Tailscale-User-Login', 'Forwarded', 'X-Real-IP', 'CF-Connecting-IP'):
            self.assertEqual(http(self.port, 'GET', '/api/days', headers={hdr: 'x'})[0], 403, hdr)

        # ---- the app's own callback route (https Redirect URL case)
        start = S.login_start()
        state = parse_qs(urlsplit(start['authorize_url']).query)['state'][0]
        code, body = http(self.port, 'GET', f'/wizard-polar/callback?code=good-code&state={state}')
        self.assertEqual(code, 200)
        self.assertIn(b'Polar connected', body)
        self.wait_sync()

        # ---- Polar has no refresh tokens: a 401 means the sign-in ended -> needs_login, token removed, data kept
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        FAKE.force_401 = 1
        with self.assertRaises(S.Problem):
            S.api_get('/v3/users/sleep')
        st = S.status_info()
        self.assertEqual(st['status'], 'needs_login')
        self.assertFalse((S.DATA / 'tokens.json').exists())
        self.assertEqual(st['counts']['days'], ACT_DAYS)
        ds = st['dataset']

        # ---- disconnect de-registers the user, wipes the copy; forget wipes the keys too
        self.connect()
        self.wait_sync()
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        access = FAKE.access
        code, body = http(self.port, 'POST', '/api/disconnect', {'forget_keys': True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['status'], 'not_configured')
        self.assertTrue(FAKE.deleted)
        blob = self.files_text()
        for secret in (SECRET, CID, access):
            self.assertNotIn(secret, blob)
        st = S.status_info()
        self.assertEqual(st['counts'], {'days': 0, 'sleeps': 0, 'workouts': 0})
        self.assertNotEqual(st['dataset'], ds)


class MappingTest(unittest.TestCase):
    """The documented formulas and the exact sample payloads from Polar's documentation."""
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = server

    def test_readiness_formula_matches_health_bands(self):
        S = self.S
        want = {1: 'low', 2: 'low', 3: 'medium', 4: 'medium', 5: 'high', 6: 'high'}
        for status, band in want.items():
            for charge in (-10, -7.5, -3, 0, 0.1, 4.4, 9.9, 10, None, -99, 99):
                sc = S.readiness_score(status, charge)
                self.assertTrue(0 <= sc <= 100)
                self.assertEqual(S._band(sc), band, (status, charge, sc))
        self.assertEqual(S.readiness_score(4, 0), 58)
        self.assertEqual(S.readiness_score(6, 10), 100)
        self.assertEqual(S.readiness_score(1, -10), 0)
        self.assertLess(S.readiness_score(5, -5), S.readiness_score(5, 5))
        self.assertIsNone(S.readiness_score(0, 3))
        self.assertIsNone(S.readiness_score(None, 3))

    def test_load_formula(self):
        S = self.S
        anchors = {0: 0.0, 50: 8.3, 100: 13.3, 150: 16.3, 200: 18.2, 300: 20.0}
        for trimp, eq in anchors.items():
            self.assertEqual(S.load_equivalent(trimp), eq)
            self.assertEqual(S.load_equivalent(trimp), round(21 * (1 - math.exp(-trimp / 100)), 1))
        self.assertLess(S.load_equivalent(10000), 21.0001)
        self.assertIsNone(S.load_equivalent(None))
        self.assertIsNone(S.load_equivalent(-1))

    def test_doc_samples(self):
        S = self.S
        sample_day = {'sleep': copy.deepcopy(DOC_SLEEP), 'recharge': copy.deepcopy(DOC_RECHARGE),
                      'activity': copy.deepcopy(DOC_ACTIVITY), 'exercise': [copy.deepcopy(DOC_EXERCISE)],
                      'cardio': dict(DOC_CARDIO, cardio_load_status='MAINTAINING', cardio_load=96.4)}
        d = S._n_day('2020-01-01', sample_day)
        check_day(self, d)
        self.assertEqual(d['readiness'], {'score': 42, 'band': 'medium', 'state': 'scored', 'calibrating': False})
        self.assertEqual((d['hrv_ms'], d['resting_hr_bpm'], d['respiratory_rate']), (28, 70, 14.1))
        sl = d['sleep']
        self.assertEqual(sl['start'], '2019-12-31T21:39:07.000Z')
        self.assertEqual((sl['asleep_h'], sl['in_bed_h'], sl['awake_h'], sl['score'], sl['need_h']), (1.11, round(31230 / 3600, 2), 0.28, 80, 8.0))
        self.assertEqual(d['load']['value'], S.load_equivalent(96.4))
        self.assertEqual((d['steps'], d['calories_kcal']), (8823, 2500))
        self.assertEqual(d['extras']['polar']['active_min'], 191)
        self.assertEqual(d['extras']['polar']['nightly_recharge_word'], 'compromised')
        self.assertEqual(d['extras']['polar']['sleep_charge_word'], 'usual')
        w = d['workouts'][0]
        self.assertEqual((w['minutes'], w['start'], w['end']), (164, '2008-10-13T07:40:02.000Z', '2008-10-13T10:24:02.000Z'))
        self.assertEqual((w['avg_hr_bpm'], w['max_hr_bpm'], w['calories_kcal'], w['distance_m']), (129, 147, 530, 1600))
        self.assertEqual(w['zones_min'], [None, 0.1, None, None, None, None])
        self.assertEqual(w['sport'], 'Watersports waterski')
        self.assertEqual(w['load']['native']['value'], 1)       # Training Load Pro cardio load wins over the older training_load
        self.assertEqual(S._secs('PT18H23M30S'), 66210)
        # a NOT_AVAILABLE Cardio Load row is not stored as data
        self.assertFalse(S._meaningful('cardio', DOC_CARDIO | {'cardio_load': 0}))
        self.assertTrue(S._meaningful('cardio', DOC_CARDIO))      # docs sample: N/A status but a load value


DAY_KEYS = {'date', 'updated_at', 'readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c', 'respiratory_rate',
            'sleep', 'load', 'calories_kcal', 'energy_kj', 'avg_hr_bpm', 'max_hr_bpm', 'steps', 'workouts', 'extras'}
SLEEP_KEYS = {'start', 'end', 'asleep_h', 'in_bed_h', 'awake_h', 'light_h', 'deep_h', 'rem_h', 'score', 'efficiency_pct', 'respiratory_rate', 'need_h', 'naps'}
WORKOUT_KEYS = {'id', 'sport', 'start', 'end', 'minutes', 'load', 'avg_hr_bpm', 'max_hr_bpm', 'calories_kcal', 'energy_kj', 'distance_m', 'zones_min', 'extras'}


def check_day(t, d):
    """The day record shape promised by docs/health-data-contract.md."""
    t.assertTrue(DAY_KEYS <= set(d), DAY_KEYS - set(d))
    t.assertRegex(d['date'], r'^\d{4}-\d{2}-\d{2}$')
    if d['readiness']:
        t.assertIn(d['readiness']['band'], ('high', 'medium', 'low'))
        t.assertIn(d['readiness']['state'], ('scored', 'pending', 'unscorable'))
        t.assertTrue(0 <= d['readiness']['score'] <= 100)
    if d['sleep']:
        t.assertTrue(SLEEP_KEYS <= set(d['sleep']), SLEEP_KEYS - set(d['sleep']))
    if d['load']:
        t.assertTrue({'value', 'max', 'label', 'scale_max'} <= set(d['load']))
        t.assertTrue(0 <= d['load']['value'] <= d['load']['scale_max'])
    for w in d['workouts']:
        t.assertTrue(WORKOUT_KEYS <= set(w), WORKOUT_KEYS - set(w))
    t.assertEqual(set(d['extras']), {'polar'})


class IconTest(unittest.TestCase):
    """Own icon: full-colour store square (icon.png from wizard.md) and a silhouette at web/icon.svg the shell tints.
    A generic chest strap with a heart; never Polar's own logo or wordmark."""
    root = ROOT

    def test_icons(self):
        self.assertIn('\nicon: icon.png\n', (self.root / 'wizard.md').read_text())
        png = (self.root / 'icon.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')), (1024, 1024))
        mask = (self.root / 'web' / 'icon.svg').read_text()
        self.assertIn('viewBox="0 0 512 512"', mask)
        self.assertNotRegex(mask, r'<text|<image|href=|<script')
        self.assertNotIn('polar', (mask + (self.root / 'icon.svg').read_text()).lower())


if __name__ == '__main__':
    unittest.main()
