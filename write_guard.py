"""
Safety layer for the admin write tools.

Every admin write (approve/decline a member, edit/delete a post, reply to a
comment, calendar create/update/delete, invites) goes through run(), which
enforces, in this order:

  1. The write switch. SKOOL_WRITE_TOOLS must be on (1/true/yes/on). Off by
     default; when off, server.py does not even register the write tools.
  2. Two-step confirm. confirm=False never writes: it re-reads the target, builds
     the exact request, and returns a preview plus a one-time confirm token. Only
     a second call with confirm=True and that token, for the SAME tool and the
     SAME arguments, within 10 minutes, executes. Tokens live in memory, so a
     server restart simply means "preview again".
  3. A fresh pre-flight. On confirm the target is re-read right before writing
     (member still pending, post/event still exists, post re-fetched for the full
     body merge). The preview's read is never trusted for the write.
  4. Rate caps, enforced from the audit log, so they survive restarts.
  5. A global floor between writes (default 1s, can only be raised).
  6. An audit trail. An "attempt" line is written BEFORE the request goes out and
     a "result" line after, so a crash mid-write still counts against the caps.
     If the audit log cannot be written, the write is refused: no trail, no write.

The audit log never holds the cookie or a message body. Every free-text field is
clipped to 200 characters and scrubbed of anything that looks like the cookie.

Deliberately independent of skool_client: it knows nothing about HTTP.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

TOKEN_TTL_SECONDS = 600
_MAX_PENDING_TOKENS = 200
_SECRET = secrets.token_bytes(32)
_tokens: dict = {}
_token_lock = threading.Lock()
_write_lock = threading.Lock()
_last_attempt_t = 0.0

# Indirection so tests can drive the clock without sleeping.
_now = time.time
_sleep = time.sleep

_TRUTHY = {"1", "true", "yes", "on"}

DISABLED_MSG = (
    "Skool admin write tools are turned off. They are opt-in: set "
    "SKOOL_WRITE_TOOLS=1 in the MCP server's environment and restart it. See the "
    "README section 'Admin write tools'.")


class WriteRefused(RuntimeError):
    """A write that was deliberately not sent. Nothing reached Skool."""


def writes_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("SKOOL_WRITE_TOOLS", "")).strip().lower() in _TRUTHY


# --- audit log location ---------------------------------------------------------
def _audit_path() -> str:
    """Per OS, mirroring refresh_cookie.py's log placement.

    macOS:   ~/Library/Logs — outside ~/Documents, which macOS TCC protects.
    Windows: %LOCALAPPDATA%\\skool-mcp
    other:   ~/.local/state/skool-mcp (XDG state dir)
    """
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/skool-mcp-audit.jsonl")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "skool-mcp", "audit.jsonl")
    return os.path.expanduser("~/.local/state/skool-mcp/audit.jsonl")


AUDIT_LOG = _audit_path()


# --- caps -------------------------------------------------------------------------
# bucket -> ((window, env var, default), ...). Units: one per member/post/event
# action; one per EMAIL for invites.
BUCKETS = {
    "members": (("hour", "SKOOL_CAP_MEMBERS_PER_HOUR", 30),
                ("day", "SKOOL_CAP_MEMBERS_PER_DAY", 200)),
    "invites": (("day", "SKOOL_CAP_INVITES_PER_DAY", 20),),
    "calendar": (("hour", "SKOOL_CAP_CALENDAR_PER_HOUR", 10),),
    "posts": (("hour", "SKOOL_CAP_POSTS_PER_HOUR", 20),),
}
WINDOWS = {"hour": 3600, "day": 86400}
INVITES_PER_CALL = ("SKOOL_CAP_INVITES_PER_CALL", 5)
MIN_INTERVAL = ("SKOOL_WRITE_MIN_INTERVAL", 1.0)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v >= 0 else default


def cap_limit(bucket: str, window: str) -> int:
    for w, env, default in BUCKETS[bucket]:
        if w == window:
            return _env_int(env, default)
    raise KeyError(f"{bucket}/{window}")


def invites_per_call() -> int:
    return _env_int(*INVITES_PER_CALL)


def min_interval() -> float:
    """The floor between writes. Configurable UPWARD only: 1s is the floor."""
    raw = os.environ.get(MIN_INTERVAL[0], "").strip()
    try:
        v = float(raw) if raw else MIN_INTERVAL[1]
    except ValueError:
        v = MIN_INTERVAL[1]
    return max(MIN_INTERVAL[1], v)


def _read_records(path: str | None = None) -> list:
    path = path or AUDIT_LOG
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a torn line must not disable the caps
                if isinstance(rec, dict):
                    out.append(rec)
    except FileNotFoundError:
        pass  # no writes yet
    except OSError as e:
        # The caps are computed from this file. If it cannot be read they cannot
        # be enforced, so fail closed rather than write uncapped.
        raise WriteRefused(f"Audit log is not readable at {path} ({type(e).__name__}), "
                           "so the rate caps cannot be enforced. Refusing to write.") from None
    return out


def usage(bucket: str, now: float | None = None, records: list | None = None) -> dict:
    """Units already spent in each window of this bucket (attempts, not results:
    an attempt that errored may still have landed on Skool's side)."""
    now = _now() if now is None else now
    records = _read_records() if records is None else records
    out = {}
    for window, _env, _default in BUCKETS[bucket]:
        since = now - WINDOWS[window]
        used = 0
        for r in records:
            if r.get("event") != "attempt" or r.get("bucket") != bucket:
                continue
            try:
                t = float(r.get("t", 0))
            except (TypeError, ValueError):
                continue
            if t >= since:
                try:
                    used += int(r.get("units", 1))
                except (TypeError, ValueError):
                    used += 1
        out[window] = {"used": used, "limit": cap_limit(bucket, window)}
    return out


def check_caps(bucket: str, units: int, now: float | None = None) -> dict:
    u = usage(bucket, now=now)
    for window, info in u.items():
        if info["used"] + units > info["limit"]:
            raise WriteRefused(
                f"Rate cap reached for {bucket}: {info['used']} used in the last "
                f"{window}, limit {info['limit']}, this action needs {units}. "
                f"Wait, or raise the cap via its environment variable (see README).")
    return u


# --- confirm tokens ---------------------------------------------------------------
def _canonical(tool: str, args: dict) -> str:
    return json.dumps({"tool": tool, "args": args}, sort_keys=True,
                      separators=(",", ":"), default=str, ensure_ascii=False)


def _args_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(_canonical(tool, args).encode("utf-8")).hexdigest()


def issue_token(tool: str, args: dict) -> str:
    """A one-time token bound to this tool + these exact arguments (group
    included), valid TOKEN_TTL_SECONDS."""
    now = _now()
    nonce = secrets.token_hex(8)
    digest = hmac.new(_SECRET, f"{_canonical(tool, args)}|{nonce}|{now}".encode("utf-8"),
                      hashlib.sha256).hexdigest()
    token = digest[:24]
    with _token_lock:
        for k in [k for k, v in _tokens.items() if v["exp"] <= now]:
            del _tokens[k]
        while len(_tokens) >= _MAX_PENDING_TOKENS:
            del _tokens[min(_tokens, key=lambda k: _tokens[k]["exp"])]
        _tokens[token] = {"tool": tool, "hash": _args_hash(tool, args),
                          "exp": now + TOKEN_TTL_SECONDS}
    return token


def redeem_token(tool: str, args: dict, token: str) -> None:
    """Consume a token or raise WriteRefused. Single use either way: a token that
    fails a check is burned too, so a wrong guess cannot be retried into a match."""
    token = (token or "").strip()
    now = _now()
    with _token_lock:
        rec = _tokens.pop(token, None)
        if rec is not None and rec["tool"] == tool and \
                hmac.compare_digest(rec["hash"], _args_hash(tool, args)):
            # Burn sibling tokens from repeated previews of the SAME action, so
            # previewing twice cannot be turned into executing twice (a duplicate
            # reply, a second round of invite emails).
            for k in [k for k, v in _tokens.items()
                      if v["tool"] == tool and v["hash"] == rec["hash"]]:
                del _tokens[k]
    if rec is None:
        raise WriteRefused("Unknown or already-used confirm token. Run the tool again "
                           "with confirm=false to get a fresh preview and token.")
    if rec["exp"] <= now:
        raise WriteRefused("Confirm token expired (they last 10 minutes). Run the tool "
                           "again with confirm=false for a fresh preview.")
    if rec["tool"] != tool:
        raise WriteRefused(f"That confirm token was issued for {rec['tool']}, not {tool}.")
    if not hmac.compare_digest(rec["hash"], _args_hash(tool, args)):
        raise WriteRefused("The arguments changed since the preview, so the token does "
                           "not match. Preview again with the new arguments.")


# --- audit ------------------------------------------------------------------------
def _cookie_fragments() -> list:
    cookie = os.environ.get("SKOOL_COOKIE", "")
    frags = [cookie] if len(cookie) >= 8 else []
    for part in cookie.split(";"):
        _, _, val = part.partition("=")
        val = val.strip()
        if len(val) >= 8:
            frags.append(val)
    return sorted(set(frags), key=len, reverse=True)


def scrub(text) -> str:
    s = "" if text is None else str(text)
    for frag in _cookie_fragments():
        s = s.replace(frag, "[redacted]")
    return s


def _clip(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _ensure_audit_writable(path: str | None = None) -> None:
    path = path or AUDIT_LOG
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(fd)
    except OSError as e:
        raise WriteRefused(f"Audit log is not writable at {path} ({type(e).__name__}). "
                           "Refusing to write to Skool without an audit trail.") from None


def audit(event: str, tool: str, group: str, *, targets=(), bucket=None, units=None,
          status=None, detail=None, now: float | None = None) -> dict:
    now = _now() if now is None else now
    rec = {
        "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        "t": round(now, 3),
        "event": event,
        "tool": tool,
        "group": group,
        "bucket": bucket,
        "units": units,
        "targets": [_clip(scrub(t), 120) for t in list(targets)[:20]],
        "status": status,
        "detail": _clip(scrub(detail), 200) if detail is not None else None,
    }
    rec = {k: v for k, v in rec.items() if v is not None}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(AUDIT_LOG), mode=0o700, exist_ok=True)
    fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return rec


def _audit_refusal(tool: str, group: str, **kw) -> None:
    """Log a refusal without letting a broken log hide the refusal's own reason."""
    try:
        audit("refused", tool, group, **kw)
    except OSError:
        pass


def _last_attempt_from_log(records: list) -> float:
    last = 0.0
    for r in records:
        if r.get("event") == "attempt":
            try:
                last = max(last, float(r.get("t", 0)))
            except (TypeError, ValueError):
                pass
    return last


def _respect_floor() -> None:
    global _last_attempt_t
    last = max(_last_attempt_t, _last_attempt_from_log(_read_records()))
    wait = min_interval() - (_now() - last)
    if wait > 0:
        _sleep(wait)


# --- the flow ---------------------------------------------------------------------
@dataclass
class Plan:
    """What a write would do, built from a FRESH read of the target."""
    summary: str
    method: str
    url: str
    body: object = None
    targets: list = field(default_factory=list)
    units: int = 1
    warnings: list = field(default_factory=list)
    details: dict = field(default_factory=dict)


def run(*, tool: str, bucket: str, group: str, args: dict, confirm: bool,
        confirm_token: str, plan_fn, execute_fn) -> dict:
    """Preview (confirm=False) or execute (confirm=True + token) one write.

    plan_fn() -> Plan: reads the target and validates. Raise WriteRefused (or
    ValueError) to stop. It runs on BOTH calls: the confirm call re-reads.
    execute_fn(plan) -> dict: does the HTTP write. May return {"status": ...}.
    """
    global _last_attempt_t
    if not writes_enabled():
        raise WriteRefused(DISABLED_MSG)
    key_args = {"group": group, **args}

    if not confirm:
        try:
            plan = plan_fn()
        except ValueError as e:
            raise WriteRefused(str(e)) from None
        token = issue_token(tool, key_args)
        caps = usage(bucket)
        warnings = list(plan.warnings)
        for window, info in caps.items():
            if info["used"] + plan.units > info["limit"]:
                warnings.append(f"Rate cap: {info['used']}/{info['limit']} used this "
                                f"{window}. Confirming now will be refused.")
        preview = {
            "preview": True,
            "executed": False,
            "tool": tool,
            "group": group,
            "summary": plan.summary,
            "request": {"method": plan.method, "url": plan.url, "body": plan.body},
            "targets": plan.targets,
            "warnings": warnings,
            "rate_caps": {"bucket": bucket, "this_action_units": plan.units, **caps},
            "confirm_token": token,
            "expires_in_seconds": TOKEN_TTL_SECONDS,
            "next_step": (
                f"Show the user this preview. Only if they say yes, call {tool} again "
                f"with exactly the same arguments plus confirm=true and "
                f'confirm_token="{token}". The token works once and expires in '
                f"{TOKEN_TTL_SECONDS // 60} minutes."),
        }
        if plan.details:
            preview["details"] = plan.details
        return preview

    if not (confirm_token or "").strip():
        _audit_refusal(tool, group, bucket=bucket, detail="confirm=true without a token")
        raise WriteRefused("confirm=true needs the confirm_token from a preview. Call "
                           "the tool with confirm=false first and show the user the preview.")
    try:
        redeem_token(tool, key_args, confirm_token)
    except WriteRefused as e:
        _audit_refusal(tool, group, bucket=bucket, detail=str(e))
        raise

    with _write_lock:
        try:
            plan = plan_fn()  # fresh pre-flight re-read, right before the write
        except Exception as e:
            _audit_refusal(tool, group, bucket=bucket,
                           detail=f"pre-flight: {type(e).__name__}: {e}")
            if isinstance(e, ValueError):
                raise WriteRefused(str(e)) from None
            raise
        try:
            check_caps(bucket, plan.units)
            _ensure_audit_writable()
        except WriteRefused as e:
            _audit_refusal(tool, group, targets=plan.targets, bucket=bucket,
                           units=plan.units, detail=str(e))
            raise
        _respect_floor()
        try:
            audit("attempt", tool, group, targets=plan.targets, bucket=bucket,
                  units=plan.units, detail=plan.summary)
        except OSError as e:
            raise WriteRefused(f"Could not write the audit log at {AUDIT_LOG} "
                               f"({type(e).__name__}). Nothing was sent to Skool.") from None
        _last_attempt_t = _now()
        try:
            result = execute_fn(plan) or {}
        except Exception as e:
            # Never let a broken log replace the real error the caller needs.
            _audit_result(tool, group, plan, bucket, "error", f"{type(e).__name__}: {e}")
            raise
        status = result.get("status", "ok") if isinstance(result, dict) else "ok"
        logged = _audit_result(tool, group, plan, bucket, status)

    out = {"preview": False, "executed": True, "tool": tool, "group": group,
           "summary": plan.summary, "status": status, "result": result,
           "audit_log": AUDIT_LOG}
    if not logged:
        # The write DID happen. Reporting it as an exception would invite a retry,
        # which for a reply or an invite does it twice.
        out["audit_warning"] = ("The write went through, but its result line could not "
                                "be added to the audit log. The attempt line is there.")
    return out


def _audit_result(tool, group, plan, bucket, status, detail=None) -> bool:
    try:
        audit("result", tool, group, targets=plan.targets, bucket=bucket,
              status=status, detail=detail)
        return True
    except OSError:
        return False
