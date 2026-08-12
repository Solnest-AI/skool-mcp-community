#!/usr/bin/env python3
"""Connect this MCP server to YOUR Skool community.

    uv run connect_skool.py                    # interactive picker
    uv run connect_skool.py --group my-slug    # skip the picker

Opens a real browser window. You log into Skool in it yourself — Google SSO and
2FA work because it is a real, visible window. When you are in, this harvests the
session cookie straight out of the browser and writes it into `.env`.

SECURITY CONTRACT — this script exists so the cookie never has to be handled by
a human or an agent:
  * The cookie value is NEVER printed, logged, or returned. Lengths only, and
    error messages carry the exception TYPE, never its text.
  * The only file this script writes is `.env`, created owner-only (0600) and
    swapped in atomically. No skool-cookies.json, no HAR, no capture file —
    that dump was the leak in the old capture script. (Playwright keeps its own
    throwaway browser profile while the window is open; it is deleted when the
    browser closes, including on Ctrl-C.)
  * Keys already in `.env` that are not SKOOL_COOKIE / SKOOL_GROUP are preserved.

Exit 0 on success, 1 with an actionable message on any failure.
"""
import argparse
import os
import sys
import time
from typing import NoReturn

# The cookie helpers are shared with the auto-refresh job so both agree on the
# exact header format Skool accepts.
from refresh_cookie import parse_cookie_header, rebuild_cookie_header

DIR = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(DIR, ".env")

LOGIN_URL = "https://www.skool.com/login"
GROUPS_URL = "https://api2.skool.com/self/groups"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

LOGIN_TIMEOUT = 600  # 10 min — SSO, 2FA and password managers are slow
POLL_SECONDS = 2

SUCCESS = "✅ Connected to {slug}. Wrote .env (cookie hidden)."


# --- output -------------------------------------------------------------------
def die(reason: str, hint: str = "") -> NoReturn:
    """Fail with an actionable message and exit 1.

    Call sites pass exception TYPES, never exception text: a third-party message
    could quote the request headers, and those carry the cookie.
    """
    print(f"\nFAILED: {reason}", file=sys.stderr)
    if hint:
        print(f"  -> {hint}", file=sys.stderr)
    sys.exit(1)


def summarize_capture(cookie_header: str) -> str:
    """A safe one-liner about what we grabbed. Counts and lengths, never values."""
    jar = parse_cookie_header(cookie_header)
    return (f"Captured {len(jar)} cookies "
            f"({len(cookie_header)} chars total, values hidden).")


def report_success(slug: str) -> str:
    return SUCCESS.format(slug=slug)


def humanize_seconds(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{round(seconds / 60)} minutes"


# --- .env reading / writing -------------------------------------------------------
def read_env_var(env_path: str, key: str) -> str:
    """One value out of .env, or "" if absent/unreadable. Splits on the FIRST '='
    because cookie values are base64 and full of '='."""
    prefix = f"{key}="
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("export "):
                    stripped = stripped[len("export "):].strip()
                if stripped.startswith(prefix):
                    value = stripped[len(prefix):].strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return value
    except OSError:
        pass
    return ""


def upsert_env_var(text: str, key: str, value: str) -> str:
    """Set KEY=value in .env text, leaving every other line alone.

    Generalization of refresh_cookie.replace_cookie_line — the tests assert the
    two behave identically for SKOOL_COOKIE so the refresh job and this script
    can never disagree about .env's shape.
    """
    prefix = f"{key}="
    out, found = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith(prefix):
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"{prefix}{value}{nl}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"{prefix}{value}\n")
    return "".join(out)


def write_env_atomic(env_path: str, updates: dict) -> None:
    """Merge `updates` into .env and swap the file in atomically.

    Temp file + os.replace, the pattern refresh_cookie.py uses, with three extra
    guarantees because this file holds an account-takeover token:
      * O_CREAT|O_EXCL with mode 0600 — the cookie is never on disk at a laxer
        mode even momentarily, and an attacker cannot pre-plant the temp path as
        a symlink to redirect it.
      * BaseException cleanup — Ctrl-C during a write must not strand a readable
        temp file holding the cookie.
      * os.replace — a crash can never leave a half-written .env.
    """
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""

    for key, value in updates.items():
        text = upsert_env_var(text, key, value)

    tmp = env_path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = None
    try:
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:
            if os.path.islink(tmp):
                raise RuntimeError(
                    f"{tmp} is a symlink. Refusing to write the cookie through it.")
            os.unlink(tmp)  # leftover from a killed run
            fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            fd = None  # the file object owns the descriptor now
            f.write(text)
        os.replace(tmp, env_path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)  # never leave a credential-bearing temp file behind
            except OSError:
                pass
        raise


def plan_env_updates(env_path: str, slug: str, group_id: str, cookie_header: str) -> dict:
    """Which keys this run will write.

    Always SKOOL_COOKIE + SKOOL_GROUP. SKOOL_GROUP_ID is a derived cache of
    SKOOL_GROUP, not an unrelated user key, so it is corrected when it already
    holds an id for a DIFFERENT community — leaving it stale would silently point
    every MCP tool at the old group. When the new id is unknown the line is
    blanked rather than left wrong; the client then resolves it from the slug.
    The key is never created if it was not already there.
    """
    updates = {"SKOOL_COOKIE": cookie_header, "SKOOL_GROUP": slug}
    existing = read_env_var(env_path, "SKOOL_GROUP_ID")
    if existing and existing != group_id:
        updates["SKOOL_GROUP_ID"] = group_id  # "" when unknown -> resolve at runtime
    return updates


# --- browser leg ----------------------------------------------------------------
def is_skool_domain(domain: str) -> bool:
    """Suffix match, not substring: 'notskool.com' and 'skool.com.evil.net' are
    not Skool, and this is the one function whose job is scoping a credential."""
    d = (domain or "").lstrip(".").lower()
    return d == "skool.com" or d.endswith(".skool.com")


def cookie_header_from(cookies: list) -> str:
    """Build the Cookie header from Playwright cookie dicts (skool.com only)."""
    jar = {}
    for c in cookies:
        if not is_skool_domain(c.get("domain")):
            continue
        name = c.get("name")
        if name:
            jar[name] = c.get("value", "")
    return rebuild_cookie_header(jar)


def is_logged_in(cookies: list, urls) -> bool:
    """Logged in == an auth_token cookie exists AND we are off the login page.

    `urls` is every open page. Skool only sets auth_token after a successful
    login, so if no page URL is readable (the member closed the tab once their
    feed loaded) the cookie alone is enough — stalling until timeout would throw
    away a login that already worked.
    """
    if not any(c.get("name") == "auth_token" and c.get("value") for c in cookies):
        return False
    if isinstance(urls, str):  # a bare string would iterate as characters
        urls = [urls]
    urls = [u for u in (urls or []) if u]
    if not urls:
        return True
    return any("/login" not in u for u in urls)


def _launch(pw):
    """Headed browser. Headless is not an option: members log in with Google SSO
    and 2FA. Prefer the system Chrome channel (Google is far less likely to block
    it as an insecure browser), fall back to Playwright's bundled Chromium."""
    try:
        return pw.chromium.launch(headless=False, channel="chrome",
                                  args=["--start-maximized"])
    except Exception:
        return pw.chromium.launch(headless=False, args=["--start-maximized"])


def open_page_urls(ctx) -> list:
    try:
        return [p.url for p in ctx.pages if not p.is_closed()]
    except Exception:
        return []


def wait_for_login(timeout: int = LOGIN_TIMEOUT) -> str:
    """Open Skool's login page and wait for the member. Returns the Cookie header."""
    try:
        from playwright.sync_api import sync_playwright, Error as PWError
    except ImportError:
        die("Playwright is not installed.",
            "Run `uv sync` then `uv run playwright install chromium`.")

    print("\n" + "=" * 68)
    print(" A browser window is opening on Skool's login page.")
    print(" 1. Log in there (Google, email, whatever you normally use).")
    print(" 2. Wait until your community feed loads.")
    print(" 3. Leave the window open — this closes it for you.")
    print(" Nothing you type in that window is visible to this script or to Claude.")
    print("=" * 68 + "\n")

    header = ""
    with sync_playwright() as pw:
        try:
            browser = _launch(pw)
        except Exception as e:
            die(f"Could not start a browser ({type(e).__name__}).",
                "Run `uv run playwright install chromium` and try again.")
        try:
            try:
                ctx = browser.new_context(user_agent=UA, no_viewport=True)
                page = ctx.new_page()
                page.goto(LOGIN_URL, wait_until="domcontentloaded")
            except Exception as e:
                die(f"Could not open Skool's login page ({type(e).__name__}).",
                    "Check your internet connection, then re-run.")

            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(POLL_SECONDS)
                if not browser.is_connected():
                    die("The browser window was closed before login finished.",
                        "Re-run `uv run connect_skool.py` and leave it open.")
                try:
                    cookies = ctx.cookies()
                except PWError:
                    die("The browser window was closed before login finished.",
                        "Re-run `uv run connect_skool.py` and leave it open.")
                if is_logged_in(cookies, open_page_urls(ctx)):
                    header = cookie_header_from(cookies)
                    break
        finally:
            try:
                browser.close()  # never orphan a Chromium process or its profile
            except Exception:
                pass

    if not header:
        die(f"Timed out after {humanize_seconds(timeout)} waiting for a Skool login.",
            "Re-run `uv run connect_skool.py`, or pass --timeout <seconds>.")
    if "auth_token" not in parse_cookie_header(header):
        die("Logged in, but no auth_token cookie was found.",
            "Log in at skool.com in your normal browser first, then re-run this.")
    return header


# --- group picker -----------------------------------------------------------------
def parse_groups_payload(payload) -> list:
    """Skool's /self/groups body -> [{slug, id, label}, ...]."""
    groups = []
    for g in (payload or {}).get("groups") or []:
        slug = g.get("name")
        if not slug:
            continue
        md = g.get("metadata") or {}
        groups.append({"slug": slug, "id": g.get("id") or "",
                       "label": md.get("display_name") or ""})
    return groups


def fetch_groups(cookie_header: str, fatal: bool = True):
    """The communities this login belongs to.

    fatal=False returns None instead of exiting, so a saved cookie can be probed
    without committing to it.
    """
    def bail(reason, hint):
        if fatal:
            die(reason, hint)
        return None

    try:
        import httpx
    except ImportError:
        return bail("Missing httpx.",
                    "Run `uv sync` (or `pip install -r requirements.txt`).")
    try:
        with httpx.Client(timeout=40) as c:
            r = c.get(GROUPS_URL, params={"limit": 50, "prefs": "false"},
                      headers={"Cookie": cookie_header, "accept": "application/json",
                               "user-agent": UA})
    except Exception as e:
        return bail(f"Could not reach api2.skool.com ({type(e).__name__}).",
                    "Check your internet connection and re-run.")
    if r.status_code in (401, 403):
        return bail(f"Skool rejected the session (HTTP {r.status_code}).",
                    "Make sure you were fully logged in before the window closed, "
                    "then re-run.")
    if r.status_code != 200:
        return bail(f"Skool returned HTTP {r.status_code} listing your communities.",
                    "Wait a minute and re-run.")
    try:
        payload = r.json()
    except Exception:
        return bail("Skool's community list was not JSON (they may have changed the API).",
                    "Re-run; if it persists this bundle needs an update.")
    return parse_groups_payload(payload)


def choose_group(groups: list, preselected: str = "") -> dict:
    if not groups:
        die("This Skool account is not a member of any community.",
            "Join or create one at skool.com, then re-run.")

    if preselected:
        for g in groups:
            if g["slug"] == preselected:
                return g
        die(f"'{preselected}' is not one of your Skool communities.",
            "Yours: " + ", ".join(g["slug"] for g in groups))

    if len(groups) == 1:
        print(f"One community on this account: {groups[0]['slug']}")
        return groups[0]

    print("\nYour Skool communities:")
    for i, g in enumerate(groups, 1):
        label = f"   ({g['label']})" if g["label"] else ""
        print(f"  [{i}] {g['slug']}{label}")

    if not sys.stdin.isatty():
        die("No terminal available to read your choice.",
            f"Re-run with --group <slug>, e.g. `uv run connect_skool.py "
            f"--group {groups[0]['slug']}`. Your login is already saved, so it "
            "will not ask you to log in again.")

    while True:
        try:
            raw = input("\nConnect this MCP to which one? [number] ").strip()
        except (EOFError, KeyboardInterrupt):
            die("Cancelled.", "Re-run with --group <slug> to skip the picker.")
        if raw.isdigit() and 1 <= int(raw) <= len(groups):
            return groups[int(raw) - 1]
        print(f"  Enter a number between 1 and {len(groups)}.")


# --- verification -------------------------------------------------------------------
def check_membership(info: dict, slug: str) -> str:
    """"" if whoami's answer is consistent with `slug`, else why it is not.

    The membership test uses the server-returned group list. `active_group` alone
    is just the env var we set a moment ago, so on its own it proves nothing.
    """
    known = {g.get("slug") for g in (info.get("groups") or []) if isinstance(g, dict)}
    if known and slug not in known:
        return f"this Skool account is not a member of '{slug}'"
    if info.get("active_group") != slug:
        return f"the client resolved '{info.get('active_group')}' instead of '{slug}'"
    if not info.get("active_group_id"):
        return f"no group id could be resolved for '{slug}'"
    return ""


def verify(slug: str, cookie_header: str) -> None:
    """Prove the written credentials actually drive the client before claiming success."""
    os.environ["SKOOL_COOKIE"] = cookie_header
    os.environ["SKOOL_GROUP"] = slug
    # .env has already been corrected, so whatever SKOOL_GROUP_ID the client picks
    # up from it (via load_dotenv on import) is either right or blank.
    os.environ.pop("SKOOL_GROUP_ID", None)
    try:
        import skool_client
    except ImportError as e:
        die(f"Could not import skool_client ({type(e).__name__}).",
            "Run `uv sync` and re-run.")
    try:
        info = skool_client.whoami()
    except Exception as e:
        die(f"Wrote .env, but the credentials did not authenticate ({type(e).__name__}).",
            "Re-run `uv run connect_skool.py`, or `uv run doctor.py` for detail.")
    problem = check_membership(info or {}, slug)
    if problem:
        die(f"Wrote .env, but {problem}.",
            "Re-run `uv run connect_skool.py` and pick again.")


# --- main --------------------------------------------------------------------------
def save(updates: dict) -> None:
    try:
        write_env_atomic(ENV, updates)
    except Exception as e:
        die(f"Could not write {ENV} ({type(e).__name__}).",
            "Check the folder is writable, then re-run.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Log into Skool in a browser and write the session into .env.")
    ap.add_argument("--group", default="", metavar="SLUG",
                    help="community slug to connect to (skips the picker)")
    ap.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT, metavar="SECONDS",
                    help=f"how long to wait for login (default {LOGIN_TIMEOUT})")
    args = ap.parse_args(argv)
    if args.timeout < 1:
        die("--timeout must be at least 1 second.", f"Try --timeout {LOGIN_TIMEOUT}.")
    wanted = args.group.strip()

    cookie_header, groups = "", None
    if wanted:
        # Finish a run whose picker could not prompt, without a second login.
        cached = read_env_var(ENV, "SKOOL_COOKIE")
        if cached:
            groups = fetch_groups(cached, fatal=False)
            if groups:
                cookie_header = cached
                print("Reusing the Skool session already in .env (no login needed).")
    if groups is None:
        cookie_header = wait_for_login(args.timeout)
        print(summarize_capture(cookie_header))
        groups = fetch_groups(cookie_header)

    # Persist the cookie BEFORE the picker. It is valid for every community, so a
    # picker that cannot prompt must never throw away a login the member just did.
    save({"SKOOL_COOKIE": cookie_header})
    chosen = choose_group(groups, wanted)
    save(plan_env_updates(ENV, chosen["slug"], chosen["id"], cookie_header))

    verify(chosen["slug"], cookie_header)

    line = report_success(chosen["slug"])
    try:
        print(line)
    except UnicodeEncodeError:  # legacy Windows console codepage
        print(line.encode("ascii", "ignore").decode().strip())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:  # never dump a raw traceback at a member
        die(f"Unexpected error ({type(exc).__name__}).",
            "Run `uv run doctor.py` for a full diagnosis.")
