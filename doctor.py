#!/usr/bin/env python3
"""Health check for the Skool MCP install.

    uv run doctor.py           # human-readable
    uv run doctor.py --json    # machine-readable, for an agent to parse
    uv run doctor.py --no-live # skip the network check

Ten checks, each PASS / WARN / FAIL / INFO / SKIP with a fix hint. Exit 0 when
nothing FAILed, 1 otherwise.

SECURITY CONTRACT: this prints cookie LENGTHS and JWT EXPIRY only, never a cookie
value. Every rendered string is additionally run through a scrubber that replaces
any known secret substring, so even an exception message that happens to contain
the cookie cannot leak it. There is a unit test for exactly that.

Deliberately stdlib-only at import time: doctor.py has to run and give a useful
answer on a half-installed machine, which is when it matters most. Third-party
imports happen inside the individual checks.
"""
import argparse
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, asdict

DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(DIR, ".env")

PASS, WARN, FAIL, INFO, SKIP = "PASS", "WARN", "FAIL", "INFO", "SKIP"
MIN_PYTHON = (3, 10)
EXPIRY_WARN_DAYS = 14
REQUIRED_IMPORTS = ("mcp", "httpx", "dotenv", "playwright")
LIVE_CHECK = "Live auth"

# Must stay byte-identical to the labels registered by install_refresh_mac.sh
# (launchd Label) and install_refresh_windows.ps1 (-TaskName). tests/test_doctor.py
# asserts these against the installer files so the two can't drift apart again.
_JOB_LABELS = {
    "Darwin": "com.skoolmcp.cookie-refresh",
    "Windows": "SkoolMCP-CookieRefresh",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


# --- .env reading (stdlib; python-dotenv may not be installed yet) -------------
def read_env_file(path: str) -> dict:
    """Parse a .env into a dict. Splits on the FIRST '=' only — cookie values are
    base64 and full of '='. Returns {} if the file is missing."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _parse_cookie_header(header: str) -> dict:
    """refresh_cookie.parse_cookie_header, with a stdlib fallback so doctor still
    works when the repo is only partially in place."""
    try:
        from refresh_cookie import parse_cookie_header
        return parse_cookie_header(header)
    except Exception:
        jar = {}
        for part in header.split(";"):
            part = part.strip()
            if part:
                name, _, value = part.partition("=")
                jar[name.strip()] = value
        return jar


def _auth_token_days_left(cookies: dict):
    """refresh_cookie.auth_token_days_left — shared so doctor and the refresh job
    can never disagree about when a login expires."""
    try:
        from refresh_cookie import auth_token_days_left
    except Exception:
        return None
    return auth_token_days_left(cookies)


# --- redaction -----------------------------------------------------------------
def secrets_from(env: dict) -> list:
    """Every string that must never appear in output: the whole cookie header and
    each individual cookie value."""
    header = (env or {}).get("SKOOL_COOKIE", "") or ""
    secrets = [header]
    for value in _parse_cookie_header(header).values():
        secrets.append(value)
        # JWTs are dotted; a leaked payload segment is still a leak.
        secrets.extend(value.split("."))
    return sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)


# Shape-based redaction, applied on top of the known-value pass. Exact-substring
# matching alone is defeated by any transformation of the value: percent-encoding
# an aws-waf-token splits it into fragments that match nothing. These patterns
# catch a credential we have never seen, in whatever form it arrives.
_STRUCTURAL = [
    (re.compile(r"eyJ[A-Za-z0-9_\-]{6,}"), "<redacted>"),                    # JWT
    (re.compile(r"((?:auth_token|aws-waf-token|client_id)\s*(?:=|:|%3[Dd])\s*)"
                r"[^;,\s\"'&]+"), r"\1<redacted>"),                          # name=value
]


def scrub(text: str, secrets: list) -> str:
    for s in secrets:
        if s in text:
            text = text.replace(s, "<redacted>")
    for pattern, replacement in _STRUCTURAL:
        text = pattern.sub(replacement, text)
    return text


# --- the checks -----------------------------------------------------------------
def check_python(version=None) -> Check:
    v = tuple(version or sys.version_info[:3])
    shown = ".".join(str(p) for p in v)
    if v[:2] >= MIN_PYTHON:
        return Check("Python version", PASS, f"Python {shown}")
    return Check("Python version", FAIL, f"Python {shown}",
                 f"Need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+. `uv run` picks a good one for you.")


def check_imports(finder=None) -> Check:
    import importlib.util
    finder = finder or importlib.util.find_spec
    missing = []
    for mod in REQUIRED_IMPORTS:
        try:
            if finder(mod) is None:
                missing.append(mod)
        except Exception:
            missing.append(mod)
    if not missing:
        return Check("Dependencies", PASS, ", ".join(REQUIRED_IMPORTS) + " all import")
    return Check("Dependencies", FAIL, "missing: " + ", ".join(missing),
                 "Run `uv sync` (or `pip install -r requirements.txt`).")


def check_mcp_server_api(importer=None) -> Check:
    """Can server.py actually build an MCP server?

    `import mcp` succeeding is not enough. SDK 2.0 renamed FastMCP to MCPServer and
    deleted the old module path, so a machine can have a perfectly importable `mcp`
    and still fail to start the server. Checking only the top-level package reported
    a PASS on exactly that machine, which sends the member off debugging Claude's
    config when the real fault is here. Probe the same two paths server.py does.
    """
    import importlib
    importer = importer or importlib.import_module
    for path, symbol in (("mcp.server.mcpserver", "MCPServer"),
                         ("mcp.server.fastmcp", "FastMCP")):
        try:
            if hasattr(importer(path), symbol):
                return Check("MCP server API", PASS, f"{path}.{symbol}")
        except Exception:
            continue
    return Check("MCP server API", FAIL, "no usable MCP server class",
                 "Your `mcp` package is too old or too new. Run `uv sync` to install "
                 "the pinned version.")


_CHROMIUM_PROBE = ("from playwright.sync_api import sync_playwright\n"
                   "with sync_playwright() as p:\n"
                   "    print(p.chromium.executable_path)\n")


def check_chromium(runner=None) -> Check:
    name = "Playwright Chromium"
    hint = "Run `uv run playwright install chromium`."
    import importlib.util
    try:
        if importlib.util.find_spec("playwright") is None:
            return Check(name, FAIL, "playwright is not installed", "Run `uv sync` first.")
    except Exception:
        return Check(name, FAIL, "playwright is not installed", "Run `uv sync` first.")

    # Out of process on purpose. Querying Playwright starts its Node driver, and
    # tearing that down mid-process dumps asyncio "Task was destroyed" noise that
    # reads like a crash to a member running doctor for the first time.
    runner = runner or subprocess.run
    try:
        proc = runner([sys.executable, "-c", _CHROMIUM_PROBE],
                      capture_output=True, text=True, timeout=90, env=safe_env())
    except Exception as e:
        return Check(name, FAIL, f"could not query Playwright ({type(e).__name__})", hint)
    lines = (getattr(proc, "stdout", "") or "").strip().splitlines()
    path = lines[-1].strip() if lines else ""
    if path and os.path.exists(path):
        return Check(name, PASS, "browser installed")
    # FAIL, not WARN: connect_skool.py can fall back to system Chrome, but the
    # auto-refresh job launches the bundled binary and will silently stop working.
    return Check(name, FAIL, "browser binary not downloaded (auto-refresh needs it)",
                 hint)


def check_env_file(env_path: str) -> Check:
    if os.path.isfile(env_path):
        return Check(".env file", PASS, f"found at {env_path}")
    return Check(".env file", FAIL, f"not found at {env_path}",
                 "Run `uv run connect_skool.py` to create it. Never write it by hand.")


def check_cookie(env: dict) -> Check:
    name = "SKOOL_COOKIE"
    header = (env or {}).get("SKOOL_COOKIE", "").strip()
    if not header:
        return Check(name, FAIL, "not set", "Run `uv run connect_skool.py`.")
    jar = _parse_cookie_header(header)
    if not jar or not any(jar.values()):
        return Check(name, FAIL, f"set ({len(header)} chars) but does not parse as a "
                     "cookie header", "Run `uv run connect_skool.py` to rewrite it.")
    return Check(name, PASS, f"{len(jar)} cookies, {len(header)} chars")


def check_auth_token(cookies: dict) -> Check:
    name = "Skool login (auth_token)"
    token = (cookies or {}).get("auth_token", "")
    if not token:
        return Check(name, FAIL, "no auth_token cookie", "Run `uv run connect_skool.py`.")
    days = _auth_token_days_left(cookies)
    if days is None:
        return Check(name, WARN, f"present ({len(token)} chars) but expiry is unreadable",
                     "Usually harmless. `uv run doctor.py` again after a refresh.")
    if days <= 0:
        return Check(name, FAIL, f"expired {abs(days):.1f} days ago",
                     "Run `uv run connect_skool.py` to log in again.")
    if days < EXPIRY_WARN_DAYS:
        return Check(name, WARN, f"expires in {days:.1f} days",
                     "Run `uv run connect_skool.py` again before it lapses.")
    return Check(name, PASS, f"valid for {days:.0f} more days")


def check_waf_token(cookies: dict) -> Check:
    name = "AWS WAF token"
    token = (cookies or {}).get("aws-waf-token", "")
    if token:
        return Check(name, PASS, f"present ({len(token)} chars)")
    # WARN not FAIL: this one is short-lived and re-minted automatically. The live
    # auth check below is the authority on whether requests actually get through.
    return Check(name, WARN, "missing (Skool's firewall may block requests)",
                 "Run `uv run refresh_cookie.py` to mint a fresh one.")


def check_group(env: dict) -> Check:
    name = "SKOOL_GROUP"
    slug = (env or {}).get("SKOOL_GROUP", "").strip()
    if not slug:
        return Check(name, FAIL, "not set",
                     "Run `uv run connect_skool.py` and pick your community.")
    return Check(name, PASS, slug)


_SKOOL_VARS = ("SKOOL_COOKIE", "SKOOL_GROUP", "SKOOL_GROUP_ID")


def check_live_auth(env: dict, whoami=None) -> Check:
    env = env or {}
    cookie = env.get("SKOOL_COOKIE", "").strip()
    slug = env.get("SKOOL_GROUP", "").strip()
    if not cookie or not slug:
        return Check(LIVE_CHECK, SKIP, "needs SKOOL_COOKIE and SKOOL_GROUP",
                     "Fix the checks above first.")

    # skool_client reads credentials from the environment, so they have to go
    # there — but only for the duration of this one call. Anything left behind is
    # inherited by every later subprocess (launchctl, schtasks) and is readable
    # via `ps eww`, which hands the cookie to tools that have no business with it.
    saved = {k: os.environ.get(k) for k in _SKOOL_VARS}
    try:
        if whoami is None:
            os.environ["SKOOL_COOKIE"] = cookie
            os.environ["SKOOL_GROUP"] = slug
            gid = env.get("SKOOL_GROUP_ID", "").strip()
            if gid:
                os.environ["SKOOL_GROUP_ID"] = gid
            else:
                os.environ.pop("SKOOL_GROUP_ID", None)
            try:
                import skool_client
            except Exception as e:
                return Check(LIVE_CHECK, FAIL,
                             f"could not import skool_client ({type(e).__name__})",
                             "Run `uv sync`.")
            whoami = skool_client.whoami
        try:
            info = whoami() or {}
        except Exception as e:
            return Check(LIVE_CHECK, FAIL, f"{type(e).__name__}: {e}",
                         "Cookie is probably expired. Run `uv run connect_skool.py`.")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # active_group is only ever the value we just set, so the server-returned
    # group list is the one part of this that actually proves anything.
    known = {g.get("slug") for g in (info.get("groups") or []) if isinstance(g, dict)}
    if known and slug not in known:
        return Check(LIVE_CHECK, FAIL,
                     f"authenticated, but this account is not a member of '{slug}'",
                     "Run `uv run connect_skool.py` and pick from your own communities.")
    if info.get("active_group") != slug:
        return Check(LIVE_CHECK, FAIL,
                     f"resolved '{info.get('active_group')}' instead of '{slug}'",
                     "Run `uv run connect_skool.py` and pick your community again.")
    if not info.get("active_group_id"):
        return Check(LIVE_CHECK, FAIL, f"could not resolve a group id for '{slug}'",
                     "Check SKOOL_GROUP matches the slug in your Skool URL.")
    return Check(LIVE_CHECK, PASS, f"authenticated, '{slug}' resolved")


def safe_env() -> dict:
    """A copy of the environment with the Skool credentials stripped. Child
    processes have no need for them and their environments are widely readable."""
    return {k: v for k, v in os.environ.items() if not k.startswith("SKOOL_")}


def _system_binary(system: str, name: str) -> str:
    """Absolute path to a system tool. On Windows a bare name is resolved against
    the current directory first, so an executable dropped next to an unzipped
    bundle would run instead of the real one."""
    # Separators are hard-coded per branch: os.path.join follows the HOST's rules,
    # so building the Windows path on any other OS yields mixed separators.
    if system == "Windows":
        root = os.environ.get("SystemRoot", r"C:\Windows").rstrip("\\/")
        return f"{root}\\System32\\{name}.exe"
    return f"/bin/{name}"


def check_refresh_job(runner=None, system=None) -> Check:
    """Is the optional auto-refresh job registered? INFO only — never a failure,
    the job is an optional convenience."""
    name = "Auto-refresh job"
    runner = runner or subprocess.run
    system = system or platform.system()
    if system == "Darwin":
        cmd = [_system_binary(system, "launchctl"), "list"]
    elif system == "Windows":
        cmd = [_system_binary(system, "schtasks"), "/query", "/fo", "csv"]
    else:
        return Check(name, INFO, f"not checked on {system}",
                     "Auto-refresh installers ship for macOS and Windows only.")
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=20, env=safe_env())
        out = (getattr(proc, "stdout", "") or "")
    except Exception as e:
        return Check(name, INFO, f"could not check ({type(e).__name__})",
                     "Optional. Skip it or install the job manually.")
    # Match the exact labels our installers register, per platform. A loose
    # substring is wrong in both directions: it missed both real labels (so a
    # correct install reported "not installed" and failed the setup gate), while
    # still matching an unrelated older job someone happened to have.
    if _JOB_LABELS[system].lower() in out.lower():
        return Check(name, INFO, "registered")
    return Check(name, INFO, "not installed (optional)",
                 "macOS: `./install_refresh_mac.sh`. "
                 "Windows: `powershell -File install_refresh_windows.ps1`.")


# --- orchestration -----------------------------------------------------------------
def run_checks(env_path: str = ENV_PATH, live: bool = True, whoami=None,
               chromium_runner=None, job_runner=None) -> list:
    env = read_env_file(env_path)
    cookies = _parse_cookie_header(env.get("SKOOL_COOKIE", ""))
    checks = [
        check_python(),
        check_imports(),
        check_mcp_server_api(),
        check_chromium(runner=chromium_runner),
        check_env_file(env_path),
        check_cookie(env),
        check_auth_token(cookies),
        check_waf_token(cookies),
        check_group(env),
    ]
    if live or whoami is not None:
        checks.append(check_live_auth(env, whoami=whoami))
    else:
        checks.append(Check(LIVE_CHECK, SKIP, "skipped (--no-live)",
                            "Drop --no-live to test against Skool."))
    checks.append(check_refresh_job(runner=job_runner))
    return checks


def auth_ok(checks: list) -> bool:
    return any(c.name == LIVE_CHECK and c.status == PASS for c in checks)


def exit_code(checks: list) -> int:
    """0 only when the install is actually proven healthy.

    A live auth that was skipped is not a pass: an agent gating on the exit code
    of `doctor.py --no-live` would otherwise read "no failures" as "working".
    """
    if any(c.status == FAIL for c in checks):
        return 1
    return 0 if auth_ok(checks) else 1


def render_human(checks: list, secrets=None) -> str:
    secrets = secrets or []
    lines = ["", "Skool MCP doctor", "-" * 58]
    for c in checks:
        lines.append(f"  [{c.status:<4}] {c.name:<24} {scrub(c.detail, secrets)}")
        if c.hint and c.status in (WARN, FAIL, SKIP, INFO):
            lines.append(f"         fix: {scrub(c.hint, secrets)}")
    tally = {s: sum(1 for c in checks if c.status == s)
             for s in (PASS, WARN, FAIL, INFO, SKIP)}
    lines.append("-" * 58)
    lines.append("AUTH OK" if auth_ok(checks) else "AUTH NOT OK")
    plural = "" if tally[WARN] == 1 else "s"
    summary = (f"{tally[PASS]} passed, {tally[WARN]} warning{plural}, "
               f"{tally[FAIL]} failed")
    skipped = tally[INFO] + tally[SKIP]
    if skipped:
        summary += f", {skipped} informational"
    lines.append(summary)
    if tally[FAIL]:
        lines.append("Fix the FAIL lines above, then run `uv run doctor.py` again.")
    lines.append("")
    return "\n".join(lines)


def render_json(checks: list, secrets=None) -> str:
    secrets = secrets or []
    payload = {
        "ok": exit_code(checks) == 0,
        "auth_ok": auth_ok(checks),
        "checks": [
            {**asdict(c),
             "detail": scrub(c.detail, secrets),
             "hint": scrub(c.hint, secrets)}
            for c in checks
        ],
    }
    return json.dumps(payload, indent=2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Health-check the Skool MCP install.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-live", action="store_true",
                    help="skip the live Skool auth check (no network)")
    args = ap.parse_args(argv)

    checks = run_checks(live=not args.no_live)
    secrets = secrets_from(read_env_file(ENV_PATH))
    out = render_json(checks, secrets) if args.json else render_human(checks, secrets)
    try:
        print(out)
    except UnicodeEncodeError:  # legacy Windows console codepage
        print(out.encode("ascii", "ignore").decode())
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
