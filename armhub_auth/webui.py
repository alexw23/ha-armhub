#!/usr/bin/env python3
"""
ARM Hub — Add-on web server.

Owns all authentication state:
  - Token stored encrypted at rest in /data/token.enc (key = sha256(password))
  - Silently refreshes access tokens before expiry
  - Proactively re-authenticates before the refresh token expires
  - Manages the Gmail (OAuth) or IMAP watcher for auto MFA code submission
  - Every POST /token verifies credentials — unauthorized callers get 401

JSON endpoints:
  POST /token  Body: {email, password, imap_entity}
               → {status,access_token,expires_on} | {status:mfa_pending|logging_in|refreshing|error}
  GET  /mfa_status → {pending: bool}
  POST /mfa    Body: {code} or form-encoded

HTML pages:
  GET/POST /gmail  Connect a Gmail account via OAuth (client ID/secret from add-on options)
"""

import base64
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import websocket
from cryptography.fernet import Fernet, InvalidToken

sys.path.insert(0, os.path.dirname(__file__))
import armadillo


def _log(msg: str) -> None:
    print(f"[webui {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── B2C constants ─────────────────────────────────────────────────────────────

_B2C_TENANT    = "armlivecp"
_B2C_POLICY    = "b2c_1a_v5_cp_login"
_B2C_CLIENT_ID = "11c5e818-5649-4b43-bf73-901ce67c3d8e"
_B2C_SCOPE     = (
    "https://armlivecp.onmicrosoft.com/armlivecp/user.service.readwrite "
    "openid offline_access profile"
)
_B2C_TOKEN_URL = (
    f"https://{_B2C_TENANT}.b2clogin.com/{_B2C_TENANT}.onmicrosoft.com/"
    f"{_B2C_POLICY}/oauth2/v2.0/token"
)

# ── Paths ─────────────────────────────────────────────────────────────────────

DATA_DIR    = Path(os.getenv("DATA_DIR", "/data"))
MFA_PENDING = DATA_DIR / "mfa_pending"
MFA_CODE    = DATA_DIR / "mfa_code"
TOKEN_FILE  = DATA_DIR / "token.enc"   # Fernet-encrypted JSON

HA_TOKEN    = os.getenv("SUPERVISOR_TOKEN", "")
HA_REST     = "http://supervisor/core/api"
HA_WS       = "ws://supervisor/core/websocket"

# Any address at arm-hub.com / arm-hub.co or a subdomain (e.g. verify@auth.arm-hub.com)
MFA_SENDER_RE = re.compile(r"@(?:[\w-]+\.)*arm-hub\.com?\b", re.IGNORECASE)

OPTIONS_FILE      = DATA_DIR / "options.json"
GMAIL_TOKEN_FILE  = DATA_DIR / "gmail_token.json"
GMAIL_SCOPE       = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_REDIRECT    = "https://my.home-assistant.io/redirect/oauth"
GOOGLE_AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL  = "https://oauth2.googleapis.com/token"
GMAIL_API         = "https://gmail.googleapis.com/gmail/v1/users/me"


def _extract_code(text: str) -> str | None:
    """Pull the 6-digit MFA code out of an email body (plain text or HTML)."""
    text = re.sub(r"(?is)<(style|head)\b.*?</\1>", " ", str(text))
    text = re.sub(r"<[^>]+>", " ", text)  # drops inline styles like color:#333333
    match = (re.search(r"\bis:?\s*(\d{6})\b", text)
             or re.search(r"(?<![#\w])(\d{6})(?!\w)", text))
    return match.group(1) if match else None

# ── State ─────────────────────────────────────────────────────────────────────

_token_lock = threading.Lock()
_token: dict = {}  # access_token, refresh_token, expires_on, refresh_token_obtained_at, refresh_token_lifetime

_auth_lock   = threading.Lock()
_auth_thread: threading.Thread | None = None
_auth_result: dict = {"status": "idle"}

_creds_lock  = threading.Lock()
_email:       str = ""
_password:    str = ""
_creds_hash:  str = ""  # sha256(email:password) — used for verification
_imap_entity: str = ""

_imap_thread: threading.Thread | None = None


# ── Crypto ────────────────────────────────────────────────────────────────────

def _make_fernet(password: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(password.encode()).digest())
    return Fernet(key)


def _save_token() -> None:
    with _creds_lock:
        pw = _password
    if not pw:
        return
    with _token_lock:
        data = dict(_token)
    try:
        TOKEN_FILE.write_bytes(_make_fernet(pw).encrypt(json.dumps(data).encode()))
    except Exception as e:
        _log(f"Failed to save token: {e}")


def _load_token(password: str) -> bool:
    """Decrypt and load token file. Returns True if a valid (non-expired) token was loaded."""
    try:
        encrypted = TOKEN_FILE.read_bytes()
        plaintext = _make_fernet(password).decrypt(encrypted)
        data = json.loads(plaintext)
        with _token_lock:
            _token.update(data)
        now         = time.time()
        at_exp      = data.get("expires_on", 0)
        rt_obtained = data.get("refresh_token_obtained_at", 0)
        rt_lifetime = data.get("refresh_token_lifetime")
        rt_exp      = (rt_obtained + rt_lifetime) if rt_obtained and rt_lifetime else None
        valid       = now < at_exp - 60
        at_str      = datetime.fromtimestamp(at_exp).strftime("%Y-%m-%d %H:%M") if at_exp else "?"
        rt_str      = datetime.fromtimestamp(rt_exp).strftime("%Y-%m-%d %H:%M") if rt_exp else "unknown"
        _log(f"Token loaded (valid={valid}, access_token_expires={at_str}, refresh_token_expires={rt_str})")
        return valid
    except InvalidToken:
        _log("Token file exists but could not be decrypted (wrong password?)")
        return False
    except Exception:
        return False


# ── Credential management ─────────────────────────────────────────────────────

def _set_credentials(email: str, password: str, imap_entity: str) -> None:
    global _email, _password, _creds_hash, _imap_entity
    with _creds_lock:
        _email        = email
        _password     = password
        _creds_hash   = hashlib.sha256(f"{email}:{password}".encode()).hexdigest()
        _imap_entity  = imap_entity or _imap_entity


def _verify_credentials(email: str, password: str) -> bool:
    with _creds_lock:
        if not _creds_hash:
            return True  # first call — accept and store
        expected = hashlib.sha256(f"{email}:{password}".encode()).hexdigest()
        return expected == _creds_hash


# ── Silent refresh ────────────────────────────────────────────────────────────

_B2C_REFRESH_HEADERS = {
    "Content-Type":  "application/x-www-form-urlencoded",
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept":        "application/json, text/plain, */*",
    "Origin":        f"https://armlivecp.b2clogin.com",
    "Referer":       f"https://armlivecp.b2clogin.com/",
}


def _silent_refresh() -> bool:
    with _token_lock:
        rt     = _token.get("refresh_token")
        policy = _token.get("b2c_policy", _B2C_POLICY)
    if not rt:
        return False
    token_url = (
        f"https://{_B2C_TENANT}.b2clogin.com/{_B2C_TENANT}.onmicrosoft.com/"
        f"{policy}/oauth2/v2.0/token"
    )
    _log(f"Silent refresh: using policy={policy}")
    try:
        resp = requests.post(
            token_url,
            data={
                "grant_type":    "refresh_token",
                "client_id":     _B2C_CLIENT_ID,
                "scope":         _B2C_SCOPE,
                "refresh_token": rt,
            },
            headers=_B2C_REFRESH_HEADERS,
            timeout=30,
        )
        if not resp.ok:
            try:
                err = resp.json()
            except Exception:
                err = resp.text[:300]
            _log(f"Silent refresh failed: HTTP {resp.status_code} — {err}")
            return False
        data = resp.json()
        if not data.get("access_token"):
            _log(f"Silent refresh: unexpected response (no access_token): {data}")
            return False
        now = time.time()
        with _token_lock:
            _token["access_token"] = data["access_token"]
            _token["expires_on"]   = now + data.get("expires_in", 3600)
            if data.get("refresh_token"):
                _token["refresh_token"]             = data["refresh_token"]
                _token["refresh_token_obtained_at"] = now
            if data.get("refresh_token_expires_in"):
                _token["refresh_token_lifetime"] = data["refresh_token_expires_in"]
        _save_token()
        _log("Silent refresh succeeded")
        return True
    except Exception as e:
        _log(f"Silent refresh exception: {e}")
        return False


# ── Proactive re-auth ─────────────────────────────────────────────────────────

def _trigger_login() -> None:
    global _auth_thread, _auth_result
    with _creds_lock:
        email = _email
        pw    = _password
    if not email or not pw:
        _log("Trigger login: no credentials in memory")
        return
    with _auth_lock:
        if _auth_thread and _auth_thread.is_alive():
            return
        _auth_result = {"status": "logging_in"}
        _auth_thread = threading.Thread(target=_run_auth, args=(email, pw), daemon=True)
        _auth_thread.start()
    _log(f"Auth thread triggered for {email}")


def _mfa_monitor() -> None:
    """Watch for mfa_pending and start the email watcher immediately — don't wait for coordinator."""
    while True:
        time.sleep(2)
        if MFA_PENDING.exists() and (_gmail_connected() or _imap_entity):
            if not _mfa_watcher_alive():
                _log("MFA pending detected — auto-starting email watcher")
                _start_mfa_watcher()


def _token_monitor() -> None:
    _log("Token monitor started")
    while True:
        time.sleep(60)

        with _creds_lock:
            pw = _password
        if not pw:
            continue  # no credentials yet — nothing we can do

        if MFA_PENDING.exists():
            continue
        with _auth_lock:
            busy = _auth_thread and _auth_thread.is_alive()
        if busy:
            continue

        with _token_lock:
            at_expires  = _token.get("expires_on", 0)
            rt          = _token.get("refresh_token")
            rt_obtained = _token.get("refresh_token_obtained_at", 0)
            rt_lifetime = _token.get("refresh_token_lifetime", 14 * 86400)

        now = time.time()

        if rt and rt_obtained and rt_lifetime:
            rt_expires_at = rt_obtained + rt_lifetime
            if now > rt_expires_at - 1800:  # 30 min before actual expiry
                mins = max(0, int((rt_expires_at - now) / 60))
                _log(f"Refresh token expires in {mins} min — proactive re-auth")
                _trigger_login()
                continue

        if rt and now > at_expires - 600:
            _log("Access token expiring — silent refresh")
            if not _silent_refresh():
                _log("Silent refresh failed — triggering full re-auth")
                _trigger_login()


# ── IMAP watcher ──────────────────────────────────────────────────────────────

def _imap_watcher(entity_id: str) -> None:
    _log(f"IMAP watcher started for {entity_id}")
    headers  = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    deadline = time.time() + 600

    state        = {"entry_id": None}
    SUBSCRIBE_ID = 1
    REGISTRY_ID  = 2

    def _fetch_code(uid: str, entry_id: str) -> None:
        try:
            resp = requests.post(
                f"{HA_REST}/services/imap/fetch",
                headers=headers,
                json={"entry": entry_id, "uid": uid},
                params={"return_response": "true"},
                timeout=10,
            )
            if resp.status_code != 200:
                _log(f"IMAP fetch error {resp.status_code}: {resp.text[:200]}")
                return
            result = resp.json()
            text   = result.get("service_response", {}).get("text", "")
            code   = _extract_code(text)
            if code and MFA_PENDING.exists() and not MFA_CODE.exists():
                _log(f"IMAP watcher: got code {code} — writing mfa_code")
                MFA_CODE.write_text(code)
            else:
                _log(f"IMAP fetch: no code in text={text!r}")
        except Exception as e:
            _log(f"IMAP fetch failed: {e}")

    def _on_message(ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        msg_type = msg.get("type")

        if msg_type == "auth_required":
            ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))

        elif msg_type == "auth_ok":
            _log("IMAP WS: authenticated")
            ws.send(json.dumps({"id": SUBSCRIBE_ID, "type": "subscribe_events", "event_type": "imap_content"}))
            ws.send(json.dumps({"id": REGISTRY_ID,  "type": "config/entity_registry/get", "entity_id": entity_id}))

        elif msg_type == "result":
            if msg.get("id") == REGISTRY_ID:
                state["entry_id"] = (msg.get("result") or {}).get("config_entry_id")
                _log(f"IMAP WS: entry_id={state['entry_id']}")
            elif msg.get("id") == SUBSCRIBE_ID:
                _log(f"IMAP WS: subscribed success={msg.get('success')}")

        elif msg_type == "event":
            data   = msg.get("event", {}).get("data", {})
            sender = data.get("sender", "")
            uid    = str(data.get("uid", ""))
            _log(f"IMAP WS event: sender={sender!r} uid={uid} initial={data.get('initial')}")
            if MFA_SENDER_RE.search(sender) and uid and state["entry_id"] and MFA_PENDING.exists():
                threading.Thread(target=_fetch_code, args=(uid, state["entry_id"]), daemon=True).start()

    def _on_error(ws, err):
        _log(f"IMAP WS error: {err}")

    def _on_close(ws, code, msg):
        if MFA_PENDING.exists() and not MFA_CODE.exists():
            _log(f"IMAP WS closed ({code}) — will reconnect")

    def _force_update():
        try:
            resp = requests.post(
                f"{HA_REST}/services/homeassistant/update_entity",
                headers=headers,
                json={"entity_id": entity_id},
                timeout=5,
            )
            if resp.status_code != 200:
                _log(f"IMAP force-update: status={resp.status_code}")
        except Exception as e:
            _log(f"IMAP force-update failed: {e}")

    ws = websocket.WebSocketApp(HA_WS, on_message=_on_message, on_error=_on_error, on_close=_on_close)
    ws_thread = threading.Thread(target=lambda: ws.run_forever(reconnect=5), daemon=True)
    ws_thread.start()

    while MFA_PENDING.exists() and not MFA_CODE.exists():
        if time.time() >= deadline:
            _log("IMAP watcher: 10-minute timeout reached — stopping")
            break
        _force_update()
        for _ in range(45):
            if not MFA_PENDING.exists() or MFA_CODE.exists() or time.time() >= deadline:
                break
            time.sleep(1)

    ws.close()
    _log("IMAP watcher stopped")


def _start_imap_watcher(entity_id: str) -> None:
    global _imap_thread
    if not entity_id:
        _log("IMAP watcher: no imap_entity configured — auto-fetch disabled")
        return
    if not HA_TOKEN:
        _log("IMAP watcher: SUPERVISOR_TOKEN not set — auto-fetch disabled")
        return
    if _imap_thread and _imap_thread.is_alive():
        _log("IMAP watcher: already running")
        return
    _imap_thread = threading.Thread(target=_imap_watcher, args=(entity_id,), daemon=True)
    _imap_thread.start()


# ── Gmail (OAuth) ─────────────────────────────────────────────────────────────

_gmail_lock    = threading.Lock()
_gmail_tok:    dict = {}  # refresh_token, access_token, expires_on, email
_gmail_pending: dict = {}  # state, verifier — for an in-progress consent
_gmail_thread: threading.Thread | None = None


def _gmail_client() -> tuple[str, str]:
    try:
        opts = json.loads(OPTIONS_FILE.read_text())
    except Exception:
        opts = {}
    return (opts.get("gmail_client_id") or "").strip(), (opts.get("gmail_client_secret") or "").strip()


def _gmail_load() -> None:
    try:
        data = json.loads(GMAIL_TOKEN_FILE.read_text())
    except Exception:
        return
    with _gmail_lock:
        _gmail_tok.update(data)
    _log(f"Gmail token loaded for {data.get('email') or 'unknown account'}")


def _gmail_save() -> None:
    with _gmail_lock:
        data = dict(_gmail_tok)
    try:
        GMAIL_TOKEN_FILE.write_text(json.dumps(data))
        GMAIL_TOKEN_FILE.chmod(0o600)
    except Exception as e:
        _log(f"Failed to save Gmail token: {e}")


def _gmail_connected() -> bool:
    client_id, _ = _gmail_client()
    with _gmail_lock:
        return bool(client_id and _gmail_tok.get("refresh_token"))


def _gmail_auth_url() -> str:
    client_id, _ = _gmail_client()
    with _gmail_lock:
        if not _gmail_pending:
            _gmail_pending.update({
                "state":    base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode(),
                "verifier": base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode(),
            })
        state, verifier = _gmail_pending["state"], _gmail_pending["verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id":             client_id,
        "redirect_uri":          GMAIL_REDIRECT,
        "response_type":         "code",
        "scope":                 GMAIL_SCOPE,
        "access_type":           "offline",
        "prompt":                "consent",
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    })


def _gmail_exchange(pasted: str) -> str:
    """Exchange a pasted redirect URL (or bare code) for tokens. Returns an error message or ''."""
    pasted = pasted.strip()
    if "code=" in pasted:
        query = parse_qs(urlparse(pasted).query or pasted.split("?", 1)[-1])
        code  = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
    else:
        code, state = pasted, None
    with _gmail_lock:
        pending = dict(_gmail_pending)
    if not code:
        return "No authorisation code found in what you pasted."
    if not pending.get("verifier"):
        return "No sign-in in progress — click “Sign in with Google” again."
    if state is not None and state != pending.get("state"):
        return "That link is from an older sign-in attempt — click “Sign in with Google” again."

    client_id, client_secret = _gmail_client()
    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  GMAIL_REDIRECT,
            "code_verifier": pending["verifier"],
        }, timeout=20)
        data = resp.json()
    except Exception as e:
        return f"Token exchange failed: {e}"
    if not resp.ok or not data.get("access_token"):
        return f"Google rejected the code: {data.get('error_description') or data.get('error') or resp.status_code}"
    if not data.get("refresh_token"):
        return "Google didn't return a refresh token — remove the app's access in your Google account and try again."

    with _gmail_lock:
        _gmail_tok.clear()
        _gmail_tok.update({
            "refresh_token": data["refresh_token"],
            "access_token":  data["access_token"],
            "expires_on":    time.time() + data.get("expires_in", 3600),
        })
        _gmail_pending.clear()
    try:
        profile = requests.get(f"{GMAIL_API}/profile",
                               headers={"Authorization": f"Bearer {data['access_token']}"}, timeout=10).json()
        with _gmail_lock:
            _gmail_tok["email"] = profile.get("emailAddress", "")
    except Exception:
        pass
    _gmail_save()
    _log(f"Gmail connected for {_gmail_tok.get('email') or 'unknown account'}")
    return ""


def _gmail_access_token() -> str | None:
    with _gmail_lock:
        at  = _gmail_tok.get("access_token")
        exp = _gmail_tok.get("expires_on", 0)
        rt  = _gmail_tok.get("refresh_token")
    if at and time.time() < exp - 60:
        return at
    if not rt:
        return None
    client_id, client_secret = _gmail_client()
    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": rt,
            "client_id":     client_id,
            "client_secret": client_secret,
        }, timeout=20)
        data = resp.json()
    except Exception as e:
        _log(f"Gmail token refresh exception: {e}")
        return None
    if not resp.ok or not data.get("access_token"):
        _log(f"Gmail token refresh failed: {data.get('error_description') or data.get('error') or resp.status_code}"
             " — reconnect Gmail from the add-on panel")
        return None
    with _gmail_lock:
        _gmail_tok["access_token"] = data["access_token"]
        _gmail_tok["expires_on"]   = time.time() + data.get("expires_in", 3600)
        if data.get("refresh_token"):
            _gmail_tok["refresh_token"] = data["refresh_token"]
    _gmail_save()
    return data["access_token"]


def _gmail_body(payload: dict) -> str:
    """Return the text/plain part of a Gmail message payload, falling back to text/html."""
    found: dict = {}

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime in ("text/plain", "text/html") and mime not in found:
            found[mime] = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    return found.get("text/plain") or found.get("text/html", "")


def _gmail_watcher() -> None:
    _log(f"Gmail watcher started for {_gmail_tok.get('email') or 'connected account'}")
    deadline = time.time() + 600
    try:
        since_ms = int((MFA_PENDING.stat().st_mtime - 60) * 1000)
    except FileNotFoundError:
        return
    seen: set[str] = set()

    while MFA_PENDING.exists() and not MFA_CODE.exists() and time.time() < deadline:
        at = _gmail_access_token()
        if not at:
            _log("Gmail watcher: no access token — stopping")
            break
        headers = {"Authorization": f"Bearer {at}"}
        try:
            resp = requests.get(f"{GMAIL_API}/messages", headers=headers,
                                params={"q": "newer_than:1d", "maxResults": 10}, timeout=15)
            if resp.status_code != 200:
                _log(f"Gmail list error {resp.status_code}: {resp.text[:200]}")
            for item in (resp.json().get("messages") or []) if resp.ok else []:
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                msg = requests.get(f"{GMAIL_API}/messages/{item['id']}", headers=headers,
                                   params={"format": "full"}, timeout=15).json()
                if int(msg.get("internalDate", 0)) < since_ms:
                    continue
                sender = next((h["value"] for h in msg.get("payload", {}).get("headers", [])
                               if h.get("name", "").lower() == "from"), "")
                if not MFA_SENDER_RE.search(sender):
                    continue
                code = _extract_code(_gmail_body(msg.get("payload", {})))
                _log(f"Gmail: MFA email from {sender!r} — code {'found' if code else 'not found'}")
                if code and MFA_PENDING.exists() and not MFA_CODE.exists():
                    _log(f"Gmail watcher: got code {code} — writing mfa_code")
                    MFA_CODE.write_text(code)
                    break
        except Exception as e:
            _log(f"Gmail poll failed: {e}")
        for _ in range(10):
            if not MFA_PENDING.exists() or MFA_CODE.exists():
                break
            time.sleep(1)

    _log("Gmail watcher stopped")


def _start_mfa_watcher() -> None:
    """Start the Gmail watcher if Gmail is connected, otherwise the IMAP watcher."""
    global _gmail_thread
    if _gmail_connected():
        if not (_gmail_thread and _gmail_thread.is_alive()):
            _gmail_thread = threading.Thread(target=_gmail_watcher, daemon=True)
            _gmail_thread.start()
    elif _imap_entity:
        _start_imap_watcher(_imap_entity)


def _mfa_watcher_alive() -> bool:
    return bool((_gmail_thread and _gmail_thread.is_alive()) or (_imap_thread and _imap_thread.is_alive()))


# ── Auth thread ───────────────────────────────────────────────────────────────

def _run_auth(email: str, password: str) -> None:
    global _auth_result
    _log(f"Auth thread started for {email}")
    try:
        token_data = armadillo.get_token_data(email, password)
        _log("Auth thread succeeded — token obtained")
        now = time.time()
        with _token_lock:
            _token.update({
                "access_token":              token_data["access_token"],
                "refresh_token":             token_data.get("refresh_token"),
                "expires_on":                token_data.get("expires_on", now + 3600),
                "refresh_token_obtained_at": now,
                "refresh_token_lifetime":    token_data.get("refresh_token_expires_in"),
                "b2c_policy":                token_data.get("b2c_policy", _B2C_POLICY),
            })
        _save_token()
        with _auth_lock:
            _auth_result = {
                "status":        "ok",
                "access_token":  token_data["access_token"],
                "refresh_token": token_data.get("refresh_token"),
                "expires_on":    token_data.get("expires_on", now + 3600),
            }
    except Exception as exc:
        _log(f"Auth thread failed: {exc}")
        with _auth_lock:
            _auth_result = {"status": "error", "message": str(exc)}


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_WAITING = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>ARM Hub</title>
<meta http-equiv="refresh" content="5">
<style>body{{font-family:sans-serif;max-width:480px;margin:60px auto;padding:0 16px}}
h2{{color:#03a9f4}}p{{color:#666}}</style>
</head><body>
  <h2>ARM Hub</h2><p>Waiting for next fetch…</p>
  <p><small>This page refreshes automatically.</small></p>
  <p><a href="gmail">Gmail auto-MFA settings</a></p>
</body></html>"""

HTML_GMAIL = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Gmail — ARM Hub</title>
<style>body{{font-family:sans-serif;max-width:560px;margin:60px auto;padding:0 16px}}
h2{{color:#03a9f4}}p,li{{color:#444}}.err{{color:#c62828}}.ok{{color:#2e7d32}}
input{{width:100%;font-size:1rem;padding:8px;box-sizing:border-box;border:2px solid #03a9f4;border-radius:6px}}
button,.btn{{display:inline-block;font-size:1rem;padding:10px 20px;background:#03a9f4;color:#fff;
        border:none;border-radius:6px;cursor:pointer;text-decoration:none;margin-top:8px}}</style>
</head><body>
  <h2>Gmail auto-MFA</h2>
  {body}
  <p><a href="./">← Back</a></p>
</body></html>"""

HTML_MFA = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>MFA Required — ARM Hub</title>
<style>body{{font-family:sans-serif;max-width:480px;margin:60px auto;padding:0 16px}}
h2{{color:#e65100}}
input{{font-size:1.4rem;width:8rem;letter-spacing:.2rem;text-align:center;
      border:2px solid #e65100;border-radius:6px;padding:8px}}
button{{font-size:1rem;padding:10px 24px;background:#e65100;color:#fff;
        border:none;border-radius:6px;cursor:pointer;margin-left:8px}}</style>
</head><body>
  <h2>📱 MFA Code Required</h2>
  <p>Enter the 6-digit SMS code sent to your phone:</p>
  <form method="POST" action="mfa">
    <input type="text" name="code" maxlength="6" pattern="\\d{{6}}"
           autofocus autocomplete="one-time-code" placeholder="000000">
    <button type="submit">Submit</button>
  </form>
  <p><small>{hint}</small></p>
</body></html>"""

HTML_SUBMITTED = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Submitted</title>
<meta http-equiv="refresh" content="3; url=/">
<style>body{{font-family:sans-serif;max-width:480px;margin:60px auto}}</style>
</head><body>
  <h2 style="color:#2e7d32">✓ Code submitted</h2><p>Returning…</p>
</body></html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_html(self, code: int, body: str) -> None:
        enc = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(enc)))
        self.end_headers()
        self.wfile.write(enc)

    def _send_json(self, code: int, data: dict) -> None:
        enc = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(enc)))
        self.end_headers()
        self.wfile.write(enc)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def _send_gmail_page(self, error: str = "", notice: str = "") -> None:
        client_id, client_secret = _gmail_client()
        parts = []
        if error:
            parts.append(f'<p class="err">{html.escape(error)}</p>')
        if notice:
            parts.append(f'<p class="ok">{html.escape(notice)}</p>')
        if not client_id or not client_secret:
            parts.append("<p>Set <b>gmail_client_id</b> and <b>gmail_client_secret</b> in the add-on's "
                         "Configuration tab (Google Cloud OAuth client, type <i>Desktop app</i>), "
                         "restart the add-on, then come back here.</p>")
        else:
            with _gmail_lock:
                email = _gmail_tok.get("email")
                connected = bool(_gmail_tok.get("refresh_token"))
            if connected:
                parts.append(f'<p class="ok">✓ Connected as <b>{html.escape(email or "unknown account")}</b>. '
                             "MFA codes will be read from this inbox automatically.</p>")
            parts.append(
                "<ol><li>Click the button and approve read-only Gmail access.</li>"
                "<li>Google will send you to a <code>localhost</code> page that fails to load — that's expected.</li>"
                "<li>Copy the whole URL from the address bar and paste it below.</li></ol>"
                f'<a class="btn" href="{html.escape(_gmail_auth_url())}" target="_blank" rel="noopener">'
                f'{"Reconnect" if connected else "Sign in with Google"}</a>'
                '<form method="POST" action="gmail" style="margin-top:16px">'
                '<input type="text" name="redirect" placeholder="http://localhost/?state=…&amp;code=…" required>'
                '<button type="submit">Connect</button></form>'
            )
        self._send_html(200, HTML_GMAIL.format(body="\n  ".join(parts)))

    def do_GET(self):
        if self.path == "/mfa_status":
            self._send_json(200, {"pending": MFA_PENDING.exists()})
        elif self.path.split("?", 1)[0] == "/gmail":
            self._send_gmail_page()
        elif MFA_PENDING.exists():
            hint = MFA_PENDING.read_text().strip() or "Check your phone."
            self._send_html(200, HTML_MFA.format(hint=hint))
        else:
            self._send_html(200, HTML_WAITING)

    def do_POST(self):
        if self.path == "/token":
            self._handle_token_request()
        elif self.path == "/mfa":
            self._handle_mfa_submit()
        elif self.path == "/gmail":
            params = parse_qs(self._read_body().decode(errors="replace"))
            error  = _gmail_exchange(params.get("redirect", [""])[0])
            self._send_gmail_page(error=error, notice="" if error else "Gmail connected.")
            if not error and MFA_PENDING.exists():
                _start_mfa_watcher()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_token_request(self):
        global _auth_thread, _auth_result

        raw = self._read_body()
        try:
            payload = json.loads(raw)
        except Exception:
            self._send_json(400, {"error": "expected JSON body"})
            return

        email    = payload.get("email", "").strip()
        password = payload.get("password", "").strip()
        imap_ent = payload.get("imap_entity", "").strip()

        if not email or not password:
            self._send_json(400, {"error": "email and password required"})
            return

        # Verify credentials — reject unauthorised callers
        if not _verify_credentials(email, password):
            _log(f"POST /token: rejected invalid credentials for {email}")
            self._send_json(401, {"error": "invalid credentials"})
            return

        # First call: store credentials in memory and try to decrypt any saved token
        with _creds_lock:
            first_call = not _creds_hash

        if first_call:
            _set_credentials(email, password, imap_ent)
            _load_token(password)
        elif imap_ent and imap_ent != _imap_entity:
            _set_credentials(email, password, imap_ent)

        _log(f"POST /token for {email}")

        # Return valid stored token immediately — even during proactive re-auth
        with _token_lock:
            at  = _token.get("access_token")
            exp = _token.get("expires_on", 0)

        if at and time.time() < exp - 60:
            self._send_json(200, {
                "status":       "ok",
                "access_token": at,
                "expires_on":   int(exp),
            })
            return

        with _auth_lock:
            thread_alive = _auth_thread is not None and _auth_thread.is_alive()

        if MFA_PENDING.exists():
            if thread_alive:
                _log("MFA pending — starting email watcher if needed")
                _start_mfa_watcher()
                self._send_json(200, {"status": "mfa_pending"})
                return
            else:
                _log("Stale mfa_pending — clearing")
                MFA_PENDING.unlink(missing_ok=True)
                MFA_CODE.unlink(missing_ok=True)

        # Check auth thread result
        with _auth_lock:
            result = _auth_result.copy()

        if result.get("status") == "ok":
            _log("Returning fresh token from auth thread")
            with _auth_lock:
                _auth_result = {"status": "idle"}
            self._send_json(200, result)
            return

        if result.get("status") == "error":
            _log(f"Returning auth error: {result.get('message')}")
            with _auth_lock:
                _auth_result = {"status": "idle"}
            self._send_json(200, result)
            return

        # Try silent refresh before falling back to full browser login
        if not thread_alive:
            with _token_lock:
                has_rt = bool(_token.get("refresh_token"))
            if has_rt:
                _log("Access token expired — attempting silent refresh")
                if _silent_refresh():
                    with _token_lock:
                        at  = _token["access_token"]
                        exp = _token["expires_on"]
                    self._send_json(200, {"status": "ok", "access_token": at, "expires_on": int(exp)})
                    return
                _log("Silent refresh failed — starting full browser login")

            _log(f"Starting auth thread for {email}")
            with _auth_lock:
                _auth_result = {"status": "logging_in"}
                _auth_thread = threading.Thread(
                    target=_run_auth, args=(email, password), daemon=True
                )
                _auth_thread.start()
        else:
            _log("Auth thread already running")

        self._send_json(200, {"status": "logging_in"})

    def _handle_mfa_submit(self):
        raw          = self._read_body()
        content_type = self.headers.get("Content-Type", "")

        if "application/json" in content_type:
            try:
                code = json.loads(raw).get("code", "").strip()
            except Exception:
                code = ""
        else:
            params = parse_qs(raw.decode(errors="replace"))
            code   = params.get("code", [""])[0].strip()

        if code:
            _log(f"MFA code received ({len(code)} digits)")
            MFA_CODE.write_text(code)
        else:
            _log("MFA submit: empty code")

        if "application/json" not in content_type:
            self._send_html(200, HTML_SUBMITTED)
        else:
            self._send_json(200, {"ok": True})


if __name__ == "__main__":
    MFA_PENDING.unlink(missing_ok=True)
    MFA_CODE.unlink(missing_ok=True)
    _log("Cleared stale MFA state on startup")
    _gmail_load()

    threading.Thread(target=_token_monitor, daemon=True).start()
    threading.Thread(target=_mfa_monitor, daemon=True).start()

    server = HTTPServer(("0.0.0.0", 8099), Handler)
    _log("webui listening on :8099")
    server.serve_forever()
