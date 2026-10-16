#!/usr/bin/env python3
"""Poke one RemiliaNET user every few minutes, around the clock, without an API key: friends whose
24h cooldown is up first, then everyone else it can find. How often, and how many per day, is set in settings.ini. After poking someone who isn't a friend it sends
them a friend request (or accepts theirs), and never asks anyone who declined again. Also views the profile
of everyone else who shows up in the activity feed (the site notifies them).

Drives a headless Google Chrome (through Playwright) that is signed in to
remilia.net in its own profile, separate from your everyday Chrome. launchd keeps it
running; it times each poke itself, keeping the page open while the next poke is near,
and views whoever the feed has shown in between, with the same requests the website
makes. Having the page open regularly also keeps the website's sign-in fresh. Runs
next to an always-on --watch-feed listener (see install_browser.sh).

    python3 poke_browser.py --login       one-time: sign in in a Chrome window (tick "Remember me")
    python3 poke_browser.py --dry-run     show who is due a poke or a view, without doing either
    python3 poke_browser.py               poke the next person now, and view everyone who is due
    python3 poke_browser.py --watch-feed  record everyone in the activity feed (runs forever)
"""
import argparse
import configparser
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SITE = "https://www.remilia.net"
FEED_URL = SITE + "/api/activityFeed"
USER_AGENT = "remilianet-poke (personal script)"

APP_DIR = Path.home() / "Library/Application Support/remilianet-poke-browser"
VENV_PYTHON = APP_DIR / "venv/bin/python"
PROFILE_DIR = APP_DIR / "chrome-profile"
COOKIES_FILE = APP_DIR / "cookies.json"
STATE_FILE = APP_DIR / "state.json"
FEED_FILE = APP_DIR / "feed.json"  # written only by --watch-feed
EVERYONE_FILE = APP_DIR / "everyone.json"  # everyone discovered so far, and who has been crawled
SETTINGS_FILE = APP_DIR / "settings.ini"  # copied from the project folder by install_browser.sh

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    # Playwright lives in install_browser.sh's Python environment; hop into it so `python3 poke_browser.py` just works.
    if VENV_PYTHON.exists() and Path(sys.executable) != VENV_PYTHON:
        os.execv(VENV_PYTHON, [str(VENV_PYTHON), *sys.argv])
    sys.exit("Playwright isn't installed yet. Run ./install_browser.sh first.")

# The scheduled bot runs all the time and times each poke itself (launchd just keeps it running).
OPEN_CHROME_AHEAD = 30  # start Chrome this long before a poke is due
CLOSE_CHROME_ABOVE = 3 * 60  # close Chrome while the next poke is further away than this
CHROME_SESSION = 15 * 60  # restart Chrome this often, so it stays fresh
ERROR_RETRY = 60
COOLDOWN = 24 * 3600
FRIENDS_REFRESH = 6 * 3600  # re-fetch the friend list at least this often, to pick up new friends
FAILED_RETRY = 6 * 3600
UNKNOWN_COOLDOWN_RETRY = 3600
DELAY = 0.5
RATE_LIMIT_BACKOFF = 10
MAX_RETRIES = 3
SIGN_IN_WAIT = 45  # how long a run gives the page to restore its sign-in
LOGIN_TIMEOUT = 15 * 60
VIEW_EVERY = 24 * 3600  # view someone at most this often, and only if the feed has shown them since
FEED_IDLE_TIMEOUT = 300  # reconnect if the feed sends nothing for this long
FEED_RECONNECT = 10
# One poke at a time, around the clock: friends whose cooldown is up first, then everyone else.
# How many pokes and friend requests, and how far apart, come from settings.ini (see load_settings).
LIMIT_REST = 15 * 60  # with today's poke limit reached, still open the page this often to keep the sign-in fresh
MAX_TRIES_PER_SLOT = 5  # people found to be on cooldown don't use up a slot; try this many in a row at most
DISCOVER_BELOW = 50  # look for more people when fewer than this many non-friends are due
CRAWL_PAGES_PER_RUN = 20  # friend-list pages read per look, to find more people
CRAWL_MAX_PAGES_PER_USER = 4  # first 200 friends of each person is plenty to keep discovering
STOP_AFTER_FAILURES = 5  # this many failed pokes in a row: the site is unhappy, so rest
FAILURE_REST = 15 * 60  # long enough to back off, short enough to keep the sign-in fresh

# Runs inside the page: same-origin requests carry the authToken cookie the website keeps fresh.
FETCH = """async ([method, path, payload]) => {
  try {
    const headers = {Accept: 'application/json'};
    const init = {method, headers, credentials: 'same-origin'};
    if (payload !== null) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(payload);
    }
    const res = await fetch(path, init);
    let body = null;
    try { body = await res.json(); } catch {}
    return {status: res.status, body};
  } catch {
    return {status: 0, body: null};
  }
}"""


class NeedsLogin(Exception):
    """Something only a new --login can solve; scheduled runs pause."""


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
    """Atomic write, readable only by you (cookies.json holds the sign-in)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_settings():
    """Reads settings.ini. Anything missing falls back to the default; anything unreadable too, with a note in the log."""
    parser = configparser.ConfigParser()
    try:
        parser.read(SETTINGS_FILE)
    except configparser.Error as e:
        log(f"settings.ini couldn't be read ({e}); using the defaults.")
        parser = configparser.ConfigParser()

    def number(section, key, default, lowest):
        try:
            value = parser.getfloat(section, key, fallback=default)
        except ValueError:
            log(f"settings.ini: [{section}] {key} isn't a number; using {default}.")
            value = default
        return max(value, lowest)

    try:
        friends_first = parser.getboolean("pokes", "friends_first", fallback=True)
    except ValueError:
        log("settings.ini: [pokes] friends_first should be yes or no; using yes.")
        friends_first = True

    shortest, longest = sorted((number("pokes", "min_minutes_between", 1, 1), number("pokes", "max_minutes_between", 5, 1)))
    return {
        "pokes_per_day": int(number("pokes", "per_day", 500, 0)),
        "friends_first": friends_first,
        "friend_requests_per_day": int(number("friend requests", "per_day", 500, 0)),
        "gap": (shortest * 60, longest * 60),
    }


def today(state):
    """Today's poke and friend-request counts, which start again from zero at midnight."""
    day = datetime.now().strftime("%Y-%m-%d")
    if state.get("today", {}).get("day") != day:
        state["today"] = {"day": day, "pokes": 0, "friend_requests": 0}
    return state["today"]


class Site:
    """Chrome with remilia.net open: headless for runs, a visible window for --login."""

    def __init__(self, playwright, headless):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.context = playwright.chromium.launch_persistent_context(PROFILE_DIR, channel="chrome", headless=headless)
        # Chrome drops session-only cookies when it quits, and the sign-in may use them; put back the ones saved last run.
        saved = read_json(COOKIES_FILE).get("cookies")
        if saved:
            self.context.add_cookies(saved)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(SITE, wait_until="load", timeout=60000)

    def api(self, method, path, body=None):
        """Sends the request (with body as JSON, if given) from inside the page, so it carries the website's sign-in.
        Status 0 means it couldn't be sent."""
        for _ in range(5):
            try:
                result = self.page.evaluate(FETCH, [method, path, body])
                return result["status"], result["body"]
            except PlaywrightError:
                # The page navigated mid-call (it does while restoring a sign-in); let it settle and try again.
                self.page.wait_for_load_state("load")
                self.wait(1)
        raise RuntimeError(f"the page kept navigating during {method} {path}")

    def wait(self, seconds):
        self.page.wait_for_timeout(seconds * 1000)

    def signed_in_handle(self, wait):
        """Polls until the page has restored its sign-in. Returns (handle or None, the last whoami status)."""
        deadline = time.time() + wait
        while True:
            status, body = self.api("GET", "/api/profile/whoami")
            if status == 200 and (body or {}).get("userHandle"):
                return body["userHandle"], status
            if time.time() >= deadline:
                return None, status
            self.wait(2)

    def require_sign_in(self):
        handle, status = self.signed_in_handle(SIGN_IN_WAIT)
        if handle:
            self.save_cookies()
            return handle
        if status == 401:
            raise NeedsLogin("the website isn't signed in any more")
        raise RuntimeError(f"couldn't check the sign-in (HTTP {status})")

    def restore_sign_in(self):
        """Reloads the page so it renews its sign-in."""
        self.page.reload(wait_until="load", timeout=60000)
        self.require_sign_in()

    def save_cookies(self):
        write_json(COOKIES_FILE, {"cookies": self.context.cookies()})

    def close(self):
        self.context.close()


def login():
    with sync_playwright() as playwright:
        site = Site(playwright, headless=False)
        try:
            handle, _ = site.signed_in_handle(0)
            if not handle:
                print('Log in to RemiliaNET in the Chrome window that just opened, and tick "Remember me". '
                      "This waits until you're signed in.")
                handle, _ = site.signed_in_handle(LOGIN_TIMEOUT)
            if not handle:
                sys.exit("Timed out waiting for the sign-in.")
            site.save_cookies()
        finally:
            site.close()

    state = read_json(STATE_FILE)
    state.pop("needs_login", None)
    write_json(STATE_FILE, state)
    print(f"Signed in as ~{handle}. Each run renews the sign-in; if it ever lapses (say, after a long sleep), "
          "you'll get a notification to run --login again.")


def fetch_friends(site, handle):
    friends = []
    for page in range(1, 1000):
        query = urllib.parse.urlencode({"username": handle, "page": page, "limit": 50})
        status, body = site.api("GET", f"/api/profile/friends?{query}")
        if status != 200:
            raise RuntimeError(f"could not load friends page {page} (HTTP {status})")
        friends += [f["user"]["userHandle"] for f in body["friends"]]
        if not body["pagination"]["hasMore"]:
            return friends
    raise RuntimeError("friend list never ended")


def poke(site, friend, next_poke_at):
    """Returns "poked", "cooldown" or "failed", and records when the friend can next be poked."""
    quoted = urllib.parse.quote(friend, safe="")
    for attempt in range(MAX_RETRIES + 1):
        status, body = site.api("POST", f"/api/users/{quoted}/poke")
        if 200 <= status < 300:
            next_poke_at[friend] = time.time() + COOLDOWN + 5
            return "poked"

        if status == 401 and attempt < MAX_RETRIES:
            site.restore_sign_in()
            continue
        if status == 429:
            # Either this friend's 24h cooldown or a general rate limit; their profile tells which, and how long is left.
            profile_status, profile = site.api("GET", f"/api/profile/~{quoted}")
            viewer = (profile or {}).get("viewerContext") or {}
            if profile_status == 200 and viewer.get("canPoke") is False:
                remaining = viewer.get("pokeCooldownSeconds") or 0
                next_poke_at[friend] = time.time() + (min(remaining, COOLDOWN) + 5 if remaining > 0 else UNKNOWN_COOLDOWN_RETRY)
                return "cooldown"
            if attempt < MAX_RETRIES:
                site.wait(RATE_LIMIT_BACKOFF)
                continue

        log(f"~{friend}: failed with HTTP {status} {json.dumps(body)[:200] if body else ''}")
        next_poke_at[friend] = time.time() + FAILED_RETRY
        return "failed"


def befriend(site, handle, state):
    """After poking someone who isn't a friend: sends them a friend request, or accepts theirs. Returns what happened.

    Each request sent is remembered. If that person later shows as neither a friend nor pending, they declined,
    and they're never asked again."""
    requested = state.setdefault("friend_requested", {})
    declined = state.setdefault("friend_declined", {})
    if handle in declined:
        return "declined a friend request before, not asking again"
    status, profile = site.api("GET", f"/api/profile/~{urllib.parse.quote(handle, safe='')}")
    viewer = (profile or {}).get("viewerContext") or {}
    if status != 200:
        return f"couldn't check friendship (HTTP {status})"
    if viewer.get("areFriends"):
        requested.pop(handle, None)
        return "already friends"
    if viewer.get("pendingRequestTo"):
        return "friend request still pending"
    if viewer.get("pendingRequestFrom"):
        status, body = site.api("POST", "/api/friendship/accept", {"username": handle})
        if 200 <= status < 300:
            return "accepted their friend request"
        return f"couldn't accept their friend request (HTTP {status} {json.dumps(body)[:120] if body else ''})"
    if handle in requested:
        declined[handle] = time.time()
        del requested[handle]
        return "declined our friend request, won't ask again"
    status, body = site.api("POST", "/api/friendship/request", {"username": handle})
    if 200 <= status < 300:
        requested[handle] = time.time()
        return "friend request sent"
    return f"friend request failed (HTTP {status} {json.dumps(body)[:120] if body else ''})"


def poke_next(site, state, dry_run, settings):
    """Pokes one person: the friend whose cooldown ended longest ago, or else someone else who is due
    (or, with friends_first off, anyone due at random). Does nothing once today's poke limit is reached.
    Returns when the poke was sent, or None if nobody was poked."""
    handle = site.require_sign_in()
    if state.get("handle") != handle:
        # First run, or signed in as someone else: start a fresh schedule.
        state.clear()
        state["handle"] = handle
    state.pop("needs_login", None)
    next_poke_at = state.setdefault("next_poke_at", {})

    friends = state.get("friends") or []
    if not friends or time.time() - state.get("friends_fetched_at", 0) >= FRIENDS_REFRESH:
        friends = fetch_friends(site, handle)
        state["friends"], state["friends_fetched_at"] = friends, time.time()
    write_json(STATE_FILE, state)

    counts = today(state)
    poke_limit, request_limit = settings["pokes_per_day"], settings["friend_requests_per_day"]
    if dry_run:
        print(f"Today: {counts['pokes']} of {poke_limit} pokes, {counts['friend_requests']} of {request_limit} friend requests.")
    if counts["pokes"] >= poke_limit:
        if dry_run:
            print("Today's poke limit is reached; poking starts again after midnight.")
        return

    def due(people):
        """Who in people can be poked now, longest-waiting first (ties, like never-poked, in random order)."""
        now = time.time()
        return [h for _, _, h in sorted((next_poke_at.get(h, 0), random.random(), h) for h in people if next_poke_at.get(h, 0) <= now)]

    not_others = set(friends) | {handle}
    others = set(read_json(EVERYONE_FILE).get("known", [])) - not_others
    if not dry_run and len(due(others)) < DISCOVER_BELOW:
        others = discover(site, state) - not_others
    friends_due, others_due = due(friends), due(others)
    queue = friends_due + others_due
    if not settings["friends_first"]:
        random.shuffle(queue)
    if dry_run:
        print(f"~{handle}: {len(friends_due)} of {len(friends)} friends and {len(others_due)} of {len(others)} others due for a poke.")
        if queue:
            print(f"Next poke: ~{queue[0]}")
        return

    for target in queue[:MAX_TRIES_PER_SLOT]:
        poked_at = time.time()
        result = poke(site, target, next_poke_at)
        state["failures_in_a_row"] = state.get("failures_in_a_row", 0) + 1 if result == "failed" else 0
        write_json(STATE_FILE, state)
        if result != "cooldown":
            kind = "friend" if target in friends else "other"
            if result == "poked":
                counts["pokes"] += 1
                if kind == "other" and request_limit > 0:
                    if counts["friend_requests"] >= request_limit:
                        result += "; today's friend request limit is reached"
                    else:
                        outcome = befriend(site, target, state)
                        counts["friend_requests"] += outcome == "friend request sent"
                        result += "; " + outcome
                write_json(STATE_FILE, state)
            log(f"~{target} ({kind}): {result}. Today: {counts['pokes']}/{poke_limit} pokes, "
                f"{counts['friend_requests']}/{request_limit} friend requests. "
                f"Still due: {len(due(friends))} friends, {len(due(others))} others.")
            if counts["pokes"] == poke_limit:
                log(f"Reached today's limit of {poke_limit} pokes; poking starts again after midnight.")
            return poked_at
        site.wait(DELAY)


def feed_handles(event):
    """Everyone an activity-feed event mentions (from/to on pokes, users on friendships, user on joins, ...)."""
    if isinstance(event, dict):
        if isinstance(event.get("userHandle"), str):
            yield event["userHandle"]
        for value in event.values():
            yield from feed_handles(value)
    elif isinstance(event, list):
        for value in event:
            yield from feed_handles(value)


def discover(site, state):
    """Finds more people by reading public friend lists, starting from your friends and anyone the activity feed shows."""
    everyone = read_json(EVERYONE_FILE)
    known = set(everyone.get("known", []))
    crawled = set(everyone.get("crawled", []))
    known |= set(state.get("friends", [])) | set(read_json(FEED_FILE).get("seen", {}))
    known.discard(state["handle"])
    pages = 0
    for handle in sorted(known - crawled, key=lambda h: (h not in state.get("friends", []), random.random())):
        if pages >= CRAWL_PAGES_PER_RUN:
            break
        quoted = urllib.parse.quote(handle, safe="")
        for page in range(1, CRAWL_MAX_PAGES_PER_USER + 1):
            status, body = site.api("GET", f"/api/profile/friends?username={quoted}&page={page}&limit=50")
            pages += 1
            site.wait(DELAY)
            if status != 200:
                break
            known |= {f["user"]["userHandle"] for f in body["friends"]}
            if not body["pagination"]["hasMore"] or pages >= CRAWL_PAGES_PER_RUN:
                break
        crawled.add(handle)
    known.discard(state["handle"])
    write_json(EVERYONE_FILE, {"known": sorted(known), "crawled": sorted(crawled)})
    return known


def watch_feed():
    """Runs forever: notes when each user last showed up in the public activity feed, for runs to view them.

    Sightings older than VIEW_EVERY are dropped, so anyone still listed has shown up since they were last viewed."""
    seen = read_json(FEED_FILE).get("seen", {})
    while True:
        try:
            request = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT, "Accept": "text/event-stream"})
            with urllib.request.urlopen(request, timeout=FEED_IDLE_TIMEOUT) as stream:
                for raw in stream:
                    line = raw.decode("utf-8", "replace")
                    if not line.startswith("data:"):
                        continue
                    now = time.time()
                    seen = {h: t for h, t in seen.items() if now - t < VIEW_EVERY}
                    for handle in feed_handles(json.loads(line[5:])):
                        seen[handle] = now
                    write_json(FEED_FILE, {"seen": seen})
        except socket.timeout:
            pass  # a quiet feed; just reconnect
        except (OSError, ValueError) as e:
            log(f"feed: disconnected ({e}), reconnecting in {FEED_RECONNECT}s")
        time.sleep(FEED_RECONNECT)


def view_profile(site, handle):
    """Opens their profile the way the website does (the profile data, then the page itself). Returns the HTTP status."""
    quoted = urllib.parse.quote(handle, safe="")
    status, _ = site.api("GET", f"/api/profile/~{quoted}")
    if status == 401:
        site.restore_sign_in()
        status, _ = site.api("GET", f"/api/profile/~{quoted}")
    if status == 200:
        site.api("HEAD", f"/~{quoted}")
    return status


def view_feed_users(site, state, dry_run, until=None):
    """Views everyone the feed has shown since their last view, newest first; you and your friends are skipped.
    Stops at until (a time), so views fill the wait before the next poke without delaying it."""
    seen = read_json(FEED_FILE).get("seen", {})
    viewed_at = state.setdefault("viewed_at", {})
    now = time.time()
    for handle in [h for h, t in viewed_at.items() if now - t >= VIEW_EVERY]:
        del viewed_at[handle]
    skip = set(state["friends"]) | {state["handle"]}
    todo = [h for h, _ in sorted(seen.items(), key=lambda kv: -kv[1]) if h not in skip and h not in viewed_at]
    if not todo:
        return
    if dry_run:
        print("Would view: " + ", ".join(f"~{h}" for h in todo))
        return

    viewed = 0
    for handle in todo:
        if until is not None and time.time() >= until:
            break
        status = view_profile(site, handle)
        if status in (200, 404):  # a 404 is a deleted account: nothing to retry
            viewed_at[handle] = time.time()
            viewed += status == 200
        elif status == 429:
            log("Rate limited while viewing profiles; the rest wait for the next run.")
            break
        else:
            log(f"~{handle}: profile view failed with HTTP {status}")
        write_json(STATE_FILE, state)
        site.wait(DELAY)
    if viewed:
        log(f"Profile views from the activity feed: {viewed}.")


def next_gap(state, settings):
    """Seconds from one poke to the next: a random wait from settings.ini, or a rest after failures or the daily limit."""
    if state.get("failures_in_a_row", 0) >= STOP_AFTER_FAILURES:
        log(f"{state['failures_in_a_row']} failed pokes in a row; resting {FAILURE_REST // 60} minutes.")
        return FAILURE_REST
    if today(state)["pokes"] >= settings["pokes_per_day"]:
        return LIMIT_REST
    return random.uniform(*settings["gap"])


def schedule_next(state, settings, poked_at):
    state["next_slot_at"] = (poked_at or time.time()) + next_gap(state, settings)
    write_json(STATE_FILE, state)


def pause_for_login(error):
    """Pauses the bot until --login clears it, with a single notification."""
    state = read_json(STATE_FILE)
    if not state.get("needs_login"):
        state["needs_login"] = True
        write_json(STATE_FILE, state)
        notify(f"Paused: {error}. Run: python3 poke_browser.py --login")
    log(f"Paused: {error}. Run: python3 poke_browser.py --login")


def run_once(args):
    """python3 poke_browser.py [--dry-run]: one poke (or a preview) right now."""
    if not COOKIES_FILE.exists():
        sys.exit("Not signed in yet. Run: python3 poke_browser.py --login")
    state, settings = read_json(STATE_FILE), load_settings()
    with sync_playwright() as playwright:
        site = Site(playwright, headless=True)
        try:
            poked_at = poke_next(site, state, args.dry_run, settings)
            if not args.dry_run:
                schedule_next(state, settings, poked_at)
            view_feed_users(site, state, args.dry_run)
            site.save_cookies()
        finally:
            site.close()


def chrome_session(state, settings):
    """Keeps Chrome open and pokes each time a slot comes up, viewing profiles in between. Returns once the next poke
    is far off, or (so Chrome gets restarted now and then) right after a poke once it has been open CHROME_SESSION."""
    with sync_playwright() as playwright:
        site = Site(playwright, headless=True)
        try:
            opened = time.time()
            while True:
                wait = state.get("next_slot_at", 0) - time.time()
                if wait > CLOSE_CHROME_ABOVE:
                    break
                if wait > 0:
                    site.wait(wait)
                schedule_next(state, settings, poke_next(site, state, False, settings))
                site.save_cookies()
                if time.time() - opened >= CHROME_SESSION:
                    break  # restart Chrome now, while the next poke is a whole wait away
                view_feed_users(site, state, False, until=state["next_slot_at"] - 5)
        finally:
            site.close()


def run_forever():
    """--scheduled: launchd starts this and keeps it running. It times each poke itself, opening Chrome shortly before
    a poke is due and keeping it open while the next one is near. Waits quietly while a new sign-in is needed."""
    while True:
        state = read_json(STATE_FILE)
        if state.get("needs_login") or not COOKIES_FILE.exists():
            time.sleep(60)
            continue
        wait = state.get("next_slot_at", 0) - time.time()
        if wait > OPEN_CHROME_AHEAD:
            # Short naps, so a Mac waking from sleep catches up straight away.
            time.sleep(min(wait - OPEN_CHROME_AHEAD, 60))
            continue
        try:
            chrome_session(state, load_settings())
        except NeedsLogin as e:
            pause_for_login(e)
        except (RuntimeError, OSError, PlaywrightError) as e:
            log(f"Error, retrying in a minute: {e}")
            state["next_slot_at"] = time.time() + ERROR_RETRY + OPEN_CHROME_AHEAD  # Chrome opens OPEN_CHROME_AHEAD early
            write_json(STATE_FILE, state)


def main():
    parser = argparse.ArgumentParser(description="Poke one RemiliaNET user every few minutes (friends first, as set in settings.ini), through a signed-in headless Chrome.")
    parser.add_argument("--login", action="store_true", help='sign in in a Chrome window (tick "Remember me")')
    parser.add_argument("--dry-run", action="store_true", help="show who is due a poke or a view, without doing either")
    parser.add_argument("--scheduled", action="store_true", help="set by launchd: runs forever, poking on schedule")
    parser.add_argument("--watch-feed", action="store_true", help="record everyone in the activity feed; runs forever (launchd keeps it up)")
    args = parser.parse_args()

    if args.watch_feed:
        watch_feed()
    if args.scheduled:
        run_forever()
    try:
        if args.login:
            login()
        else:
            run_once(args)
    except NeedsLogin as e:
        pause_for_login(e)
        sys.exit(1)
    except (RuntimeError, OSError, PlaywrightError) as e:
        log(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
