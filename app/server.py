"""Polar: the Polar connector for a Wizard box (contract wizard.health-data/1).

Setup happens on this app's own screen. The owner creates a free API client on Polar AccessLink
(admin.polaraccesslink.com), pastes its Client ID and Client Secret here (stored server-side, never sent back), then
signs in to Polar Flow with OAuth 2.0. Polar sends the one-time code to the Redirect URL the owner registered: either
this app's own https address (the app finishes on its own) or a loopback address such as
http://localhost:3000/callback, in which case the owner pastes the address the browser landed on back into the app.
After the token exchange the app registers the owner's account with AccessLink once (POST /v3/users), as Polar
requires before any data can be read. Polar user tokens do not expire and there are no refresh tokens; a revoked
token answers 401 and the app asks the owner to connect again.

The app keeps a local SQLite copy of Nightly Recharge, sleep, daily activity, Cardio Load and exercises: a backfill
after sign-in (Polar shares about the last 28 days of sleep and Nightly Recharge and up to a year of daily activity and
Cardio Load), then an incremental sync every 20 minutes with a few days of overlap so late uploads are picked up.
Other apps on the box (the Health app) read the copy through the box-only health-data contract: GET /api/status and
GET /api/days (docs/health-data-contract.md). Every stored change bumps a sequence number, so readers can ask for only
the days changed since their cursor.

Stdlib only. Secrets live in DATA_DIR (0600 files, 0700 folder) and are never returned by any API or tool.
"""
import base64
import datetime as dt
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-polar'
NAME = 'Polar'
VERSION = '0.1.1'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8832')
AUTH_URL = os.environ.get('POLAR_AUTH_URL', 'https://flow.polar.com/oauth2/authorization')
TOKEN_URL = os.environ.get('POLAR_TOKEN_URL', 'https://polarremote.com/v2/oauth2/token')
API_BASE = os.environ.get('POLAR_API_BASE', 'https://www.polaraccesslink.com').rstrip('/')
DASHBOARD_URL = 'https://admin.polaraccesslink.com'
CONSENT_URL = 'https://account.polar.com'
SIGNUP_URL = os.environ.get('POLAR_SIGNUP_URL', 'https://flow.polar.com')
DEFAULT_REDIRECT = 'http://localhost:3000/callback'
SUGGESTED_APP_NAME = 'Wizard Health'   # the AccessLink client's name; any name works
SUGGESTED_DESCRIPTION = 'Personal use: my own Wizard box reads my own Polar data, read-only.'
SCOPE = 'accesslink.read_all'          # AccessLink's only scope; the owner enters no scopes on Polar
DATA_TYPES = ['Exercise data', 'Daily activity data', 'Physical information data']
SYNC_MINUTES = float(os.environ.get('POLAR_SYNC_MINUTES', '20'))
STATE_TTL = float(os.environ.get('POLAR_STATE_TTL', str(2 * 3600)))   # a sign-in started here stays valid two hours
OVERLAP_DAYS = int(os.environ.get('POLAR_OVERLAP_DAYS', '4'))
HISTORY_DAYS = int(os.environ.get('POLAR_HISTORY_DAYS', '364'))      # Polar refuses dates older than 365 days
WINDOW_DAYS = int(os.environ.get('POLAR_WINDOW_DAYS', '28'))         # one date-range request covers at most 28 days
REQUEST_GAP = float(os.environ.get('POLAR_REQUEST_GAP', '0.5'))
RETRIES = int(os.environ.get('POLAR_RETRIES', '4'))
BACKOFF = float(os.environ.get('POLAR_BACKOFF', '5'))
UA = f'{APP_ID}/{VERSION} (+Wizard OS)'
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
LOCK = threading.RLock()          # local files + database
SYNC_LOCK = threading.Lock()
WAKE = threading.Event()


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ProviderError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _shred(name):
    p = DATA / name
    try:
        size = p.stat().st_size
        with open(p, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
        p.unlink()
    except FileNotFoundError:
        pass


def _keys():
    return _read('client.json')


def _tokens():
    return _read('tokens.json')


def _save_tokens(t):
    with LOCK:
        if t:
            _write_secret('tokens.json', json.dumps(t))
        else:
            _shred('tokens.json')


def _state():
    return _read('state.json')


def _update_state(**kv):
    with LOCK:
        s = _state()
        for k, v in kv.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        _write_secret('state.json', json.dumps(s))
        return s


def secure_files():
    """Runtipi loosens app-data permissions; tighten them again on every start."""
    db().close()
    for p in DATA.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def db():
    path = _private() / 'polar.sqlite3'
    fresh = not path.exists()
    c = sqlite3.connect(path, timeout=15)
    if fresh:
        os.chmod(path, 0o600)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, day TEXT, start TEXT, data TEXT NOT NULL,
        changed_seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, id));
      CREATE INDEX IF NOT EXISTS records_day ON records(kind, day);
      CREATE INDEX IF NOT EXISTS records_changed ON records(changed_seq);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
    ''')
    return c


def _next_seq(c):
    n = int(json.loads((c.execute("SELECT data FROM meta WHERE key='seq'").fetchone() or ['0'])[0])) + 1
    c.execute("INSERT OR REPLACE INTO meta (key,data) VALUES ('seq',?)", (str(n),))
    return n


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _parse(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _ago(iso):
    t = _parse(iso)
    return (_now() - t).total_seconds() if t else 1e12


_DUR = re.compile(r'^P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$')


def _secs(iso_dur):
    """ISO 8601 duration (PT2H44M45S) -> seconds, or None."""
    m = _DUR.match(str(iso_dur or ''))
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (float(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


# ---------------- Polar HTTP ----------------

def _http(method, url, form=None, body=None, headers=None, timeout=45):
    data, h = None, {'User-Agent': UA, 'Accept': 'application/json'}
    if form is not None:
        data = urlencode(form).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError(0, 'unreachable', 'Polar did not answer. Check the box is online; the app will retry.') from None


def _json(b):
    try:
        return json.loads(b or b'{}')
    except ValueError:
        return {}


def _basic(k):
    return 'Basic ' + base64.b64encode(f"{k['client_id']}:{k['client_secret']}".encode()).decode()


def _token_call(form):
    """Polar's token endpoint: client id and secret go in a Basic auth header, the grant in the form body."""
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Polar Client ID and Client Secret first (step 4).')
    status, body, _ = _http('POST', TOKEN_URL, form=form, headers={'Authorization': _basic(k), 'Accept': 'application/json;charset=UTF-8'})
    return status, _json(body)


def test_keys():
    """Exchange a made-up code: Polar answers invalid_grant when the client is real, invalid/unauthorized_client when not."""
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Polar Client ID and Client Secret first.')
    status, d = _token_call({'grant_type': 'authorization_code', 'code': 'wizard-key-check-' + secrets.token_hex(8),
                             'redirect_uri': k.get('redirect_uri') or DEFAULT_REDIRECT})
    err = str(d.get('error') or '')
    if err == 'invalid_grant':
        result, msg = 'valid', 'Polar recognised your Client ID and Client Secret. You can connect now.'
    elif err in ('invalid_client', 'unauthorized_client') or status == 401:
        result, msg = 'invalid', 'Polar did not accept this Client ID and Client Secret. Copy both again from your AccessLink client and save.'
    elif status == 429:
        result, msg = 'unknown', 'Polar is busy right now. Try the test again in a minute.'
    else:
        result, msg = 'unknown', 'Polar gave an unexpected answer, so the keys could not be confirmed. Try again shortly.'
    with LOCK:
        k = _keys()
        if k:
            k.update(verified=result, checked_at=_iso(_now()))
            _write_secret('client.json', json.dumps(k))
    return {'result': result, 'message': msg}


def save_keys(body):
    cid = str(body.get('client_id') or '').strip()
    secret = str(body.get('client_secret') or '').strip()
    redirect = str(body.get('redirect_uri') or '').strip() or DEFAULT_REDIRECT
    if not re.fullmatch(r'[A-Za-z0-9._~-]{8,200}', cid):
        raise Problem(400, 'That Client ID does not look right. Copy it again from your AccessLink client.')
    if not re.fullmatch(r'[\x21-\x7e]{8,300}', secret):
        raise Problem(400, 'That Client Secret does not look right. Copy it again from your AccessLink client.')
    u = urlsplit(redirect)
    if u.scheme not in ('http', 'https') or not u.netloc or len(redirect) > 300:
        raise Problem(400, 'The Redirect URL must be the exact http(s) address you entered in your AccessLink client.')
    if u.scheme == 'http' and u.hostname not in ('localhost', '127.0.0.1'):
        raise Problem(400, 'Use plain http only for localhost. Use https or http://localhost:3000/callback.')
    with LOCK:
        old = _keys()
        changed = old.get('client_id') != cid or old.get('client_secret') != secret
        _write_secret('client.json', json.dumps({'client_id': cid, 'client_secret': secret, 'redirect_uri': redirect,
                                                 'saved_at': _iso(_now()), 'verified': None}))
        if changed and _tokens():
            _save_tokens(None)   # the token belongs to the old client; sign in again with the new one
            _update_state(auth='needs_login', pending=None, registered=None)
    return setup_info()


def forget_keys():
    with LOCK:
        _shred('client.json')
        _save_tokens(None)
        _update_state(pending=None, auth=None, registered=None)
    return setup_info()


def _mask(v):
    v = str(v or '')
    return ('•' * 6 + v[-4:]) if len(v) > 8 else '•' * 6


def setup_info():
    k = _keys()
    return {'dashboard_url': DASHBOARD_URL, 'signup_url': SIGNUP_URL, 'consent_url': CONSENT_URL,
            'suggested_name': SUGGESTED_APP_NAME, 'suggested_description': SUGGESTED_DESCRIPTION,
            'default_redirect': DEFAULT_REDIRECT, 'scopes': [SCOPE], 'data_types': DATA_TYPES,
            'keys_saved': bool(k.get('client_id') and k.get('client_secret')),
            'client_id_hint': _mask(k.get('client_id')) if k.get('client_id') else None, 'secret_saved': bool(k.get('client_secret')),
            'redirect_uri': k.get('redirect_uri'), 'verified': k.get('verified'), 'checked_at': k.get('checked_at'),
            'connected': bool(_tokens().get('access_token'))}


# ---------------- OAuth sign-in + AccessLink registration ----------------

def login_start():
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Polar Client ID and Client Secret first (step 4).')
    state = secrets.token_hex(16)
    with LOCK:
        pend = [p for p in (_state().get('pending') or []) if _ago(p.get('at')) < STATE_TTL][-4:]
        pend.append({'state': state, 'at': _iso(_now())})
        _update_state(pending=pend)
    q = {'response_type': 'code', 'client_id': k['client_id'], 'redirect_uri': k.get('redirect_uri') or DEFAULT_REDIRECT,
         'scope': SCOPE, 'state': state}
    return {'authorize_url': AUTH_URL + '?' + urlencode(q), 'redirect_uri': q['redirect_uri'],
            'paste_back': urlsplit(q['redirect_uri']).hostname in ('localhost', '127.0.0.1')}


def login_finish(pasted):
    raw = str(pasted or '').strip()
    if not raw or len(raw) > 4096:
        raise Problem(400, 'Paste the whole address from the browser bar after you tapped Accept.')
    u = urlsplit(raw if '://' in raw else 'http://x/?' + raw.lstrip('?'))
    q = {a: b[0] for a, b in parse_qs(u.query).items()}
    if q.get('error'):
        _update_state(pending=None)
        msg = 'You chose not to allow access.' if q['error'] == 'access_denied' else 'Polar did not finish the sign-in.'
        raise Problem(400, msg + ' Tap Connect with Polar to try again.')
    code, state = q.get('code'), q.get('state')
    if not code or not state:
        raise Problem(400, 'That address has no sign-in code. Paste the whole address, starting with http.')
    with LOCK:
        pend = [p for p in (_state().get('pending') or []) if _ago(p.get('at')) < STATE_TTL]
        if not any(secrets.compare_digest(p['state'], state) for p in pend):
            raise Problem(400, 'This sign-in link expired or was already used. Tap Connect with Polar to start again.')
        _update_state(pending=[p for p in pend if p['state'] != state])
    k = _keys()
    status, d = _token_call({'grant_type': 'authorization_code', 'code': code, 'redirect_uri': k.get('redirect_uri') or DEFAULT_REDIRECT})
    if status != 200 or not d.get('access_token'):
        err = d.get('error')
        if err in ('invalid_client', 'unauthorized_client'):
            raise Problem(400, 'Polar did not accept the saved Client ID and Client Secret. Replace the keys and try again.')
        if err == 'invalid_grant':
            raise Problem(400, 'Polar refused this code. It may have been used already, be over 10 minutes old, or the Redirect URL does not match. Tap Connect with Polar again.')
        raise Problem(502, 'Polar did not finish the sign-in. Try again in a minute.')
    _save_tokens({'access_token': d['access_token'], 'user_id': str(d.get('x_user_id') or ''),
                  'expires_in': d.get('expires_in'), 'issued_at': _iso(_now())})
    _update_state(auth='connected', last_error=None, connected_at=_iso(_now()), registered=None)
    register_user()
    start_sync_thread(full=not _state().get('backfilled'))
    return status_info()


def register_user():
    """AccessLink needs the user registered to this client once (POST /v3/users). 409 = already registered."""
    if _state().get('registered'):
        return
    t = _tokens()
    if not t.get('access_token'):
        raise Problem(409, 'Not connected to Polar. Open the Polar app and tap Connect with Polar.')
    status, body, _ = _http('POST', API_BASE + '/v3/users', body={'member-id': 'wizard-' + secrets.token_hex(8)},
                            headers={'Authorization': 'Bearer ' + t['access_token']})
    if status == 200:
        _store_profile(_json(body))
    elif status == 409:
        pass                      # already registered to this client (e.g. a reconnect)
    elif status == 403:
        _needs_login('Polar needs you to accept its data-sharing consents first. Accept them at account.polar.com, then tap Connect with Polar again.')
    elif status == 401:
        _needs_login('Polar did not accept the sign-in. Tap Connect with Polar again.')
    else:
        raise ProviderError(status, 'register_failed', f'Polar could not register your account with this app ({status}). The app will retry.')
    _update_state(registered=True)


def _store_profile(d):
    if isinstance(d, dict) and d:
        with LOCK:
            c = db()
            with c:
                c.execute("INSERT OR REPLACE INTO meta (key,data) VALUES ('profile',?)", (json.dumps(d),))
            c.close()


def _needs_login(msg):
    _save_tokens(None)
    _update_state(auth='needs_login', last_error=msg, registered=None)
    raise Problem(401, msg)


def api_get(path, params=None):
    t = _tokens()
    if not t.get('access_token'):
        raise Problem(409, 'Not connected to Polar. Open the Polar app and tap Connect with Polar.')
    url = API_BASE + path + ('?' + urlencode(params) if params else '')
    status = 0
    for attempt in range(RETRIES + 1):
        status, body, headers = _http('GET', url, headers={'Authorization': 'Bearer ' + t['access_token']})
        if status == 200:
            return _json(body)
        if status in (204, 404):
            return None
        if status == 401:
            _needs_login('Polar ended this sign-in. Connect again in the Polar app.')
        if status == 403:
            _needs_login('Polar needs you to accept its data-sharing consents. Accept them at account.polar.com, then connect again.')
        if status == 400:
            raise ProviderError(400, 'bad_request', f'Polar refused the request for {path}.')
        if status == 429 or status >= 500:
            hdr = {k.lower(): v for k, v in headers.items()}
            try:   # RateLimit-Reset: "<short-term seconds>, <long-term seconds>"
                wait = float(str(hdr.get('ratelimit-reset') or hdr.get('retry-after') or '0').split(',')[0])
            except ValueError:
                wait = 0
            if (status == 429 and wait > 120) or attempt >= RETRIES:
                break
            time.sleep(min(120.0, max(wait, BACKOFF * (2 ** attempt))))
            continue
        raise ProviderError(status, 'api_error', f'Polar answered {status} for {path}.')
    if status == 429:
        _update_state(rate_limited_until=time.time() + 900)
        raise ProviderError(429, 'rate_limited', 'Polar asked the app to slow down. Syncing resumes in about 15 minutes.')
    raise ProviderError(status, 'api_error', f'Polar answered {status} for {path}. The app will retry.')


# ---------------- sync ----------------

def _rec_key(kind, r):
    """(id, day, start) for a Polar record. Daily records are keyed by their date."""
    if kind == 'exercise':
        st = r.get('start_time') or r.get('start-time') or ''
        return str(r.get('id') or ''), st[:10] or None, _exercise_start_utc(r)
    if kind == 'activity':
        day = (r.get('start_time') or (r.get('samples') or {}).get('date') or '')[:10] or None
        return day, day, r.get('start_time')
    day = str(r.get('date') or '')[:10] or None
    return day, day, r.get('sleep_start_time')


def _meaningful(kind, r):
    if kind == 'cardio':   # Polar lists every day of a range; days it could not calculate carry no data
        return r.get('cardio_load_status') not in (None, 'LOAD_STATUS_NOT_AVAILABLE') or (r.get('cardio_load') or 0) > 0
    return True


def _save(kind, rows):
    """Upsert Polar records. A row's changed_seq moves only when its data really changed, so an overlapping re-sync
    does not make every reader re-read the last few days."""
    with LOCK:
        c = db()
        with c:
            seq = _next_seq(c)
            for r in rows or []:
                if not isinstance(r, dict) or not _meaningful(kind, r):
                    continue
                rid, day, start = _rec_key(kind, r)
                if not rid or not day or not DATE_RE.match(day):
                    continue
                if kind == 'activity':
                    r = {k: v for k, v in r.items() if k != 'samples'}   # only the summary is kept
                c.execute('''INSERT INTO records (kind,id,day,start,data,changed_seq) VALUES (?,?,?,?,?,?)
                             ON CONFLICT(kind,id) DO UPDATE SET day=excluded.day, start=excluded.start,
                               changed_seq=CASE WHEN records.data IS excluded.data THEN records.changed_seq ELSE excluded.changed_seq END,
                               data=excluded.data''',
                          (kind, rid, day, start, json.dumps(r, sort_keys=True), seq))
        c.close()


def _list(d, key):
    if isinstance(d, list):
        return d
    if isinstance(d, dict) and key:
        return d.get(key) or []
    return []


def _windows(start, end):
    """Inclusive date windows of at most WINDOW_DAYS, oldest first."""
    out, a = [], start
    while a <= end:
        b = min(end, a + dt.timedelta(days=WINDOW_DAYS - 1))
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out


def _pull_range(kind, path, since):
    """Date-range endpoints (daily activity, Cardio Load), one window at a time."""
    end = (_now() + dt.timedelta(hours=14)).date()   # the owner's 'today' can be ahead of UTC
    n = 0
    for a, b in _windows(since, end):
        try:
            d = api_get(path, {'from': a.isoformat(), 'to': b.isoformat()})
        except ProviderError as e:
            if e.status != 400 or b <= _now().date():
                raise
            d = api_get(path, {'from': a.isoformat(), 'to': _now().date().isoformat()})   # 'today' not open yet on Polar
        rows = _list(d, '')
        _save(kind, rows)
        n += len(rows)
        _update_state(progress={'kind': kind, 'records': n})
        time.sleep(REQUEST_GAP)
    return n


def sync(full=False):
    if not SYNC_LOCK.acquire(blocking=False):
        return dict(status_info(), note='A sync is already running.')
    try:
        st = _state()
        if (st.get('rate_limited_until') or 0) > time.time():
            raise ProviderError(429, 'rate_limited', 'Polar asked the app to slow down. Syncing resumes shortly.')
        full = full or not st.get('backfilled')
        _update_state(syncing=_iso(_now()), sync_kind='backfill' if full else 'incremental')
        register_user()
        today = _now().date()
        if full:
            since = today - dt.timedelta(days=HISTORY_DAYS)
        else:
            since = ((_parse(st.get('last_sync')) or _now()) - dt.timedelta(days=OVERLAP_DAYS)).date()
        counts = {}
        # windowed lists: Polar returns the last 28 days of nights and recharges, the last 30 days of exercises
        for kind, path, key, params in (('sleep', '/v3/users/sleep', 'nights', None),
                                        ('recharge', '/v3/users/nightly-recharge', 'recharges', None),
                                        ('exercise', '/v3/exercises', '', {'zones': 'true'})):
            rows = _list(api_get(path, params), key)
            _save(kind, rows)
            counts[kind] = len(rows)
            _update_state(progress={'kind': kind, 'records': len(rows)})
            time.sleep(REQUEST_GAP)
        counts['activity'] = _pull_range('activity', '/v3/users/activities', since)
        counts['cardio'] = _pull_range('cardio', '/v3/users/cardio-load/date', since)
        uid = _tokens().get('user_id')
        if uid:
            _store_profile(api_get('/v3/users/' + uid))
        _update_state(last_sync=_iso(_now()), last_error=None, backfilled=True, auth='connected', syncing=None, progress=None,
                      sync_kind=None, last_counts=counts, rate_limited_until=None)
        return dict(status_info(), pulled=counts)
    except (Problem, ProviderError) as e:
        _update_state(last_error=e.message, syncing=None, progress=None, sync_kind=None)
        raise
    finally:
        SYNC_LOCK.release()


def _safe_sync(full=False):
    try:
        sync(full)
    except (Problem, ProviderError):
        pass
    except Exception:   # never let a bad record stop the scheduler
        _update_state(last_error='Sync failed unexpectedly; it will retry.', syncing=None, progress=None)


def start_sync_thread(full=False):
    threading.Thread(target=_safe_sync, args=(full,), daemon=True).start()


def _scheduler():
    time.sleep(5)
    while True:
        st = _state()
        if _tokens().get('access_token'):
            due = not st.get('backfilled') or _ago(st.get('last_sync')) >= SYNC_MINUTES * 60
            if due and (st.get('rate_limited_until') or 0) <= time.time():
                _safe_sync(full=not st.get('backfilled'))
        WAKE.wait(60)
        WAKE.clear()


# ---------------- the health-data contract (wizard.health-data/1, see docs/health-data-contract.md) ----------------

CONTRACT = 'wizard.health-data/1'
SOURCE = {'id': APP_ID, 'provider': 'polar', 'name': NAME, 'app_path': PREFIX + '/'}
LOAD_K = 100.0   # TRIMP at which the 0-21 equivalent reaches 63% of its range (docs/health-data-contract.md)
LOAD = {'label': 'Cardio load', 'max': 21, 'scale_max': 21}
SCALES = {'readiness': {'label': 'Nightly Recharge', 'min': 0, 'max': 100, 'formula': 'polar-nightly-recharge/1'},
          'sleep_score': {'label': 'Sleep score', 'min': 0, 'max': 100},
          'load': {'label': 'Cardio load', 'min': 0, 'max': 21, 'scale_max': 21, 'formula': 'polar-trimp/1',
                   'native': {'label': 'Cardio load (TRIMP)', 'unit': 'TRIMP', 'max': None}}}
# Nightly Recharge status 1-6 -> a 0-100 slice; ANS charge (-10..+10) places the night inside its slice.
NR_SLICES = {1: (0, 16), 2: (17, 33), 3: (34, 50), 4: (51, 66), 5: (67, 83), 6: (84, 100)}
NR_WORDS = {1: 'very poor', 2: 'poor', 3: 'compromised', 4: 'OK', 5: 'good', 6: 'very good'}
CHARGE_WORDS = {1: 'much below usual', 2: 'below usual', 3: 'usual', 4: 'above usual', 5: 'much above usual'}


def _r(v, n=1):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return round(v) if n == 0 else round(v, n)


def _hours(s):
    return round(s / 3600, 2) if isinstance(s, (int, float)) and not isinstance(s, bool) else None


def _band(score):
    if not isinstance(score, (int, float)):
        return None
    return 'high' if score >= 67 else 'medium' if score >= 34 else 'low'


def readiness_score(status, ans_charge):
    """Nightly Recharge -> 0-100 (formula polar-nightly-recharge/1). Each of Polar's six statuses owns a slice that
    lines up with the contract bands (very poor/poor = low, compromised/OK = medium, good/very good = high); ANS charge
    (-10 to +10, 0 = usual) moves the score inside the slice; without it the score is the slice's middle."""
    if status not in NR_SLICES:
        return None
    lo, hi = NR_SLICES[status]
    if isinstance(ans_charge, (int, float)) and not isinstance(ans_charge, bool):
        pos = (max(-10.0, min(10.0, float(ans_charge))) + 10) / 20
    else:
        pos = 0.5
    return int(round(lo + (hi - lo) * pos))


def load_equivalent(trimp):
    """Cardio load (TRIMP) -> Health's 0-21 activity scale (formula polar-trimp/1): 21 x (1 - e^(-TRIMP/100)).
    Saturating like a strain score: 50 -> 8.3, 100 -> 13.3, 150 -> 16.3, 200 -> 18.2, 300 -> 20.0."""
    if not isinstance(trimp, (int, float)) or isinstance(trimp, bool) or trimp < 0:
        return None
    return round(21 * (1 - math.exp(-float(trimp) / LOAD_K)), 1)


def _load(trimp):
    eq = load_equivalent(trimp)
    if eq is None:
        return None
    return dict(LOAD, value=eq, native={'value': _r(trimp), 'label': 'Cardio load (TRIMP)', 'unit': 'TRIMP'})


def _dataset():
    st = _state()
    if not st.get('dataset'):
        st = _update_state(dataset=secrets.token_hex(12))
    return st['dataset']


def _exercise_start_utc(r):
    st = r.get('start_time') or r.get('start-time')
    off = r.get('start_time_utc_offset', r.get('start-time-utc-offset'))
    try:
        t = dt.datetime.fromisoformat(str(st)) if st else None
    except ValueError:
        t = None
    if not t:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone(dt.timedelta(minutes=int(off or 0))))
    return _iso(t)


def _g(r, *names):
    for n in names:
        if r.get(n) is not None:
            return r[n]
    return None


def _n_workout(r):
    hr = _g(r, 'heart_rate', 'heart-rate') or {}
    secs = _secs(r.get('duration'))
    start = _exercise_start_utc(r)
    end = _iso(_parse(start) + dt.timedelta(seconds=secs)) if start and secs is not None else None
    tlp = _g(r, 'training_load_pro', 'training-load-pro') or {}
    trimp = _g(tlp, 'cardio-load', 'cardio_load')
    if not isinstance(trimp, (int, float)) or trimp <= 0:
        trimp = _g(r, 'training_load', 'training-load')
    zones = {}
    for z in _g(r, 'heart_rate_zones', 'heart-rate-zones') or []:
        s = _secs(_g(z, 'in-zone', 'in_zone'))
        if isinstance(z.get('index'), int) and s is not None:
            zones[z['index']] = round(s / 60, 1)
    zl = [None] + [zones.get(i) for i in range(1, 6)]   # Polar zones 1-5; it has no zone 0
    sport = _g(r, 'detailed_sport_info', 'detailed-sport-info') or r.get('sport') or 'Activity'
    return {'id': str(r.get('id')), 'sport': str(sport).replace('_', ' ').capitalize(), 'start': start, 'end': end,
            'minutes': round(secs / 60) if secs is not None else None, 'load': _load(trimp),
            'avg_hr_bpm': hr.get('average'), 'max_hr_bpm': hr.get('maximum'), 'calories_kcal': _r(r.get('calories'), 0),
            'energy_kj': None, 'distance_m': _r(r.get('distance'), 0), 'zones_min': zl if zones else None,
            'extras': {'polar': {'sport': r.get('sport'), 'device': r.get('device'), 'training_load': _r(_g(r, 'training_load', 'training-load')),
                                 'cardio_load_interpretation': _g(tlp, 'cardio-load-interpretation'),
                                 'muscle_load': _g(tlp, 'muscle-load'), 'running_index': r.get('running-index')}}}


def _n_sleep(s, nr):
    light, deep, rem, unk = (s.get(k) for k in ('light_sleep', 'deep_sleep', 'rem_sleep', 'unrecognized_sleep_stage'))
    parts = [x for x in (light, deep, rem, unk) if isinstance(x, (int, float))]
    a, b = _parse(s.get('sleep_start_time')), _parse(s.get('sleep_end_time'))
    in_bed = (b - a).total_seconds() if a and b else None
    asleep = sum(parts) if parts else None
    goal = s.get('sleep_goal')
    return {'start': _iso(a) if a else None, 'end': _iso(b) if b else None, 'asleep_h': _hours(asleep), 'in_bed_h': _hours(in_bed),
            'awake_h': _hours(s.get('total_interruption_duration')), 'light_h': _hours(light), 'deep_h': _hours(deep), 'rem_h': _hours(rem),
            'score': _r(s.get('sleep_score'), 0),
            'efficiency_pct': _r(100 * asleep / in_bed, 0) if asleep and in_bed else None,
            'respiratory_rate': _r((nr or {}).get('breathing_rate_avg')),
            'need_h': _hours(goal) if isinstance(goal, (int, float)) and goal > 0 else None, 'naps': []}


def _n_day(day, rows):
    nr, sl, car = rows.get('recharge'), rows.get('sleep'), rows.get('cardio')
    act = rows.get('activity') or {}
    works = rows.get('exercise') or []
    readiness = None
    if nr and nr.get('nightly_recharge_status') in NR_SLICES:
        sc = readiness_score(nr.get('nightly_recharge_status'), nr.get('ans_charge'))
        readiness = {'score': sc, 'band': _band(sc), 'state': 'scored', 'calibrating': False}
    trimp = (car or {}).get('cardio_load')
    if not isinstance(trimp, (int, float)) and works:   # no Cardio Load row: add up the day's sessions
        ws = [w['load']['native']['value'] for w in (_n_workout(x) for x in works) if w.get('load')]
        trimp = sum(ws) if ws else None
    stamps = [x for x in [(sl or {}).get('sleep_end_time'), act.get('end_time')] if x]
    nr, sl, car = nr or {}, sl or {}, car or {}
    extras = {'readiness_formula': 'polar-nightly-recharge/1' if readiness else None,
              'nightly_recharge_status': nr.get('nightly_recharge_status'),
              'nightly_recharge_word': NR_WORDS.get(nr.get('nightly_recharge_status')),
              'ans_charge': _r(nr.get('ans_charge')), 'ans_charge_status': nr.get('ans_charge_status'),
              'beat_to_beat_ms': nr.get('beat_to_beat_avg'),
              'sleep_charge': sl.get('sleep_charge'), 'sleep_charge_word': CHARGE_WORDS.get(sl.get('sleep_charge')),
              'sleep_continuity': sl.get('continuity'), 'sleep_cycles': sl.get('sleep_cycles'),
              'unrecognized_sleep_h': _hours(sl.get('unrecognized_sleep_stage')),
              'cardio_load_trimp': _r(trimp), 'cardio_load_status': car.get('cardio_load_status'),
              'cardio_strain_7d': _r(car.get('strain')), 'cardio_tolerance_28d': _r(car.get('tolerance')),
              'cardio_load_ratio': _r(car.get('cardio_load_ratio'), 2), 'cardio_load_level': car.get('cardio_load_level'),
              'active_min': _r(_secs(act.get('active_duration')) / 60, 0) if _secs(act.get('active_duration')) is not None else None,
              'daily_activity_pct': _r(act.get('daily_activity')), 'active_calories_kcal': _r(act.get('active_calories'), 0),
              'distance_from_steps_m': _r(act.get('distance_from_steps'), 0)}
    return {
        'date': day, 'updated_at': max(stamps) if stamps else None, 'readiness': readiness,
        'hrv_ms': _r(nr.get('heart_rate_variability_avg')), 'resting_hr_bpm': _r(nr.get('heart_rate_avg'), 0),
        'spo2_pct': None, 'skin_temp_c': None, 'skin_temp_delta_c': None,
        'respiratory_rate': _r(nr.get('breathing_rate_avg')), 'sleep': _n_sleep(sl, nr) if sl else None,
        'load': _load(trimp), 'calories_kcal': _r(act.get('calories'), 0), 'energy_kj': None,
        'avg_hr_bpm': None, 'max_hr_bpm': None, 'steps': act.get('steps'),
        'workouts': [_n_workout(w) for w in sorted(works, key=lambda x: _exercise_start_utc(x) or '')],
        'extras': {'polar': extras},
    }


def _build_days(dates):
    if not dates:
        return []
    with LOCK:
        c = db()
        try:
            marks = ','.join('?' * len(dates))
            by = {}
            for x in c.execute(f'SELECT kind, day, data FROM records WHERE day IN ({marks}) ORDER BY start', list(dates)):
                d = by.setdefault(x['day'], {})
                if x['kind'] == 'exercise':
                    d.setdefault('exercise', []).append(json.loads(x['data']))
                else:
                    d[x['kind']] = json.loads(x['data'])
        finally:
            c.close()
    return [_n_day(day, by.get(day, {})) for day in dates]


def _token(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(',', ':')).encode()).decode().rstrip('=')


def _untoken(s):
    try:
        d = json.loads(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)))
        if isinstance(d, dict):
            return d
    except (ValueError, TypeError):
        pass
    raise Problem(400, 'page is not a page token from this source.')


def _cursor_seq(s):
    if s in (None, ''):
        return 0
    m = re.fullmatch(r'p1\.(\d{1,15})', str(s))
    if not m:
        raise Problem(400, 'changed_since is not a cursor from this source.')
    return int(m.group(1))


def _seq_now(c):
    row = c.execute("SELECT data FROM meta WHERE key='seq'").fetchone()
    return int(json.loads(row['data'])) if row else 0


def _check_day(v, name):
    if v is None or v == '':
        return None
    if not DATE_RE.match(str(v)):
        raise Problem(400, f'{name} must be YYYY-MM-DD.')
    return str(v)


def contract_days(args):
    """GET /api/days: normalized day records, oldest first, paged; optionally only days changed since a cursor."""
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    try:
        limit = max(1, min(500, int(args.get('limit') or 100)))
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    page = _untoken(str(args['page'])) if args.get('page') else {}
    changed = 'changed_since' in args
    since = _cursor_seq(args.get('changed_since')) if changed else 0
    after = page.get('after') or ''
    with LOCK:
        c = db()
        try:
            hw = int(page.get('hw') or _seq_now(c))
            q, p = ['SELECT DISTINCT day FROM records WHERE day IS NOT NULL AND day>?'], [after]
            if frm:
                q.append('AND day>=?'); p.append(frm)
            if to:
                q.append('AND day<=?'); p.append(to)
            if changed:
                q.append('AND day IN (SELECT day FROM records WHERE changed_seq>? AND changed_seq<=?)')
                p += [since, hw]
            q.append('ORDER BY day LIMIT ?'); p.append(limit + 1)
            dates = [r['day'] for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    more = len(dates) > limit
    dates = dates[:limit]
    return {'contract': CONTRACT, 'source': APP_ID, 'dataset': _dataset(), 'days': _build_days(dates),
            'next_page': _token({'after': dates[-1], 'hw': hw}) if more else None, 'cursor': f'p1.{hw}'}


def _counts():
    with LOCK:
        c = db()
        out = {r['kind']: r['n'] for r in c.execute('SELECT kind, COUNT(*) n FROM records GROUP BY kind')}
        span = c.execute('SELECT MIN(day) a, MAX(day) b, COUNT(DISTINCT day) n FROM records').fetchone()
        c.close()
    return out, span['a'], span['b'], span['n']


def _meta(key):
    with LOCK:
        c = db()
        row = c.execute('SELECT data FROM meta WHERE key=?', (key,)).fetchone()
        c.close()
    return json.loads(row['data']) if row else None


def status_info():
    k, t, st = _keys(), _tokens(), _state()
    keys = bool(k.get('client_id') and k.get('client_secret'))
    if not t.get('access_token'):
        state = 'needs_login' if keys and st.get('auth') == 'needs_login' else 'not_configured'
    elif st.get('syncing') and _ago(st.get('syncing')) < 7200:
        state = 'syncing'
    elif (st.get('rate_limited_until') or 0) > time.time():
        state = 'rate_limited'
    else:
        state = 'connected'
    counts, first, last, ndays = _counts()
    prof = _meta('profile') or {}
    nxt = None
    if state in ('connected', 'rate_limited') and st.get('last_sync'):
        nxt = _iso(_parse(st['last_sync']) + dt.timedelta(minutes=SYNC_MINUTES))
    live = state in ('connected', 'syncing', 'rate_limited')
    return {
        'contract': CONTRACT, 'source': SOURCE, 'status': state, 'version': VERSION,
        'account': {'name': prof.get('first-name')} if live or counts else None, 'dataset': _dataset(),
        'last_sync': st.get('last_sync'), 'next_sync': nxt, 'sync_every_minutes': SYNC_MINUTES,
        'history_from': first, 'latest_date': last,
        'counts': {'days': ndays, 'sleeps': counts.get('sleep', 0), 'workouts': counts.get('exercise', 0)},
        'error': st.get('last_error'), 'scales': SCALES,
        # Polar setup state, for this app's own screen
        'setup': {'keys_saved': keys, 'keys_verified': k.get('verified'), 'redirect_uri': k.get('redirect_uri') if keys else None,
                  'registered': bool(st.get('registered')), 'backfilled': bool(st.get('backfilled')),
                  'sync_kind': st.get('sync_kind') if state == 'syncing' else None,
                  'progress': st.get('progress') if state == 'syncing' else None},
    }


def disconnect(forget=False):
    t = _tokens()
    if t.get('access_token') and t.get('user_id'):
        try:   # best effort: de-register this user from the AccessLink client, which ends the app's access
            _http('DELETE', API_BASE + '/v3/users/' + t['user_id'], headers={'Authorization': 'Bearer ' + t['access_token']}, timeout=15)
        except ProviderError:
            pass
    with LOCK:
        _save_tokens(None)
        for p in list(DATA.glob('polar.sqlite3*')):
            p.unlink(missing_ok=True)
        _shred('state.json')     # also drops the dataset id, so readers know the copy is gone
        if forget:
            _shred('client.json')
    return status_info()


# ---------------- MCP ----------------

_NONE = {'type': 'object', 'properties': {}, 'additionalProperties': False}
TOOLS = [
    {'name': 'health_data_status', 'description': "Polar connector status (contract wizard.health-data/1): not_configured, needs_login, syncing, connected or rate_limited; the connected account's first name, last/next sync, history span and record counts. Never returns keys or tokens. If not connected, send the owner the Polar app link [Polar](/wizard-polar/); its own screen walks him through setup. Never ask for Polar keys, passwords or codes in chat. For health questions use the Health app's tools instead.",
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': True}},
    {'name': 'health_data_sync', 'description': 'Ask the Polar connector to pull new data from Polar now (it also syncs every 20 minutes). Returns at once; check health_data_status for progress. Reads Polar only.',
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}},
    {'name': 'health_data_days', 'description': 'Raw contract read: normalized Polar day records (readiness from Nightly Recharge, HRV, overnight HR, breathing rate, sleep, Cardio Load on a 0-21 equivalent with the TRIMP value in load.native, calories, steps, exercises, extras.polar), oldest first, paged with next_page; pass changed_since=<cursor> to get only changed days. For owner questions prefer the Health app tools.',
     'inputSchema': {'type': 'object', 'properties': {
         'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'}, 'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
         'limit': {'type': 'integer', 'description': 'days per page, default 100, max 500'},
         'page': {'type': 'string', 'description': 'next_page from the previous reply'},
         'changed_since': {'type': 'string', 'description': 'cursor from an earlier reply; empty string = everything'}},
         'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def request_sync(full=False):
    s = status_info()
    if s['status'] in ('not_configured', 'needs_login'):
        raise Problem(409, 'Polar is not connected. The owner finishes setup in the Polar app (/wizard-polar/).')
    start_sync_thread(full=full)
    return dict(status_info(), note='Sync started.')


def call(name, a):
    if name == 'health_data_status':
        return status_info()
    if name == 'health_data_sync':
        return request_sync()
    if name == 'health_data_days':
        return contract_days(a)
    raise KeyError(name)


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            try:
                v = call(p.get('name'), p.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except (Problem, ProviderError) as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError, AttributeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
# A bare host name (no port) in WIZARD_TRUSTED_HOSTS is trusted on every shell port of that host.
TRUSTED_NAMES = {h.split(':')[0] for h in TRUSTED_HOSTS}
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _host_ok(host):
    host = (host or '').lower()
    return host in TRUSTED_HOSTS or host.split(':')[0] in TRUSTED_NAMES


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if not _host_ok(host):
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS and urlsplit(origin).netloc != host:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def forwarded(self):
        return any(self.headers.get(h) for h in FORWARDED)

    def send(self, code, body=None, kind='application/json', extra=None):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def callback_page(self, ok, message):
        home = PREFIX + '/' if self.path.startswith(PREFIX) else '/'
        title = f'{NAME} connected' if ok else f'{NAME} sign-in did not finish'
        esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{title}</title><body style="font:16px/1.5 system-ui;margin:2rem;max-width:36rem">'
                f'<h1 style="font-size:1.4rem">{title}</h1><p>{esc(message)}</p><p><a href="{home}">Open {NAME}</a></p>')
        self.send(200 if ok else 400, html.encode(), 'text/html; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
        try:
            if path in ('/healthz', '/api/health'):
                s = status_info()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'],
                                       'connected': s['status'] in ('connected', 'syncing', 'rate_limited'), 'last_sync': s['last_sync']})
            if path == '/api/status':
                return self.send(200, status_info())
            if path == '/api/setup':
                return self.send(200, setup_info())
            if path == '/api/days':
                if self.forwarded():
                    return self.send(403, {'error': 'Health data reads are box-only.'})
                return self.send(200, contract_days(query))
            if path in ('/callback', '/oauth/callback'):   # when the Redirect URL points at this app itself
                try:
                    login_finish('http://x' + self.path)
                    return self.callback_page(True, f'Your {NAME} data is downloading now. You can close this tab and go back to the {NAME} app.')
                except (Problem, ProviderError) as e:
                    return self.callback_page(False, e.message)
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if self.forwarded():
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/keys':
                return self.send(200, save_keys(b))
            if path == '/api/keys/test':
                return self.send(200, test_keys())
            if path == '/api/keys/forget':
                return self.send(200, forget_keys())
            if path == '/api/oauth/start':
                return self.send(200, login_start())
            if path == '/api/oauth/finish':
                return self.send(200, login_finish(b.get('url')))
            if path == '/api/sync':
                return self.send(202, request_sync(full=bool(b.get('full'))))
            if path == '/api/disconnect':
                return self.send(200, disconnect(forget=bool(b.get('forget_keys'))))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except ProviderError as e:
            self.send(429 if e.code == 'rate_limited' else 502, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


def main():
    secure_files()
    st = _state()
    if st.get('syncing'):
        _update_state(syncing=None, progress=None, sync_kind=None)   # a restart interrupted it; the scheduler resumes
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()


if __name__ == '__main__':
    main()
