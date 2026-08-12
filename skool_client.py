"""
Skool API client.

Skool has no public API, so this talks to Skool's internal surface using your
logged-in session cookie:
  - api2.skool.com   — JSON API (posts, comments, members preview, DMs, writes)
  - www.skool.com    — Next.js pages; list views (feed, members, classroom) are
                       read from the embedded __NEXT_DATA__ JSON blob.

Auth is the full Cookie header captured by connect_skool.py (includes auth_token
and the aws-waf-token that clears AWS's firewall). The waf token lasts ~4 days;
refresh_cookie.py re-mints it automatically if the background job is installed,
otherwise re-run connect_skool.py. All HTTP/parsing lives here so server.py stays
a thin list of tools.
"""
import json
import os
import re

import httpx
from dotenv import load_dotenv

DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(DIR, ".env"))

API = os.environ.get("SKOOL_API", "https://api2.skool.com")
WWW = "https://www.skool.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CHARACTER_LIMIT = 25000
_GID_CACHE = {}


class AuthError(RuntimeError):
    """Raised when the cookie is missing or rejected (usually expired)."""


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
    return {"Cookie": _cookie(), "content-type": "application/json",
            "accept": "application/json", "user-agent": UA}


# --- transport ----------------------------------------------------------------
def _handle(r: httpx.Response, where: str):
    if r.status_code in (401, 403):
        raise AuthError(
            "Skool rejected the request — your cookie has almost certainly expired "
            "(the anti-bot token lasts ~4 days). Run `uv run connect_skool.py` to reconnect, "
            "or `uv run doctor.py` to see exactly what's wrong."
        )
    if r.status_code == 404:
        raise RuntimeError(f"Not found: {where} (bad id/slug?).")
    if r.status_code == 429:
        raise RuntimeError("Rate limited by Skool — wait a bit and retry.")
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:CHARACTER_LIMIT]}


def _api_get(path: str, params: dict | None = None):
    with httpx.Client(timeout=40) as c:
        r = c.get(API + path, headers=_headers(), params=params or {})
    return _handle(r, path)


def _api_post(path: str, payload=None, params: dict | None = None):
    with httpx.Client(timeout=40) as c:
        r = c.post(API + path, headers=_headers(), params=params or {},
                   json=payload if payload is not None else {})
    return _handle(r, path)


def _api_put(path: str, payload=None):
    with httpx.Client(timeout=40) as c:
        r = c.put(API + path, headers=_headers(), json=payload or {})
    return _handle(r, path)


def _page_props(path: str) -> dict:
    """Fetch a www.skool.com page and return props.pageProps from __NEXT_DATA__."""
    with httpx.Client(timeout=40, follow_redirects=True) as c:
        r = c.get(WWW + path, headers={**_headers(), "accept": "text/html"})
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
    payload = {"post_type": "comment", "group_id": _group_id(),
               "root_id": post_id, "parent_id": post_id,
               "metadata": {"title": "", "content": content,
                            "attachments": "", "action": 0, "video_ids": ""}}
    r = _api_post("/posts", payload, params={"follow": "false"})
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
