#!/usr/bin/env python3
"""Auto-refresh the Skool MCP cookie.

auth_token is valid ~1 year; only aws-waf-token (~4 days) expires. This reuses the
year-long auth_token in a headless browser to silently re-mint the WAF token, so
you don't have to re-run connect_skool.py every few days.

Installed as a background job by install_refresh_mac.sh / install_refresh_windows.ps1.
Run it by hand any time; `--dry-run` proves it works without touching .env.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(DIR, ".env")


def _log_path() -> str:
    """Where the refresh log lives, per OS.

    macOS:   ~/Library/Logs — deliberately OUTSIDE ~/Documents, which macOS TCC
             protects. launchd has no Full Disk Access and aborts the job
             (EX_CONFIG / exit 78) if it cannot open the log itself.
    Windows: %LOCALAPPDATA%\\skool-mcp — the Task Scheduler job's log home.
    other:   ~/.local/state/skool-mcp — XDG state directory.
    """
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/skool-cookie-refresh.log")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "skool-mcp", "refresh.log")
    return os.path.expanduser("~/.local/state/skool-mcp/refresh.log")


LOG = _log_path()
# Ephemeral cookies the browser re-mints — never carry these forward by hand.
EPHEMERAL = {"aws-waf-token", "AWSALB", "AWSALBCORS", "AWSALBTG", "AWSALBTGCORS"}


def parse_cookie_header(header: str) -> dict:
    out = {}
    for part in header.split("; "):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")  # split on FIRST '=' only
        out[name] = value
    return out


def rebuild_cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def auth_token_days_left(cookies: dict):
    """Days until the auth_token JWT expires, or None if not a decodable JWT."""
    token = cookies.get("auth_token", "")
    if token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    exp = claims.get("exp")
    if not exp:
        return None
    return (exp - time.time()) / 86400


def replace_cookie_line(text: str, new_header: str) -> str:
    lines = text.splitlines(keepends=True)
    out, found = [], False
    for line in lines:
        if line.startswith("SKOOL_COOKIE="):
            nl = "\n" if line.endswith("\n") else ""
            out.append(f"SKOOL_COOKIE={new_header}{nl}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(f"SKOOL_COOKIE={new_header}\n")
    return "".join(out)


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# Shape-based redaction, mirroring scrub() in doctor.py. The log is the one place
# in the bundle where text reaches durable storage, and `fail(repr(e))` can carry a
# cookie out of an httpx or Playwright exception. Members share this log when asking
# for help, so scrub at the sink: every line, no matter who wrote it.
_STRUCTURAL = [
    (re.compile(r"eyJ[A-Za-z0-9_\-]{6,}"), "<redacted>"),                    # JWT
    (re.compile(r"((?:auth_token|aws-waf-token|client_id)\s*(?:=|:|%3[Dd])\s*)"
                r"[^;,\s\"'&]+"), r"\1<redacted>"),                          # name=value
]


def scrub(text: str) -> str:
    for pattern, replacement in _STRUCTURAL:
        text = pattern.sub(replacement, text)
    return text


def log(msg: str):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} | {scrub(str(msg))}\n"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="")


def notify(message: str):
    """Best-effort alert to the member. Never raises — a failed notification must
    never mask the real error it was announcing."""
    try:
        if sys.platform == "darwin":
            # Keep messages quote-free; the osascript string is double-quoted.
            safe = message.replace('"', "'")
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe}" with title "Skool MCP"'],
                check=False, timeout=10)
        elif os.name == "nt":
            # Windows: no toast dependency — the scheduled task's log is the channel.
            log(f"WARN: {message}")
        else:
            # Everything else: log-only.
            log(f"WARN: {message}")
    except Exception:
        pass


def fail(reason: str):
    notify(f"Skool cookie refresh failed: {reason}. Run connect_skool.py.")
    log(f"FAIL: {reason}")
    sys.exit(1)


def write_env_atomic(new_header: str):
    with open(ENV, "r", encoding="utf-8") as f:
        text = f.read()
    new_text = replace_cookie_line(text, new_header)
    tmp = ENV + ".tmp"
    # Mirrors write_env_atomic() in connect_skool.py — keep the two in step.
    # O_CREAT|O_EXCL at 0600 keeps the live cookie off disk at a laxer mode even
    # momentarily and blocks a planted symlink. os.replace then carries 0600 onto
    # .env, so an automatic refresh can never silently widen the permissions that
    # connect_skool.py set (it used to drop them to 0644, permanently).
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
            f.write(new_text)
        os.replace(tmp, ENV)  # atomic on POSIX — a crash never leaves a half-written .env
    except BaseException:      # BaseException so Ctrl-C can't strand the temp file
        if fd is not None:
            os.close(fd)
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)  # never leave a credential-bearing temp file behind
            except OSError:
                pass
        raise


def _group() -> str:
    g = os.environ.get("SKOOL_GROUP", "").strip()
    if not g:
        fail("SKOOL_GROUP not set in .env")
    return g


def mint_fresh_cookies(seed_header: str, group: str) -> tuple[dict, str]:
    """Headless-browse skool.com with the long-lived cookies so AWS WAF mints a
    fresh aws-waf-token. Returns (cookies_after: dict, html: str)."""
    from playwright.sync_api import sync_playwright
    existing = parse_cookie_header(seed_header)
    seed = [{"name": n, "value": v, "url": "https://www.skool.com"}
            for n, v in existing.items() if n not in EPHEMERAL]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=UA)
            ctx.add_cookies(seed)
            page = ctx.new_page()
            page.goto(f"https://www.skool.com/{group}", wait_until="domcontentloaded")
            for _ in range(10):  # up to ~20s for WAF JS to mint the token
                time.sleep(2)
                if any(c["name"] == "aws-waf-token" for c in ctx.cookies()):
                    break
            html = page.content()
            after = {c["name"]: c["value"] for c in ctx.cookies() if "skool.com" in c["domain"]}
        finally:
            browser.close()  # always close, even if navigation throws — no orphaned Chromium
    return after, html


def verify(new_header: str):
    """Prove the rebuilt header actually authenticates before we persist it."""
    import skool_client
    os.environ["SKOOL_COOKIE"] = new_header  # skool_client reads env live; not restored (process exits after)
    skool_client.whoami()  # raises AuthError/RuntimeError on a bad cookie


def refresh(dry_run: bool = False):
    from dotenv import load_dotenv
    load_dotenv(ENV)
    seed_header = os.environ.get("SKOOL_COOKIE", "").strip()
    if not seed_header:
        fail("SKOOL_COOKIE not set in .env")

    before = parse_cookie_header(seed_header)
    days = auth_token_days_left(before)
    if days is not None and days < 14:
        notify(f"Skool login expires in ~{int(days)} days. Re-run connect_skool.py soon.")
        log(f"WARN: auth_token expires in {days:.1f} days")

    group = _group()  # validate SKOOL_GROUP before opening a browser
    after, html = mint_fresh_cookies(seed_header, group)
    if "aws-waf-token" not in after:
        fail("AWS WAF token was not minted (headless may be blocked)")
    if '"postTrees"' not in html:
        fail("page did not load logged-in (auth_token likely expired)")

    merged = {**before, **after}  # refreshed values win; long-lived ones preserved
    new_header = rebuild_cookie_header(merged)
    verify(new_header)

    if dry_run:
        log(f"DRY-RUN ok: would write {len(new_header)} char header "
            f"(aws-waf-token len {len(after.get('aws-waf-token',''))})")
        return
    write_env_atomic(new_header)
    log("OK: cookie refreshed")


if __name__ == "__main__":
    try:
        refresh(dry_run="--dry-run" in sys.argv)
    except SystemExit:
        raise
    except Exception as e:
        fail(repr(e))
