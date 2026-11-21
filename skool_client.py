"""
Skool API client.

Skool has no public API, so this talks to Skool's internal surface using your
logged-in session cookie:
  - api2.skool.com   — JSON API (posts, comments, members preview, DMs, writes)
  - www.skool.com    — Next.js pages; list views (feed, members, classroom) are
                       read from the embedded __NEXT_DATA__ JSON blob.

Auth is the full Cookie header captured by connect_skool.py. The load-bearing
cookie is auth_token, a ~1-year JWT — measured 2026-08-13, api2 answers 200 with
auth_token alone and no aws-waf-token present, so a missing waf token is NOT an
auth failure and must never be reported as one.

api2.skool.com sits behind CloudFront + AWS WAF, which is a SEPARATE failure
surface from Skool's auth:
  * 401 + short plain-text body  -> Skool saw the request and rejected the cookie.
  * 403 + CloudFront HTML body   -> the edge blocked it; Skool never saw it. This
    says nothing about the cookie. It is triggered by a library User-Agent
    (python-httpx/curl/empty) or by the caller's egress IP (VPN, proxy, hosting
    provider, or Amazon's IP-reputation list).
Conflating the two is what sends people hunting a phantom expired cookie, so
_handle keeps them strictly apart.

All HTTP/parsing lives here so server.py stays a thin list of tools. The admin
write endpoints (member approval, post edit/delete, calendar, invites) live at the
bottom; the confirm/audit/rate-cap safety layer around them is write_guard.py.
"""
import json
import os
import re
from datetime import datetime, timezone

import httpx
from dotenv import dotenv_values, load_dotenv

DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(DIR, ".env")
load_dotenv(ENV_PATH)

API = os.environ.get("SKOOL_API", "https://api2.skool.com")
WWW = "https://www.skool.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CHARACTER_LIMIT = 25000
_GID_CACHE = {}


class AuthError(RuntimeError):
    """Raised when the cookie is missing or rejected (usually expired)."""


class EdgeBlocked(RuntimeError):
    """Raised when CloudFront/AWS WAF blocked the request before Skool saw it.

    Deliberately NOT an AuthError. Every caller that treats "cannot reach Skool"
    as "your login expired" sends the member off to re-authenticate, which cannot
    possibly fix a network-layer block — that dead end is the whole reason this
    class exists.
    """


class NotFound(RuntimeError):
    """404 from Skool. A RuntimeError subclass so existing callers are unaffected;
    the write tools catch it specifically to report "that post/event is gone"."""


class SkoolAPIError(RuntimeError):
    """Any other 4xx/5xx from Skool, with the (clipped) response body kept.

    The body matters for writes: "cannot update to same role" arrives as an error
    status, and the old raise_for_status() threw that text away.
    """

    def __init__(self, status: int, body: str, where: str):
        self.status = status
        self.body = body or ""
        self.where = where
        super().__init__(f"Skool returned HTTP {status} for {where}: {self.body[:300]}")


# --- config / auth ------------------------------------------------------------
def _cookie() -> str:
    c = os.environ.get("SKOOL_COOKIE", "").strip()
    if not c:
        raise AuthError(
            "No SKOOL_COOKIE set. Run `uv run connect_skool.py`, log into Skool in the "
            "window that opens, and it writes your cookie into .env for you."
        )
    return c


def _group() -> str:
    g = os.environ.get("SKOOL_GROUP", "").strip()
    if not g:
        raise AuthError("No SKOOL_GROUP set in .env (your community slug, e.g. solnest-ai).")
    return g


def _group_id() -> str:
    gid = os.environ.get("SKOOL_GROUP_ID", "").strip()
    if gid:
        return gid
    slug = _group()
    if slug in _GID_CACHE:
        return _GID_CACHE[slug]
    for g in _api_get("/self/groups", {"limit": 50, "prefs": "false"}).get("groups", []):
        if g.get("name") == slug:
            _GID_CACHE[slug] = g["id"]
            return g["id"]
    raise RuntimeError(f"Could not resolve a group id for '{slug}'. Check SKOOL_GROUP.")


def _headers() -> dict:
    """Headers for every Skool call.

    The user-agent is not cosmetic: api2 is behind AWS WAF, and a library UA
    (python-httpx, python-requests, curl) or an absent one is blocked at the edge
    with a CloudFront 403 before Skool ever sees the request. Verified 2026-08-13
    — the same cookie returns 200 with a browser UA and 403 with httpx's default.
    The sec-fetch/origin/referer trio mirrors the XHR the Skool web app itself
    sends, so the request does not stand out as scripted to the bot rules.
    """
    return {"Cookie": _cookie(), "content-type": "application/json",
            "accept": "application/json", "user-agent": UA,
            "accept-language": "en-US,en;q=0.9",
            "origin": WWW, "referer": WWW + "/",
            "sec-fetch-site": "same-site", "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty"}


# --- transport ----------------------------------------------------------------
def is_edge_block(r: httpx.Response) -> bool:
    """True when CloudFront/AWS WAF blocked this before Skool saw it.

    Skool's own auth layer answers 401 with a short plain-text body. A 403 whose
    body is CloudFront's error page is a network-layer verdict on the CALLER, not
    on the cookie — so it must never be reported as an expired session.
    """
    if r.status_code != 403:
        return False
    fingerprint = f"{r.headers.get('server', '')} {r.headers.get('via', '')}".lower()
    return "cloudfront" in fingerprint or "<html" in (r.text[:400] or "").lower()


EDGE_BLOCK_HELP = (
    "Blocked by Skool's CDN (CloudFront), not by Skool itself — the request never "
    "reached Skool, so your login is fine and reconnecting will NOT help.\n"
    "This is about where your traffic comes from. Try, in order:\n"
    "  1. Disconnect any VPN or proxy, then retry.\n"
    "  2. Switch networks (wifi -> phone hotspot) — shared/flagged IPs get blocked.\n"
    "  3. Unset HTTP_PROXY/HTTPS_PROXY in your shell, then retry.\n"
    "Run `uv run net_check.py` to confirm which of these it is."
)


def _handle(r: httpx.Response, where: str):
    if is_edge_block(r):
        raise EdgeBlocked(EDGE_BLOCK_HELP)
    if r.status_code in (401, 403):
        raise AuthError(
            "Skool rejected your session (HTTP %d) — the cookie has almost certainly "
            "expired. Run `uv run connect_skool.py` to reconnect, or `uv run doctor.py` "
            "to see exactly what's wrong." % r.status_code
        )
    if r.status_code == 404:
        raise NotFound(f"Not found: {where} (bad id/slug?).")
    if r.status_code == 429:
        raise RuntimeError("Rate limited by Skool — wait a bit and retry.")
    if r.status_code >= 400:
        raise SkoolAPIError(r.status_code, (r.text or "")[:2000], where)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:CHARACTER_LIMIT]}


# AWS WAF gates api2 on TWO independent signals, and fixing only one leaves a
# machine broken in a way that looks identical to the other:
#
#   1. The User-Agent header. A library UA ("python-httpx/...", "curl/...", or an
#      absent one) is 403'd outright. Measured 2026-08-17: this fires even behind
#      a perfect Chrome TLS handshake, so it is a genuinely separate rule.
#   2. The TLS handshake fingerprint (JA3/JA4), applied on SOME source IPs.
#      Python's OpenSSL handshake does not look like Chrome's. On most IPs this is
#      not inspected — a browser UA over plain httpx is enough — but on an IP the
#      WAF has flagged, only a real Chrome fingerprint gets through.
#
# So the transport prefers curl_cffi (Chrome-impersonating TLS) and keeps httpx as
# the fallback. httpx alone is correct for the common case and was verified
# working; it just cannot clear signal 2, which is invisible until you hit an IP
# where it matters and then looks exactly like an unfixable network block.
IMPERSONATE = os.environ.get("SKOOL_IMPERSONATE", "").strip() or "chrome"

try:
    from curl_cffi import requests as _curl
except ImportError:  # optional: older installs keep working on httpx alone
    _curl = None


def tls_impersonation_available() -> bool:
    """Whether this install can present a Chrome TLS fingerprint."""
    return _curl is not None


class WriteOutcomeUnknown(RuntimeError):
    """A write hit a network error after it may already have reached Skool.

    Raised instead of re-sending, because re-sending a write that did land does it
    twice (a second reply, a second round of invite emails, a duplicate event).
    """


_WRITE_UNKNOWN_MSG = (
    "Network error while sending this to Skool ({kind}). It may or may not have gone "
    "through, so it was NOT re-sent automatically (that could do it twice). Check in "
    "Skool before trying again.")


def _request(method: str, url: str, *, headers: dict, params=None, json_body=None,
             raw_body: bytes | None = None, follow_redirects: bool = False,
             timeout: int = 40, resend_on_error: bool = True):
    """One HTTP call, with a real Chrome TLS fingerprint when available.

    Returns something response-shaped either way: both backends expose
    .status_code, .headers.get() and .text, which is all _handle and
    is_edge_block touch.

    raw_body sends exact bytes. It exists for DELETE /posts/{id}, which wants a
    literal `{}` body: sending b"{}" verbatim does not depend on how either
    library treats an empty json= dict on a DELETE.

    resend_on_error=False is for the admin writes. By default a curl_cffi failure
    falls back to httpx, which RE-SENDS the request; after a timeout the first copy
    may already have landed. With it off, the only failure that still falls back
    is an impersonation-target error (raised before anything is sent); anything
    else becomes WriteOutcomeUnknown.
    """
    if _curl is not None:
        kwargs = {"headers": headers, "timeout": timeout,
                  "impersonate": IMPERSONATE, "allow_redirects": follow_redirects}
        if params:
            kwargs["params"] = params
        if raw_body is not None:
            kwargs["data"] = raw_body
        elif json_body is not None:
            kwargs["json"] = json_body
        try:
            return getattr(_curl, method)(url, **kwargs)
        except Exception as e:
            # A curl_cffi-specific failure (missing native lib, unknown
            # impersonation target) must not take out an install that httpx can
            # still serve. Fall through rather than surfacing a confusing error.
            if not resend_on_error and "impersonat" not in str(e).lower():
                raise WriteOutcomeUnknown(
                    _WRITE_UNKNOWN_MSG.format(kind=type(e).__name__)) from None
    try:
        with httpx.Client(timeout=timeout, follow_redirects=follow_redirects) as c:
            if raw_body is not None:
                return c.request(method.upper(), url, headers=headers,
                                 params=params or {}, content=raw_body)
            return c.request(method.upper(), url, headers=headers,
                             params=params or {},
                             json=json_body if json_body is not None else None)
    except httpx.TransportError as e:
        if resend_on_error:
            raise
        raise WriteOutcomeUnknown(_WRITE_UNKNOWN_MSG.format(kind=type(e).__name__)) from None


# --- stale-cookie reload --------------------------------------------------------
# refresh_cookie.py rewrites .env every couple of days. A long-running MCP server
# loaded the OLD cookie at import, so without this it keeps sending a cookie that
# has since been replaced and fails until someone restarts it.
#
# Only SKOOL_COOKIE is reloaded, deliberately: a full load_dotenv(override=True)
# would also let an edited .env flip SKOOL_WRITE_TOOLS or the rate caps inside a
# running server. And the reload only fires when .env actually CHANGED ON DISK
# since we last read it. That gate is load-bearing: refresh_cookie.verify() puts a
# freshly minted cookie in the environment and calls whoami() to test it BEFORE
# writing .env. Without the gate a rejected new cookie would silently fall back to
# the old one from .env, "pass", and refresh would persist the broken cookie.
def _env_stamp():
    try:
        st = os.stat(ENV_PATH)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_ENV_STAMP = _env_stamp()


def _reload_cookie_from_env() -> bool:
    """Pick up a cookie that refresh_cookie.py wrote after we started.

    True only if .env changed on disk AND now holds a different, non-empty cookie.
    """
    global _ENV_STAMP
    stamp = _env_stamp()
    if stamp is None or stamp == _ENV_STAMP:
        return False
    _ENV_STAMP = stamp
    try:
        new = (dotenv_values(ENV_PATH).get("SKOOL_COOKIE") or "").strip()
    except Exception:
        return False
    if not new or new == os.environ.get("SKOOL_COOKIE", "").strip():
        return False
    os.environ["SKOOL_COOKIE"] = new
    return True


def _auth_rejected(r) -> bool:
    return r.status_code in (401, 403) and not is_edge_block(r)


def _call(method: str, url: str, *, accept: str | None = None, params=None,
          json_body=None, raw_body: bytes | None = None,
          follow_redirects: bool = False, resend_on_error: bool = True):
    """_request with fresh headers, retried ONCE if Skool rejects the cookie and
    .env now holds a newer one. Safe for writes too: a 401/403 means Skool refused
    the request before acting on it, so the retry cannot double-apply anything.
    An edge block (CloudFront 403) is never retried — no cookie can fix it."""
    def once():
        headers = _headers()
        if accept:
            headers["accept"] = accept
        return _request(method, url, headers=headers, params=params,
                        json_body=json_body, raw_body=raw_body,
                        follow_redirects=follow_redirects,
                        resend_on_error=resend_on_error)

    r = once()
    if _auth_rejected(r) and _reload_cookie_from_env():
        r = once()
    return r


def _api_get(path: str, params: dict | None = None):
    r = _call("get", API + path, params=params or {})
    return _handle(r, path)


def _api_post(path: str, payload=None, params: dict | None = None, *,
              resend_on_error: bool = True):
    r = _call("post", API + path, params=params or {},
              json_body=payload if payload is not None else {},
              resend_on_error=resend_on_error)
    return _handle(r, path)


def _api_put(path: str, payload=None, *, resend_on_error: bool = True):
    r = _call("put", API + path, json_body=payload or {},
              resend_on_error=resend_on_error)
    return _handle(r, path)


def _api_delete(path: str, empty_json_body: bool = False):
    """DELETE. empty_json_body=True sends a literal `{}` (DELETE /posts/{id}
    needs it); False sends no body at all (DELETE /calendar-events/{id}).
    Only the admin tools use this, so it never re-sends after a transport error."""
    r = _call("delete", API + path, raw_body=b"{}" if empty_json_body else None,
              resend_on_error=False)
    return _handle(r, path)


def _page_props(path: str) -> dict:
    """Fetch a www.skool.com page and return props.pageProps from __NEXT_DATA__."""
    r = _call("get", WWW + path, accept="text/html", follow_redirects=True)
    if is_edge_block(r):
        raise EdgeBlocked(EDGE_BLOCK_HELP)
    if r.status_code in (401, 403):
        raise AuthError("Cookie expired — re-run `uv run connect_skool.py`.")
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError(f"No page data found at {path} (logged out?).")
    return json.loads(m.group(1)).get("props", {}).get("pageProps", {})


def _clip(s, n=400):
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def _truncate(data):
    t = json.dumps(data)
    if len(t) > CHARACTER_LIMIT:
        return {"truncated": True,
                "note": "Response too large; lower `limit` or paginate.",
                "preview": t[:CHARACTER_LIMIT]}
    return data


# --- identity -----------------------------------------------------------------
def whoami() -> dict:
    data = _api_get("/self/groups", {"limit": 50, "prefs": "false"})
    groups = [{"slug": g.get("name"), "id": g.get("id")} for g in data.get("groups", [])]
    return {"active_group": _group(), "active_group_id": _group_id(), "groups": groups}


# --- feed / posts -------------------------------------------------------------
def list_posts(limit: int = 20) -> dict:
    limit = max(1, min(limit, 50))
    pp = _page_props(f"/{_group()}")
    trees = pp.get("postTrees") or pp.get("renderData", {}).get("postTrees", [])
    out, seen = [], set()
    for t in trees:
        p = t.get("post", {})
        pid = p.get("id")
        # Skool prepends pinned posts to the feed but also leaves them in their
        # chronological slot, so the same id appears twice. Keep the first (pinned).
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)
        md = p.get("metadata", {})
        out.append({
            "id": p.get("id"),
            "name": p.get("name"),  # slug, used by get_post
            "title": md.get("title") or _clip(md.get("content"), 80),
            "content": _clip(md.get("content"), 400),
            "comments": md.get("comments"),
            "upvotes": md.get("upvotes"),
            "user_id": p.get("user_id"),
            "created_at": p.get("created_at"),
        })
        if len(out) >= limit:
            break
    return _truncate({"posts": out, "count": len(out), "total": pp.get("total")})


def get_post(post_name: str, with_comments: bool = True) -> dict:
    data = _api_get(f"/groups/{_group()}/post-detail", {"post": post_name})
    tree = data.get("post_tree", {})
    p = tree.get("post", {})
    md = p.get("metadata", {})
    result = {
        "id": p.get("id"), "name": p.get("name"),
        "title": md.get("title"), "content": md.get("content"),
        "comments_count": md.get("comments"), "upvotes": md.get("upvotes"),
        "user_id": p.get("user_id"), "created_at": p.get("created_at"),
    }
    if with_comments and p.get("id"):
        try:
            result["comments"] = get_comments(p["id"]).get("comments", [])
        except Exception as e:
            result["comments_error"] = str(e)
    return _truncate(result)


def get_comments(post_id: str, limit: int = 50) -> dict:
    data = _api_get(f"/posts/{post_id}/comments", {"group-id": _group_id()})
    children = data.get("post_tree", {}).get("children", [])
    out = []
    for ch in children[:limit]:
        p = ch.get("post", {})
        md = p.get("metadata", {})
        out.append({"id": p.get("id"), "content": md.get("content"),
                    "user_id": p.get("user_id"), "created_at": p.get("created_at")})
    return _truncate({"comments": out, "count": len(out)})


def create_post(title: str, content: str) -> dict:
    payload = {"post_type": "post", "group_id": _group_id(),
               "metadata": {"title": title or "", "content": content,
                            "attachments": "", "action": 0, "video_ids": ""}}
    r = _api_post("/posts", payload, params={"follow": "false"})
    return {"created": True, "id": r.get("id"), "name": r.get("metadata", {}).get("title")}


def comment_on_post(post_id: str, content: str) -> dict:
    # A top-level comment is root = parent = the post. Same payload builder as
    # reply_to_comment, so the two cannot drift apart.
    r = _api_post("/posts", comment_payload(post_id, post_id, content),
                  params={"follow": "false"})
    return {"created": True, "id": r.get("id")}


# --- members ------------------------------------------------------------------
def list_members(limit: int = 10000) -> dict:
    group = _group()
    pp = _page_props(f"/{group}/-/members")
    all_users = list(pp.get("users") or pp.get("renderData", {}).get("members", []))
    total_pages = pp.get("totalPages", 1)
    total = pp.get("total") or pp.get("totalMembers", len(all_users))

    for page in range(2, total_pages + 1):
        pp2 = _page_props(f"/{group}/-/members?p={page}")
        page_users = pp2.get("users") or pp2.get("renderData", {}).get("members", [])
        all_users.extend(page_users)

    out = []
    for u in all_users[:limit]:
        out.append({"id": u.get("id"), "name": u.get("name"),
                    "first_name": u.get("firstName"), "last_name": u.get("lastName"),
                    "email": u.get("email")})
    return _truncate({"members": out, "count": len(out), "total": total})


def list_members_raw(limit: int = 100000) -> list:
    """Full member objects across ALL pages, metadata intact — including the
    billing fields the MCP tool above strips (`member.metadata.mmbp` =
    {amount_cents, recurring_interval}, `mbsltv` = LTV cents, `mbme` = email).
    Powers the daily MRR sync. Same auth/cookie/page path as list_members(), so
    a Skool site change only needs fixing here once, for both consumers."""
    group = _group()
    pp = _page_props(f"/{group}/-/members")
    all_users = list(pp.get("users") or pp.get("renderData", {}).get("members", []))
    total_pages = pp.get("totalPages", 1)
    for page in range(2, total_pages + 1):
        pp2 = _page_props(f"/{group}/-/members?p={page}")
        all_users.extend(pp2.get("users") or pp2.get("renderData", {}).get("members", []))
    return all_users[:limit]


# --- classroom ----------------------------------------------------------------
def list_courses() -> dict:
    pp = _page_props(f"/{_group()}/classroom")
    courses = pp.get("allCourses") or pp.get("renderData", {}).get("allCourses", [])
    out = []
    for c in courses:
        md = c.get("metadata", {})
        out.append({"id": c.get("id"), "name": c.get("name"),
                    "title": md.get("title") or c.get("name"),
                    "description": _clip(md.get("description"), 200)})
    return _truncate({"courses": out, "count": len(out)})


def get_lesson(course_id: str, lesson_id: str = "") -> dict:
    """Read a classroom course. `course_id` is a top course id from list_courses;
    it returns the course's modules/lessons (id + title). Pass a `lesson_id` (a
    child id) to also pull that lesson's full content/metadata (text body if any,
    plus video refs — many lessons are video-only so `content` may be empty)."""
    params = {"md": lesson_id} if lesson_id else None
    d = _api_get(f"/courses/{course_id}", params)
    course = d.get("course", {})
    md = course.get("metadata", {})
    children, target = [], None
    for ch in d.get("children", []):
        cu = ch.get("course", {})
        cmd = cu.get("metadata", {})
        children.append({"id": cu.get("id"), "title": cmd.get("title"),
                         "has_content": bool(cmd.get("content"))})
        if lesson_id and cu.get("id") == lesson_id:
            target = {"id": cu.get("id"), "title": cmd.get("title"),
                      "content": cmd.get("content"), "metadata": cmd}
    result = {
        "id": course.get("id"),
        "title": md.get("title"),
        "description": md.get("desc") or md.get("description"),
        "modules_or_lessons": children,
        "count": len(children),
    }
    if lesson_id:
        result["lesson"] = target or {"note": "lesson_id not found in this course"}
    return _truncate(result)


# --- DMs / inbox --------------------------------------------------------------
def list_conversations(limit: int = 30) -> dict:
    limit = max(1, min(limit, 50))
    data = _api_get("/self/chat-channels",
                    {"offset": 0, "limit": limit, "last": "true", "unread-only": "false"})
    out = []
    for ch in data.get("channels", []):
        md = ch.get("metadata", {})
        out.append({
            "channel_id": ch.get("id"),
            "num_unread": md.get("num_unread"),
            "last_message": _clip(md.get("last_msg") or md.get("last_message"), 140),
            "last_read_at": md.get("last_read_at"),
            "request_user": md.get("request_user"),
            "first_msg_user": md.get("first_msg_user"),
        })
    return _truncate({"conversations": out, "count": len(out)})


def get_conversation(channel_id: str, limit: int = 30) -> dict:
    limit = max(1, min(limit, 100))
    # `before` = how many messages back from the latest; returns the most recent ones.
    data = _api_get(f"/channels/{channel_id}/messages", {"before": limit})
    msgs = data.get("messages", [])
    out = []
    for m in msgs[-limit:]:
        md = m.get("metadata", {})
        out.append({"id": m.get("id"),
                    "from": md.get("src") or m.get("user_id"),
                    "to": md.get("dst"),
                    "content": md.get("content"),
                    "created_at": m.get("created_at")})
    return _truncate({"channel_id": channel_id, "messages": out, "count": len(out)})


def send_message(channel_id: str, content: str) -> dict:
    r = _api_post(f"/channels/{channel_id}/messages",
                  {"content": content, "attachments": []}, params={"ct": "wdm"})
    return {"sent": True, "id": r.get("id") if isinstance(r, dict) else None}


def mark_read(channel_id: str) -> dict:
    _api_post(f"/channels/{channel_id}/read")
    return {"marked_read": True, "channel_id": channel_id}


def start_chat(user_id: str) -> dict:
    r = _api_post(f"/users/{user_id}/chat-request", params={"g": _group_id()})
    return {"started": True, "result": r}


# ==============================================================================
# Admin endpoints (member approval, post edit/delete, calendar, invites).
#
# Raw HTTP + payload building only. Every one of these is reached through the
# confirm / audit / rate-cap layer in write_guard.py via admin_tools.py; nothing
# here should be wired to an MCP tool directly.
#
# Response SHAPES for several reads below (pending members, GET /posts/{id},
# GET /calendar-events/{id}) were not live-verified when this was written, so the
# parsers try the documented key first and fall back, and they never invent data.
# See endpoints.md for the verification status of each endpoint.
# ==============================================================================
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _safe_id(value, what: str = "id") -> str:
    """Ids go straight into URL paths. Refuse anything that could change the path
    ('../', '/', '?', whitespace) instead of trusting the caller."""
    v = str(value or "").strip()
    if not _ID_RE.match(v):
        raise ValueError(f"{what} {value!r} is not a valid Skool id "
                         "(letters, digits, - and _ only).")
    return v


def _decode_json_str(value) -> dict:
    """Skool stores some metadata (event location, privacy) as a JSON *string*."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            d = json.loads(value)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


def _compact_json(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"), ensure_ascii=False)


def _users_of(pp: dict) -> list:
    """The user list on a members page. `users` can be present but null (a page
    with nobody on it), which must read as an empty list, not crash."""
    users = pp.get("users")
    if users is None:
        users = (pp.get("renderData") or {}).get("members")
    return list(users or [])


# --- members: pending requests ------------------------------------------------
# Member roles that are definitely NOT a pending request. Used as a tripwire: if
# Skool ever ignored ?t=pending and served the ACTIVE member list instead, a
# "decline" would remove real members. Entries carrying one of these roles are
# dropped from the pending list no matter which page they came from.
ACTIVE_ROLES = {"member", "admin", "owner", "group-admin", "group-owner",
                "group-moderator", "moderator"}
MAX_PENDING_PAGES = 20
_ANSWER_KEYS = ("survey", "surveyAnswers", "survey_answers", "answers",
                "questions", "membershipQuestions", "joinAnswers")


def _normalize_answers(raw):
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            raw = json.loads(s)
        except ValueError:
            return [{"question": None, "answer": _clip(s, 600)}]
    if isinstance(raw, dict):
        for k in ("survey", "answers", "questions"):
            if isinstance(raw.get(k), list):
                raw = raw[k]
                break
        else:
            return [{"question": str(k), "answer": _clip(str(v), 600)}
                    for k, v in raw.items()] or None
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                q = item.get("question") or item.get("q") or item.get("label")
                a = item.get("answer")
                if a is None:
                    a = item.get("a", item.get("value"))
                out.append({"question": q, "answer": _clip(str(a or ""), 600)})
            elif item is not None:
                out.append({"question": None, "answer": _clip(str(item), 600)})
        return out or None
    return None


def _first_present(*candidates):
    for c in candidates:
        if c is not None and c != "":
            return c
    return None


def pending_entry(u: dict) -> dict:
    """One pending request, normalized. membership_id is users[].member.id — the
    id the approve/decline endpoint takes. It is NOT the user id."""
    m = u.get("member") or {}
    mmd = m.get("metadata") or {}
    umd = u.get("metadata") or {}
    name = " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x).strip()
    answers = None
    for src in (mmd, m, umd, u):
        for k in _ANSWER_KEYS:
            if src.get(k) not in (None, "", [], {}):
                answers = _normalize_answers(src.get(k))
                if answers:
                    break
        if answers:
            break
    entry = {
        "membership_id": m.get("id"),
        "user_id": u.get("id"),
        "name": name or u.get("name"),
        "handle": u.get("name"),
        "role": m.get("role"),
        "join_answers": answers,
        "high_risk_score": _first_present(u.get("highRiskScore"), m.get("highRiskScore"),
                                          mmd.get("highRiskScore"), umd.get("highRiskScore")),
        "requested_at": _first_present(m.get("requestedAt"), m.get("createdAt"),
                                       m.get("created_at"), mmd.get("requestedAt"),
                                       u.get("createdAt")),
    }
    if answers is None:
        # Field names for join answers are unverified. Surface which keys exist
        # (names only, no values) so a wrong guess is visible instead of silent.
        entry["metadata_keys_seen"] = sorted(set(mmd) | set(m) - {"metadata"})[:40]
    return entry


def pending_members_raw(max_pages: int = MAX_PENDING_PAGES) -> dict:
    """All pending requests across pages (bounded), plus tripwire counters."""
    group = _group()
    pp = _page_props(f"/{group}/-/members?t=pending")
    users = _users_of(pp)
    try:
        total_pages = int(pp.get("totalPages") or 1)
    except (TypeError, ValueError):
        total_pages = 1
    last = min(total_pages, max_pages)
    for page in range(2, last + 1):
        users.extend(_users_of(_page_props(f"/{group}/-/members?t=pending&p={page}")))
    pending, excluded = [], 0
    for u in users:
        e = pending_entry(u)
        if (e.get("role") or "").lower() in ACTIVE_ROLES:
            excluded += 1
            continue
        if e.get("membership_id"):
            pending.append(e)
    return {"pending": pending, "excluded_active": excluded,
            "complete": total_pages <= max_pages, "total_pages": total_pages}


def list_pending_members(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 500))
    data = pending_members_raw()
    out = {"pending": data["pending"][:limit], "count": min(len(data["pending"]), limit),
           "total_pending_seen": len(data["pending"])}
    notes = []
    if data["excluded_active"]:
        notes.append(f"{data['excluded_active']} entries with an active-member role were "
                     "dropped. If that is most of the list, Skool may have ignored the "
                     "pending filter: do not approve or decline anything until checked.")
    if not data["complete"]:
        notes.append(f"Only the first {MAX_PENDING_PAGES} of {data['total_pages']} pages "
                     "were read.")
    if not data["pending"]:
        notes.append("No pending requests found. Pending requests are only visible to "
                     "admins of the group.")
    if notes:
        out["notes"] = notes
    return _truncate(out)


def set_member_role(membership_id: str, new_role: str) -> dict:
    """POST /members/{membershipId}/role {"new": "member"|"declined"}.

    "cannot update to same role" means it already happened (someone clicked it in
    the browser, or a retry): reported as already_done, not as a failure.
    """
    mid = _safe_id(membership_id, "membership_id")
    if new_role not in ("member", "declined"):
        raise ValueError(f"Unsupported role {new_role!r}.")
    try:
        r = _api_post(f"/members/{mid}/role", {"new": new_role}, resend_on_error=False)
    except SkoolAPIError as e:
        if "same role" in e.body.lower():
            return {"status": "already_done", "detail": _clip(e.body, 200)}
        raise
    if "same role" in json.dumps(r).lower():
        return {"status": "already_done", "detail": _clip(json.dumps(r), 200)}
    return {"status": "ok", "response": r}


# --- posts and comments (a comment is a post) ---------------------------------
POST_UPDATE_FIELDS = ("title", "content", "attachments", "labels",
                      "video_links", "video_ids")


def get_post_by_id(post_id: str) -> dict:
    """GET /posts/{id}. Unwraps {post: {...}} / {post_tree: {post: {...}}} if the
    response is wrapped; returns the post object itself."""
    pid = _safe_id(post_id, "post_id")
    d = _api_get(f"/posts/{pid}")
    if not isinstance(d, dict):
        raise RuntimeError(f"Unexpected response for post {pid}.")
    post = d.get("post") if isinstance(d.get("post"), dict) else None
    if post is None and isinstance(d.get("post_tree"), dict):
        post = d["post_tree"].get("post")
    post = post if isinstance(post, dict) else d
    if not post.get("id") and not isinstance(post.get("metadata"), dict):
        raise RuntimeError(f"Post {pid} came back in an unrecognized shape.")
    return post


def build_post_update(current: dict, title=None, content=None) -> dict:
    """The FULL flat body for POST /posts/{id}/update.

    Skool replaces the post with exactly what is sent: any field left out is
    CLEARED (attachments, labels, videos vanish). So every field is carried over
    from the freshly fetched post and only title/content are overridden. None =
    keep current; "" is a real value (clears it).
    """
    md = current.get("metadata") or {}
    body = {}
    for f in POST_UPDATE_FIELDS:
        v = md.get(f)
        body[f] = "" if v is None else v
    if title is not None:
        body["title"] = title
    if content is not None:
        body["content"] = content
    return body


def update_post(post_id: str, body: dict) -> dict:
    """POST /posts/{id}/update (PATCH and PUT return 405). Works for comments too."""
    pid = _safe_id(post_id, "post_id")
    missing = [f for f in POST_UPDATE_FIELDS if f not in body]
    if missing:
        raise ValueError(f"Refusing a partial post update (missing {missing}): Skool "
                         "clears every field that is not sent.")
    return _api_post(f"/posts/{pid}/update", body, resend_on_error=False)


def delete_post(post_id: str) -> dict:
    """DELETE /posts/{id} with body {}. Deletes the post AND its comments."""
    pid = _safe_id(post_id, "post_id")
    return _api_delete(f"/posts/{pid}", empty_json_body=True)


def comment_payload(root_id: str, parent_id: str, content: str) -> dict:
    return {"post_type": "comment", "group_id": _group_id(),
            "root_id": root_id, "parent_id": parent_id,
            "metadata": {"title": "", "content": content,
                         "attachments": "", "action": 0, "video_ids": ""}}


def reply_to_comment(post_id_root: str, parent_comment_id: str, content: str) -> dict:
    """A reply is a comment whose parent is another comment: root_id = the post,
    parent_id = the comment being replied to. Same endpoint as comment_on_post."""
    root = _safe_id(post_id_root, "post_id_root")
    parent = _safe_id(parent_comment_id, "parent_comment_id")
    r = _api_post("/posts", comment_payload(root, parent, content),
                  params={"follow": "false"}, resend_on_error=False)
    return {"created": True, "id": r.get("id") if isinstance(r, dict) else None}


# --- calendar -----------------------------------------------------------------
LOCATION_TYPES = {1: "Zoom", 2: "Google Meet", 3: "Address", 4: "Link",
                  5: "Skool call", 6: "Webinar"}
EVENT_TITLE_MAX = 30
EVENT_DESCRIPTION_MAX = 300
# Field names that might mark a recurring event. Unverified against a live
# recurring event, so this errs wide: any of them truthy => treated as recurring.
_RECURRING_KEYS = ("recurring", "recurrence", "rrule", "recurring_rule",
                   "recurrence_rule", "repeat", "recurring_event_id", "series_id",
                   "recurring_id", "parent_event_id")


def _parse_iso_with_offset(value, field: str) -> datetime:
    s = str(value or "").strip()
    if not s:
        raise ValueError(f"{field} is required (ISO 8601 with a UTC offset, e.g. "
                         "2026-12-01T18:00:00-08:00).")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"{field} {value!r} is not ISO 8601 "
                         "(e.g. 2026-12-01T18:00:00-08:00).") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field} {value!r} has no UTC offset. Add one, e.g. "
                         "2026-12-01T18:00:00-08:00, so the time is unambiguous.")
    return dt


def _to_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_timezone(tz) -> str:
    tz = str(tz or "").strip()
    if not tz or len(tz) > 64 or " " in tz:
        raise ValueError(f"timezone {tz!r} must be an IANA name like America/Vancouver.")
    try:
        import zoneinfo
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            # Only reject when a tz database exists to check against. Windows
            # without the tzdata package has none, and that is not the member's
            # fault, so the name is passed through to Skool instead.
            if zoneinfo.available_timezones():
                raise ValueError(f"timezone {tz!r} is not a known IANA name "
                                 "(e.g. America/Vancouver, Europe/London).") from None
    except ImportError:
        pass
    return tz


def _validate_location(location_type, location_info) -> tuple:
    try:
        lt = int(location_type)
    except (TypeError, ValueError):
        raise ValueError(f"location_type must be one of {LOCATION_TYPES}.") from None
    if lt not in LOCATION_TYPES:
        raise ValueError(f"location_type {location_type!r} must be one of {LOCATION_TYPES}.")
    info = str(location_info or "").strip()
    if lt in (1, 2, 3, 4) and not info:
        raise ValueError(f"location_info is required for {LOCATION_TYPES[lt]} "
                         "(the meeting link or the address).")
    return lt, info


def _validate_privacy(privacy_type) -> int:
    try:
        pt = int(privacy_type)
    except (TypeError, ValueError):
        raise ValueError("privacy_type must be an integer (0 = everyone in the group).") from None
    if pt < 0:
        raise ValueError("privacy_type must be 0 or higher (0 = everyone in the group).")
    return pt


def _validate_title(title) -> str:
    t = str(title or "").strip()
    if not t:
        raise ValueError("title is required.")
    if len(t) > EVENT_TITLE_MAX:
        raise ValueError(f"title is {len(t)} characters; Skool allows {EVENT_TITLE_MAX}.")
    return t


def _validate_description(description) -> str:
    d = str(description or "")
    if len(d) > EVENT_DESCRIPTION_MAX:
        raise ValueError(f"description is {len(d)} characters; Skool allows "
                         f"{EVENT_DESCRIPTION_MAX}.")
    return d


def build_event_payload(group_id: str, title: str, description: str, start_time: str,
                        end_time: str, timezone_name: str, location_type: int,
                        location_info: str = "", privacy_type: int = 0,
                        reminder_disabled: bool = False) -> dict:
    """Body for POST /calendar-events. location and privacy are JSON STRINGS
    inside metadata, not nested objects — that is how Skool stores them."""
    t = _validate_title(title)
    d = _validate_description(description)
    start = _parse_iso_with_offset(start_time, "start_time")
    end = _parse_iso_with_offset(end_time, "end_time")
    if end <= start:
        raise ValueError("end_time must be after start_time.")
    tz = _validate_timezone(timezone_name)
    lt, info = _validate_location(location_type, location_info)
    pt = _validate_privacy(privacy_type)
    return {
        "group_id": group_id,
        "start_time": _to_utc_z(start),
        "end_time": _to_utc_z(end),
        "metadata": {
            "title": t,
            "description": d,
            "timezone": tz,
            "reminder_disabled": 1 if reminder_disabled else 0,
            "cover_image": "",
            "location": _compact_json({"location_type": lt, "location_info": info}),
            "privacy": _compact_json({"privacy_type": pt}),
        },
    }


def looks_recurring(event: dict) -> bool:
    md = event.get("metadata") or {}
    for src in (event, md):
        for k in _RECURRING_KEYS:
            v = src.get(k)
            if v not in (None, "", 0, False, [], {}, "0", "{}", "null"):
                return True
    return False


def event_summary(ev: dict) -> dict:
    md = ev.get("metadata") or {}
    loc = _decode_json_str(md.get("location"))
    lt = loc.get("location_type")
    return {
        "id": ev.get("id"),
        "title": md.get("title") or ev.get("title"),
        "description": _clip(md.get("description"), 300),
        "start_time": ev.get("start_time") or md.get("start_time"),
        "end_time": ev.get("end_time") or md.get("end_time"),
        "timezone": md.get("timezone"),
        "location_type": lt,
        "location_kind": LOCATION_TYPES.get(lt) if isinstance(lt, int) else None,
        "location_info": loc.get("location_info"),
        "privacy": _decode_json_str(md.get("privacy")) or None,
        "recurring": looks_recurring(ev),
    }


def list_events(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    pp = _page_props(f"/{_group()}/calendar")
    events = (pp.get("renderData") or {}).get("events")
    if events is None:
        events = pp.get("events")
    out = [event_summary(e) for e in (events or []) if isinstance(e, dict)][:limit]
    return _truncate({"events": out, "count": len(out)})


def get_event(event_id: str) -> dict:
    """GET /calendar-events/{id}; unwraps {event: {...}} style wrappers."""
    eid = _safe_id(event_id, "event_id")
    d = _api_get(f"/calendar-events/{eid}")
    if not isinstance(d, dict):
        raise RuntimeError(f"Unexpected response for event {eid}.")
    for k in ("event", "calendar_event", "calendarEvent"):
        if isinstance(d.get(k), dict):
            return d[k]
    return d


def build_event_update(current: dict, group_id: str, *, title=None, description=None,
                       start_time=None, end_time=None, timezone_name=None,
                       location_type=None, location_info=None, privacy_type=None,
                       reminder_disabled=None) -> dict:
    """Full-replace body for PUT /calendar-events/{id}.

    Starts from EVERY metadata key the event already has (unknown ones included)
    and overrides only what was passed, so nothing is dropped by the replace. Only
    the changed fields are validated: an existing value came from Skool itself.
    location/privacy stay byte-identical unless one of their parts changed.
    """
    md = dict(current.get("metadata") or {})
    cur_start = current.get("start_time") or md.get("start_time")
    cur_end = current.get("end_time") or md.get("end_time")
    if (cur_start is None and start_time is None) or (cur_end is None and end_time is None):
        raise ValueError("Could not read this event's current start/end time; pass both "
                         "start_time and end_time so the full replace is complete.")
    start_out, end_out = cur_start, cur_end
    if start_time is not None or end_time is not None:
        # The order check needs both ends. An unchanged end is re-read from the
        # event; if Skool's own value will not parse, ask for both explicitly.
        try:
            start = _parse_iso_with_offset(start_time if start_time is not None else cur_start,
                                           "start_time")
            end = _parse_iso_with_offset(end_time if end_time is not None else cur_end,
                                         "end_time")
        except ValueError as e:
            raise ValueError(f"{e} (When changing one end of an event, pass both "
                             "start_time and end_time if this persists.)") from None
        if end <= start:
            raise ValueError("end_time must be after start_time.")
        if start_time is not None:
            start_out = _to_utc_z(start)
        if end_time is not None:
            end_out = _to_utc_z(end)
    if title is not None:
        md["title"] = _validate_title(title)
    if description is not None:
        md["description"] = _validate_description(description)
    if timezone_name is not None:
        md["timezone"] = _validate_timezone(timezone_name)
    if reminder_disabled is not None:
        md["reminder_disabled"] = 1 if reminder_disabled else 0
    if location_type is not None or location_info is not None:
        loc = _decode_json_str(md.get("location"))
        lt, info = _validate_location(
            location_type if location_type is not None else loc.get("location_type"),
            location_info if location_info is not None else loc.get("location_info"))
        loc.update({"location_type": lt, "location_info": info})
        md["location"] = _compact_json(loc)
    if privacy_type is not None:
        priv = _decode_json_str(md.get("privacy"))
        priv["privacy_type"] = _validate_privacy(privacy_type)
        md["privacy"] = _compact_json(priv)
    return {
        "group_id": current.get("group_id") or group_id,
        "start_time": start_out,
        "end_time": end_out,
        "metadata": md,
    }


def create_event(payload: dict) -> dict:
    return _api_post("/calendar-events", payload, resend_on_error=False)


def update_event(event_id: str, payload: dict) -> dict:
    eid = _safe_id(event_id, "event_id")
    return _api_put(f"/calendar-events/{eid}", payload, resend_on_error=False)


def delete_event(event_id: str) -> dict:
    eid = _safe_id(event_id, "event_id")
    return _api_delete(f"/calendar-events/{eid}")


# --- invites (UNVERIFIED endpoint: single public source) -----------------------
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,}$")
MAX_MEMBER_SCAN_PAGES = 30


def normalize_emails(emails) -> tuple:
    """(valid, invalid). Lowercased, trimmed, deduped, sorted."""
    if isinstance(emails, str):
        emails = re.split(r"[,\s;]+", emails)
    valid, invalid = set(), []
    for raw in emails or []:
        e = str(raw or "").strip().lower()
        if not e:
            continue
        if len(e) <= 254 and _EMAIL_RE.match(e):
            valid.add(e)
        else:
            invalid.append(str(raw))
    return sorted(valid), invalid


def _member_email(u: dict):
    m = u.get("member") or {}
    e = (u.get("email") or (m.get("metadata") or {}).get("mbme")
         or (u.get("metadata") or {}).get("email"))
    return str(e).strip().lower() if e else None


def _scan_active_members(max_pages: int = MAX_MEMBER_SCAN_PAGES) -> tuple:
    """(users, complete) from the ACTIVE member list, bounded: a big community
    would otherwise cost one page fetch per ~30 members on every preview and
    every confirm."""
    group = _group()
    pp = _page_props(f"/{group}/-/members")
    users = _users_of(pp)
    try:
        total_pages = int(pp.get("totalPages") or 1)
    except (TypeError, ValueError):
        total_pages = 1
    for page in range(2, min(total_pages, max_pages) + 1):
        users.extend(_users_of(_page_props(f"/{group}/-/members?p={page}")))
    return users, total_pages <= max_pages


def member_emails(max_pages: int = MAX_MEMBER_SCAN_PAGES) -> dict:
    """Emails of current members, as far as the member list exposes them."""
    users, complete = _scan_active_members(max_pages)
    emails = {e for e in (_member_email(u) for u in users) if e}
    return {"emails": emails, "checked": len(users),
            "complete": complete, "any_emails_visible": bool(emails)}


def active_membership_ids(max_pages: int = MAX_MEMBER_SCAN_PAGES) -> dict:
    """Membership ids (users[].member.id) on the ACTIVE member list.

    Decline's second guard: declining an active membership removes the member,
    so a decline is refused if its id shows up here, whatever the pending page
    said. Independent of the pending page's field names, which are unverified.
    """
    users, complete = _scan_active_members(max_pages)
    ids = {(u.get("member") or {}).get("id") for u in users} - {None, ""}
    return {"ids": ids, "checked": len(users), "complete": complete}


def course_ids() -> set:
    pp = _page_props(f"/{_group()}/classroom")
    courses = pp.get("allCourses") or (pp.get("renderData") or {}).get("allCourses") or []
    return {c.get("id") for c in courses if isinstance(c, dict) and c.get("id")}


def invite_members(emails: list, course_id_list: list) -> dict:
    """POST /groups/{slug}/invite {"emails": [...], "course_ids": [...]}.
    UNVERIFIED: single public source, never live-tested by this project."""
    return _api_post(f"/groups/{_group()}/invite",
                     {"emails": list(emails), "course_ids": list(course_id_list)},
                     resend_on_error=False)
