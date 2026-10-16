#!/usr/bin/env python3
"""Poke every RemiliaNET friend as soon as their 24h poke cooldown is up.

Uses RemiliaNET's official API through a "Login" app registered in the
developer portal. You sign in once in your browser; the script keeps the
access the site grants it (never your password) and renews it as it goes.
Meant to run every 15 minutes from launchd (see install.sh); runs with
nobody due exit without touching the network.

In the developer portal, give the app the redirect URI
http://127.0.0.1:8765/callback and the remilia:pokes.write scope
(and offline_access if offered, so the sign-in survives the laptop sleeping).

    python3 poke_friends.py --login CLIENT_ID   one-time: sign in in your browser
    python3 poke_friends.py --dry-run           show who is due, without poking
    python3 poke_friends.py                     poke everyone who is due
"""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SITE = "https://www.remilia.net"
API = SITE + "/api/v1"
OIDC = SITE + "/oidc/realms/remilia/protocol/openid-connect"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
POKE_SCOPE = "remilia:pokes.write"
USER_AGENT = "remilianet-poke (personal script)"

APP_DIR = Path.home() / "Library/Application Support/remilianet-poke"
AUTH_FILE = APP_DIR / "auth.json"
STATE_FILE = APP_DIR / "state.json"

RUN_INTERVAL = 15 * 60  # keep in sync with StartInterval in install.sh
COOLDOWN = 24 * 3600
FRIENDS_REFRESH = 6 * 3600  # re-fetch the friend list at least this often, to pick up new friends
FAILED_RETRY = 6 * 3600
UNKNOWN_COOLDOWN_RETRY = 3600
DELAY = 0.5
RATE_LIMIT_BACKOFF = 10
MAX_RETRIES = 3


class NeedsLogin(Exception):
    """Something only a new --login (or a fix in the developer portal) can solve; scheduled runs pause."""


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


def notify(message):
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title \"RemiliaNET pokes\""
    subprocess.run(["osascript", "-e", script], capture_output=True)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def write_json(path, data):
    """Atomic write, readable only by you (auth.json holds the sign-in)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def http(method, url, form=None, token=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = urllib.parse.urlencode(form).encode() if form is not None else (b"" if method == "POST" else None)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    return status, body


class Auth:
    def __init__(self, data):
        self.data = data

    def save(self):
        write_json(AUTH_FILE, self.data)

    def request_token(self, form):
        status, body = http("POST", f"{OIDC}/token", {"client_id": self.data["client_id"], **form})
        body = body or {}
        # 400/401 is OAuth rejecting the sign-in or the app; anything else (rate limits, outages) is transient.
        if status in (400, 401):
            raise NeedsLogin(body.get("error_description") or body.get("error") or f"token request failed (HTTP {status})")
        if status != 200:
            raise RuntimeError(f"token request failed (HTTP {status})")
        now = time.time()
        self.data.pop("needs_login", None)
        self.data["access_token"] = body["access_token"]
        self.data["access_expires_at"] = now + body.get("expires_in", 300) - 30
        self.data["refresh_token"] = body.get("refresh_token", self.data.get("refresh_token"))
        self.data["scope"] = body.get("scope")
        # 0 means the refresh token has no fixed expiry (an offline token).
        refresh_expires_in = body.get("refresh_expires_in", 0)
        self.data["refresh_expires_at"] = now + refresh_expires_in if refresh_expires_in else None
        self.save()
        return body

    def access_token(self, force=False):
        if force or time.time() >= self.data.get("access_expires_at", 0):
            self.request_token({"grant_type": "refresh_token", "refresh_token": self.data["refresh_token"]})
        return self.data["access_token"]


def api(auth, method, path):
    status, body = http(method, API + path, token=auth.access_token())
    if status == 401:
        status, body = http(method, API + path, token=auth.access_token(force=True))
    return status, body


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def browser_sign_in(client_id, scope):
    """Authorization code + PKCE: open the real sign-in page and catch the redirect on localhost."""
    verifier = b64url(secrets.token_bytes(32))
    state = secrets.token_urlsafe(16)
    url = f"{OIDC}/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    })

    params = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            params.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<p>Done. You can close this tab and go back to the terminal.</p>".encode())

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", REDIRECT_PORT), Callback) as server:
        server.timeout = 5
        print(f"Opening your browser to sign in. If it doesn't open, visit:\n\n{url}\n")
        webbrowser.open(url)
        deadline = time.time() + 600
        while not params and time.time() < deadline:
            server.handle_request()

    if not params:
        sys.exit("Timed out waiting for the browser sign-in.")
    if "error" in params:
        return None, f"{params['error']}: {params.get('error_description', '')}"
    if params.get("state") != state or "code" not in params:
        sys.exit("The sign-in redirect didn't match this request. Run --login again.")
    return {"grant_type": "authorization_code", "code": params["code"], "redirect_uri": REDIRECT_URI, "code_verifier": verifier}, None


def login(client_id):
    auth = Auth({"client_id": client_id})
    # offline_access lets the sign-in survive long gaps (e.g. a laptop asleep overnight), if the site allows it.
    for scope in (f"openid offline_access {POKE_SCOPE}", f"openid {POKE_SCOPE}"):
        grant, error = browser_sign_in(client_id, scope)
        if grant:
            try:
                token = auth.request_token(grant)
                break
            except (NeedsLogin, RuntimeError) as e:
                error = str(e)
        if "offline" not in scope or "offline" not in error.lower():
            sys.exit(f"Sign-in failed: {error}")
        print("Long-lived access isn't allowed for this app; signing in again without it.")

    status, me = api(auth, "GET", "/me")
    if status != 200:
        sys.exit(f"Signed in, but /me returned HTTP {status}: {me}")
    auth.data["handle"] = me["user"]["username"]
    auth.save()

    granted = token.get("scope", "").split()
    print(f"Signed in as ~{auth.data['handle']}.")
    if POKE_SCOPE not in granted:
        print(f"Warning: the token is missing {POKE_SCOPE}. Add that scope to the app in the developer portal.")
    if auth.data["refresh_expires_at"] is None:
        print("This sign-in doesn't expire on a fixed schedule; it's renewed on every run.")
    else:
        minutes = round((auth.data["refresh_expires_at"] - time.time()) / 60)
        print(f"This sign-in lapses after about {minutes} minutes without a renewal. "
              "Runs renew it, but a long sleep or a session limit can still end it; you'd then get a notification to sign in again.")


def fetch_friends(handle):
    """The friend list is public; there's no official API for it yet, so this uses the one the website uses."""
    friends = []
    for page in range(1, 1000):
        query = urllib.parse.urlencode({"username": handle, "page": page, "limit": 50})
        status, body = http("GET", f"{SITE}/api/profile/friends?{query}")
        if status != 200:
            raise RuntimeError(f"could not load friends page {page} (HTTP {status})")
        friends += [f["user"]["userHandle"] for f in body["friends"]]
        if not body["pagination"]["hasMore"]:
            return friends
    raise RuntimeError("friend list never ended")


def parse_time(value):
    """ISO 8601 to epoch seconds, or None. Python 3.9's fromisoformat only takes 3 or 6 fractional digits, so pad or trim to 6."""
    text = re.sub(r"\.(\d+)", lambda m: "." + m[1][:6].ljust(6, "0"), str(value).replace("Z", "+00:00"), count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def poke(auth, friend, next_poke_at):
    """Returns "poked", "cooldown" or "failed", and records when the friend can next be poked."""
    for attempt in range(MAX_RETRIES + 1):
        status, body = api(auth, "POST", f"/users/{urllib.parse.quote(friend, safe='')}/poke")
        if 200 <= status < 300:
            next_poke_at[friend] = time.time() + COOLDOWN + 5
            return "poked"

        error = (body or {}).get("error") or {}
        if status == 429 and error.get("code") == "poke_cooldown":
            allowed_at = parse_time(error.get("next_allowed_at") or (error.get("details") or {}).get("next_allowed_at"))
            if not allowed_at:
                log(f"~{friend}: cooldown without a usable next_allowed_at: {json.dumps(body)[:300]}")
            next_poke_at[friend] = allowed_at + 5 if allowed_at else time.time() + UNKNOWN_COOLDOWN_RETRY
            return "cooldown"
        if status == 429 and attempt < MAX_RETRIES:
            time.sleep(RATE_LIMIT_BACKOFF)
            continue
        # A 403 with the poke scope granted is about this one friend, so it fails just them; only a missing scope pauses everything.
        granted = auth.data.get("scope")
        if status == 403 and granted is not None and POKE_SCOPE not in granted.split():
            raise NeedsLogin(f"the sign-in is missing {POKE_SCOPE} ({error.get('code')}: {error.get('message')})")

        log(f"~{friend}: failed with HTTP {status} {error.get('code', '')} {error.get('message', '')}")
        next_poke_at[friend] = time.time() + FAILED_RETRY
        return "failed"


def run(args):
    auth_data = read_json(AUTH_FILE)
    if not auth_data.get("refresh_token"):
        sys.exit("Not signed in yet. Run: python3 poke_friends.py --login CLIENT_ID")
    if args.scheduled and auth_data.get("needs_login"):
        return
    auth = Auth(auth_data)

    state = read_json(STATE_FILE)
    next_poke_at = state.setdefault("next_poke_at", {})
    friends = state.get("friends") or []
    now = time.time()
    friends_fresh = friends and now - state.get("friends_fetched_at", 0) < FRIENDS_REFRESH

    if friends_fresh and not args.dry_run and all(next_poke_at.get(f, 0) > now + RUN_INTERVAL for f in friends):
        refresh_expires_at = auth.data.get("refresh_expires_at")
        if refresh_expires_at and refresh_expires_at < now + 2 * RUN_INTERVAL:
            auth.access_token(force=True)  # keep the sign-in alive until the next run
        return

    if not friends_fresh:
        friends = fetch_friends(auth.data["handle"])
        state["friends"], state["friends_fetched_at"] = friends, now
        write_json(STATE_FILE, state)

    # Friends due within the next run interval are waited for, so poke times don't drift a run later each day.
    due = sorted((next_poke_at.get(f, 0), f) for f in friends)
    due = [(ready_at, f) for ready_at, f in due if ready_at <= now + RUN_INTERVAL]
    log(f"~{auth.data['handle']}: {len(friends)} friends, {len(due)} due for a poke")
    if args.dry_run:
        auth.access_token(force=True)
        print("Sign-in renewed fine.")
        if due:
            print("Would poke: " + ", ".join(f"~{f}" for _, f in due))
        return

    results = {"poked": 0, "cooldown": 0, "failed": 0}
    for ready_at, friend in due:
        if ready_at > time.time():
            time.sleep(ready_at - time.time())
        results[poke(auth, friend, next_poke_at)] += 1
        write_json(STATE_FILE, state)
        time.sleep(DELAY)
    log(f"Poked {results['poked']}, {results['cooldown']} still on cooldown, {results['failed']} failed.")


def main():
    parser = argparse.ArgumentParser(description="Poke every RemiliaNET friend whose cooldown is up.")
    parser.add_argument("--login", nargs="?", const="", metavar="CLIENT_ID", help="sign in in your browser (client ID from the developer portal; reuses the saved one if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="show who is due, without poking")
    parser.add_argument("--scheduled", action="store_true", help="set by launchd; stays quiet while a new sign-in is needed")
    args = parser.parse_args()

    if args.login is not None:
        client_id = args.login or read_json(AUTH_FILE).get("client_id")
        if not client_id:
            sys.exit("Pass the client ID from the developer portal: --login CLIENT_ID")
        login(client_id)
        return

    try:
        run(args)
    except NeedsLogin as e:
        auth_data = read_json(AUTH_FILE)
        if not auth_data.get("needs_login"):
            auth_data["needs_login"] = True
            write_json(AUTH_FILE, auth_data)
            notify(f"Paused: {e}. Run: python3 poke_friends.py --login")
        log(f"Paused: {e}. Run: python3 poke_friends.py --login")
        sys.exit(1)
    except (RuntimeError, OSError) as e:
        log(f"Error, will retry next run: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
