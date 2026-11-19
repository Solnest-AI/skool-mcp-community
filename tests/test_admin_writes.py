"""Offline tests for the admin write tools and their safety layer.

No network, ever: an autouse fixture replaces skool_client._request with a fake
that fails the test on any call it was not told to expect. That matters more than
usual here, because importing skool_client loads the developer's real .env.

Covers: write switch + registration, confirm-token flow, audit log, rate caps and
the write floor, decline refusing non-pending ids, edit_post full-body merge,
calendar location/privacy JSON-string encoding, invite caps/validation, and the
stale-cookie reload (including the refresh_cookie.verify regression it must not
introduce).
"""
import json
import os
import py_compile
from types import SimpleNamespace

import httpx
import pytest

import admin_tools
import doctor
import skool_client as sk
import write_guard as guard
from write_guard import WriteRefused

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_REQUEST = sk._request  # captured before the autouse fixture stubs it
GROUP = "test-group"
COOKIE = "auth_token=COOKIEVALUE1234567890; aws-waf-token=WAFTOKEN0987654321"
T0 = 1_800_000_000.0


# --- fakes ------------------------------------------------------------------------
def _resp(status, body, method="GET", url="https://x"):
    req = httpx.Request(method, url)
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, request=req)
    return httpx.Response(status, text=body or "", request=req)


class FakeSkool:
    """Stands in for skool_client._request. Routes www pages and api2 calls."""

    def __init__(self):
        self.calls = []
        self.pages = {}   # www path incl. query -> pageProps dict
        self.api = {}     # (METHOD, path) -> (status, body) | callable(call)

    def __call__(self, method, url, *, headers, params=None, json_body=None,
                 raw_body=None, follow_redirects=False, timeout=40,
                 resend_on_error=True):
        call = SimpleNamespace(method=method.upper(), url=url, params=params,
                               json=json_body, raw=raw_body, headers=headers,
                               resend=resend_on_error)
        self.calls.append(call)
        if url.startswith(sk.WWW):
            path = url[len(sk.WWW):]
            if path not in self.pages:
                raise AssertionError(f"unexpected page fetch: {path}")
            blob = json.dumps({"props": {"pageProps": self.pages[path]}})
            html = f'<html><script id="__NEXT_DATA__" type="application/json">{blob}</script></html>'
            return _resp(200, html, call.method, url)
        path = url[len(sk.API):]
        handler = self.api.get((call.method, path))
        if handler is None:
            raise AssertionError(f"unexpected API call: {call.method} {path}")
        if callable(handler):
            return handler(call)
        status, body = handler
        return _resp(status, body, call.method, url)

    def writes(self):
        return [c for c in self.calls if c.method != "GET"]


class FakeMCP:
    def __init__(self):
        self.names = []

    def tool(self):
        def deco(fn):
            self.names.append(fn.__name__)
            return fn
        return deco


@pytest.fixture(autouse=True)
def fake(monkeypatch, tmp_path):
    f = FakeSkool()
    monkeypatch.setattr(sk, "_request", f)
    monkeypatch.setenv("SKOOL_WRITE_TOOLS", "1")
    monkeypatch.setenv("SKOOL_GROUP", GROUP)
    monkeypatch.setenv("SKOOL_GROUP_ID", "gid123")
    monkeypatch.setenv("SKOOL_COOKIE", COOKIE)
    for var in ("SKOOL_CAP_MEMBERS_PER_HOUR", "SKOOL_CAP_MEMBERS_PER_DAY",
                "SKOOL_CAP_INVITES_PER_DAY", "SKOOL_CAP_INVITES_PER_CALL",
                "SKOOL_CAP_CALENDAR_PER_HOUR", "SKOOL_CAP_POSTS_PER_HOUR",
                "SKOOL_WRITE_MIN_INTERVAL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(guard, "AUDIT_LOG", str(tmp_path / "logs" / "audit.jsonl"))
    clock = {"t": T0, "sleeps": []}

    def fake_sleep(s):
        clock["sleeps"].append(s)
        clock["t"] += s

    monkeypatch.setattr(guard, "_now", lambda: clock["t"])
    monkeypatch.setattr(guard, "_sleep", fake_sleep)
    monkeypatch.setattr(guard, "_last_attempt_t", 0.0)
    guard._tokens.clear()
    f.clock = clock
    yield f
    guard._tokens.clear()


def _audit_lines():
    try:
        with open(guard.AUDIT_LOG, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def _pending_user(mid, uid, first, last, role=None, survey=None, risk=None):
    m = {"id": mid, "createdAt": "2026-11-18T10:00:00Z", "metadata": {}}
    if role:
        m["role"] = role
    if survey is not None:
        m["metadata"]["survey"] = survey
    u = {"id": uid, "name": f"{first}-{last}".lower(), "firstName": first,
         "lastName": last, "member": m}
    if risk is not None:
        u["highRiskScore"] = risk
    return u


ACTIVE_PAGE = {"users": [{"id": "ua", "firstName": "Active", "lastName": "Member",
                           "member": {"id": "active-1", "role": "member"}}],
               "totalPages": 1}


def _with_pending(fake, *users, total_pages=1):
    fake.pages[f"/{GROUP}/-/members?t=pending"] = {"users": list(users),
                                                   "totalPages": total_pages}
    # Decline cross-checks the ACTIVE member list too.
    fake.pages.setdefault(f"/{GROUP}/-/members", ACTIVE_PAGE)


def _confirm(tool, *args, **kwargs):
    prev = tool(*args, **kwargs)
    assert prev["preview"] is True and prev["executed"] is False
    return tool(*args, confirm=True, confirm_token=prev["confirm_token"], **kwargs)


# --- compile + registration --------------------------------------------------------
def test_modules_compile():
    for name in ("server.py", "skool_client.py", "admin_tools.py", "write_guard.py",
                 "doctor.py"):
        py_compile.compile(os.path.join(ROOT, name), doraise=True)


def test_write_tools_are_not_registered_when_the_switch_is_off(monkeypatch):
    monkeypatch.delenv("SKOOL_WRITE_TOOLS", raising=False)
    m = FakeMCP()
    names = admin_tools.register(m)
    assert names == m.names == ["skool_list_pending_members", "skool_list_events"]


def test_all_write_tools_register_when_the_switch_is_on():
    m = FakeMCP()
    admin_tools.register(m)
    assert len(m.names) == 11
    for expected in ("skool_approve_member", "skool_decline_member",
                     "skool_reply_to_comment", "skool_edit_post", "skool_delete_post",
                     "skool_create_event", "skool_update_event", "skool_delete_event",
                     "skool_invite_members"):
        assert expected in m.names


def _sdk_tools(monkeypatch, writes_on: bool) -> dict:
    """Register through the REAL MCP SDK by reloading server.py. A signature the
    SDK cannot turn into a JSON schema would crash server start for every member,
    which the FakeMCP tests above cannot catch."""
    pytest.importorskip("mcp")
    import asyncio
    import importlib
    if writes_on:
        monkeypatch.setenv("SKOOL_WRITE_TOOLS", "1")
    else:
        monkeypatch.delenv("SKOOL_WRITE_TOOLS", raising=False)
    import server
    server = importlib.reload(server)
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_real_sdk_registers_all_24_tools_with_schemas_when_on(monkeypatch):
    tools = _sdk_tools(monkeypatch, writes_on=True)
    assert len(tools) == 24
    for name in [f.__name__ for f in admin_tools.WRITE_TOOLS]:
        t = tools[name]
        schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
        assert "confirm" in schema["properties"] and "confirm_token" in schema["properties"]
        assert "confirm_token" in (t.description or ""), name
    inv = getattr(tools["skool_invite_members"], "inputSchema", None) or \
        getattr(tools["skool_invite_members"], "input_schema")
    assert inv["required"] == ["emails"]


def test_real_sdk_registers_only_15_tools_when_off(monkeypatch):
    tools = _sdk_tools(monkeypatch, writes_on=False)
    assert len(tools) == 15
    assert "skool_list_pending_members" in tools and "skool_approve_member" not in tools


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("YES", True),
                                            ("on", True), ("0", False), ("", False),
                                            ("enabled", False)])
def test_write_switch_values(value, expected):
    assert guard.writes_enabled({"SKOOL_WRITE_TOOLS": value}) is expected


def test_run_refuses_when_the_switch_is_off_even_if_called_directly(monkeypatch, fake):
    monkeypatch.delenv("SKOOL_WRITE_TOOLS", raising=False)
    with pytest.raises(WriteRefused, match="turned off"):
        admin_tools.skool_approve_member("m1")
    assert fake.calls == []


def test_every_write_tool_has_a_description_with_the_two_step_rule():
    """A `\"\"\"doc\"\"\" + X` expression is not a docstring; the MCP client would
    show these tools with no description at all."""
    for fn in admin_tools.WRITE_TOOLS:
        doc = fn.__doc__ or ""
        assert len(doc) > 200, fn.__name__
        assert "confirm_token" in doc and "preview" in doc, fn.__name__


# --- confirm-token flow --------------------------------------------------------------
def test_preview_never_writes_and_returns_the_exact_request(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    prev = admin_tools.skool_approve_member("m1")
    assert fake.writes() == []
    assert prev["request"] == {"method": "POST",
                               "url": f"{sk.API}/members/m1/role",
                               "body": {"new": "member"}}
    assert "Jane Doe" in prev["summary"]
    assert len(prev["confirm_token"]) == 24
    assert prev["expires_in_seconds"] == 600


def test_confirm_with_the_token_executes_exactly_once(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    fake.api[("POST", "/members/m1/role")] = (200, {"ok": True})
    prev = admin_tools.skool_approve_member("m1")
    res = admin_tools.skool_approve_member("m1", confirm=True,
                                           confirm_token=prev["confirm_token"])
    assert res["executed"] is True and res["status"] == "ok"
    assert [c.json for c in fake.writes()] == [{"new": "member"}]
    with pytest.raises(WriteRefused, match="already-used"):
        admin_tools.skool_approve_member("m1", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert len(fake.writes()) == 1


def test_previewing_twice_cannot_execute_twice(fake):
    fake.api[("GET", "/posts/c1")] = (200, {"id": "c1", "post_type": "comment",
                                            "root_id": "p1", "parent_id": "p1",
                                            "metadata": {}})
    fake.api[("POST", "/posts")] = (200, {"id": "r1"})
    first = admin_tools.skool_reply_to_comment("p1", "c1", "Thanks!")
    second = admin_tools.skool_reply_to_comment("p1", "c1", "Thanks!")
    admin_tools.skool_reply_to_comment("p1", "c1", "Thanks!", confirm=True,
                                       confirm_token=first["confirm_token"])
    with pytest.raises(WriteRefused, match="already-used"):
        admin_tools.skool_reply_to_comment("p1", "c1", "Thanks!", confirm=True,
                                           confirm_token=second["confirm_token"])
    assert len(fake.writes()) == 1


def test_confirm_without_a_token_is_refused(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    with pytest.raises(WriteRefused, match="preview"):
        admin_tools.skool_approve_member("m1", confirm=True)
    assert fake.writes() == []
    assert _audit_lines()[-1]["event"] == "refused"


def test_token_is_bound_to_the_arguments(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"),
                  _pending_user("m2", "u2", "Sam", "Roe"))
    prev = admin_tools.skool_approve_member("m1")
    with pytest.raises(WriteRefused, match="arguments changed"):
        admin_tools.skool_approve_member("m2", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert fake.writes() == []


def test_token_is_bound_to_the_tool(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    prev = admin_tools.skool_approve_member("m1")
    with pytest.raises(WriteRefused, match="issued for skool_approve_member"):
        admin_tools.skool_decline_member("m1", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert fake.writes() == []


def test_token_is_bound_to_the_group(fake, monkeypatch):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    prev = admin_tools.skool_approve_member("m1")
    monkeypatch.setenv("SKOOL_GROUP", "other-group")
    with pytest.raises(WriteRefused, match="arguments changed"):
        admin_tools.skool_approve_member("m1", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert fake.writes() == []


def test_token_expires_after_ten_minutes(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    prev = admin_tools.skool_approve_member("m1")
    fake.clock["t"] += 601
    with pytest.raises(WriteRefused, match="expired"):
        admin_tools.skool_approve_member("m1", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert fake.writes() == []


def test_a_made_up_token_is_refused(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    admin_tools.skool_approve_member("m1")
    with pytest.raises(WriteRefused, match="Unknown"):
        admin_tools.skool_approve_member("m1", confirm=True, confirm_token="0" * 24)
    assert fake.writes() == []


# --- audit log ---------------------------------------------------------------------
def test_audit_log_has_attempt_and_result_and_never_the_cookie_or_a_long_body(fake):
    fake.api[("GET", "/posts/c1")] = (200, {"id": "c1", "post_type": "comment",
                                            "root_id": "p1", "parent_id": "p1",
                                            "metadata": {"content": "top comment"}})
    fake.api[("POST", "/posts")] = (200, {"id": "r1"})
    long_body = "x" * 500
    res = _confirm(admin_tools.skool_reply_to_comment, "p1", "c1", long_body)
    assert res["executed"] is True
    raw = open(guard.AUDIT_LOG, encoding="utf-8").read()
    assert "COOKIEVALUE1234567890" not in raw and "WAFTOKEN0987654321" not in raw
    assert long_body not in raw
    lines = _audit_lines()
    assert [r["event"] for r in lines] == ["attempt", "result"]
    assert lines[1]["status"] == "ok"
    assert lines[0]["targets"] == ["p1", "c1"] and lines[0]["group"] == GROUP
    for r in lines:
        assert {"ts", "tool", "group", "event"} <= set(r)
        assert len(r.get("detail", "")) <= 200


def test_audit_scrubs_the_cookie_from_error_text(fake):
    rec = guard.audit("result", "t", GROUP, status="error",
                      detail="boom COOKIEVALUE1234567890 boom")
    assert "COOKIEVALUE" not in json.dumps(rec)


def test_a_failed_write_is_logged_as_an_error(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    fake.api[("POST", "/members/m1/role")] = (500, "server exploded")
    with pytest.raises(sk.SkoolAPIError):
        _confirm(admin_tools.skool_approve_member, "m1")
    assert [(r["event"], r.get("status")) for r in _audit_lines()] == [
        ("attempt", None), ("result", "error")]


def test_an_unwritable_audit_log_refuses_the_write(fake, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not a directory")
    monkeypatch.setattr(guard, "AUDIT_LOG", str(blocker / "audit.jsonl"))
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    with pytest.raises(WriteRefused, match="audit"):
        _confirm(admin_tools.skool_approve_member, "m1")
    assert fake.writes() == []


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="POSIX permission bits; root ignores them")
def test_a_read_only_audit_log_refuses_at_confirm(fake, tmp_path):
    """Readable (so the preview and caps work) but not appendable: the write must
    still be refused, because it could not be recorded."""
    os.makedirs(os.path.dirname(guard.AUDIT_LOG), exist_ok=True)
    open(guard.AUDIT_LOG, "w").close()
    os.chmod(guard.AUDIT_LOG, 0o400)
    try:
        _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
        prev = admin_tools.skool_approve_member("m1")
        with pytest.raises(WriteRefused, match="not writable"):
            admin_tools.skool_approve_member("m1", confirm=True,
                                             confirm_token=prev["confirm_token"])
        assert fake.writes() == []
    finally:
        os.chmod(guard.AUDIT_LOG, 0o600)


# --- rate caps and the floor ------------------------------------------------------
def test_member_hourly_cap_blocks_then_releases(fake, monkeypatch):
    monkeypatch.setenv("SKOOL_CAP_MEMBERS_PER_HOUR", "1")
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"),
                  _pending_user("m2", "u2", "Sam", "Roe"))
    fake.api[("POST", "/members/m1/role")] = (200, {})
    fake.api[("POST", "/members/m2/role")] = (200, {})
    _confirm(admin_tools.skool_approve_member, "m1")
    prev = admin_tools.skool_approve_member("m2")
    assert any("Rate cap" in w for w in prev["warnings"])
    with pytest.raises(WriteRefused, match="Rate cap reached for members"):
        admin_tools.skool_approve_member("m2", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert len(fake.writes()) == 1
    fake.clock["t"] += 3601
    _confirm(admin_tools.skool_approve_member, "m2")
    assert len(fake.writes()) == 2


def test_member_daily_cap_counts_a_full_day(fake):
    for i in range(200):
        guard.audit("attempt", "skool_approve_member", GROUP, bucket="members",
                    units=1, now=T0 - 80000 + i)
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    with pytest.raises(WriteRefused, match="last day, limit 200"):
        _confirm(admin_tools.skool_approve_member, "m1")
    assert fake.writes() == []


def test_usage_windows_and_torn_lines(fake):
    guard.audit("attempt", "x", GROUP, bucket="calendar", units=1, now=T0 - 3700)
    guard.audit("attempt", "x", GROUP, bucket="calendar", units=1, now=T0 - 10)
    guard.audit("attempt", "x", GROUP, bucket="posts", units=1, now=T0 - 10)
    guard.audit("refused", "x", GROUP, bucket="calendar", now=T0 - 5)
    with open(guard.AUDIT_LOG, "a", encoding="utf-8") as fh:
        fh.write("{torn line\n")
    assert guard.usage("calendar")["hour"] == {"used": 1, "limit": 10}


def test_invalid_cap_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("SKOOL_CAP_POSTS_PER_HOUR", "lots")
    assert guard.cap_limit("posts", "hour") == 20
    monkeypatch.setenv("SKOOL_CAP_POSTS_PER_HOUR", "-3")
    assert guard.cap_limit("posts", "hour") == 20
    monkeypatch.setenv("SKOOL_CAP_POSTS_PER_HOUR", "50")
    assert guard.cap_limit("posts", "hour") == 50


def test_writes_are_at_least_one_second_apart(fake, monkeypatch):
    monkeypatch.setenv("SKOOL_WRITE_MIN_INTERVAL", "0.1")  # cannot go below 1s
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"),
                  _pending_user("m2", "u2", "Sam", "Roe"))
    fake.api[("POST", "/members/m1/role")] = (200, {})
    fake.api[("POST", "/members/m2/role")] = (200, {})
    _confirm(admin_tools.skool_approve_member, "m1")
    _confirm(admin_tools.skool_approve_member, "m2")
    assert fake.clock["sleeps"] == [pytest.approx(1.0)]


# --- members -----------------------------------------------------------------------
def test_pending_list_uses_membership_id_and_parses_answers(fake):
    survey = json.dumps({"survey": [{"question": "Why join?", "answer": "To automate"}]})
    _with_pending(fake, _pending_user("mem-1", "user-1", "Jane", "Doe", survey=survey,
                                      risk=7))
    out = sk.list_pending_members()
    (e,) = out["pending"]
    assert e["membership_id"] == "mem-1" and e["user_id"] == "user-1"
    assert e["name"] == "Jane Doe"
    assert e["join_answers"] == [{"question": "Why join?", "answer": "To automate"}]
    assert e["high_risk_score"] == 7
    assert e["requested_at"] == "2026-11-18T10:00:00Z"


def test_pending_users_null_is_an_empty_list(fake):
    fake.pages[f"/{GROUP}/-/members?t=pending"] = {"users": None}
    out = sk.list_pending_members()
    assert out["pending"] == [] and out["count"] == 0
    assert any("No pending requests" in n for n in out["notes"])


def test_decline_refuses_an_id_that_is_not_pending(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    with pytest.raises(WriteRefused, match="not in the current pending list"):
        admin_tools.skool_decline_member("m999")
    assert fake.writes() == []


def test_decline_refuses_a_user_id_passed_as_a_membership_id(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    with pytest.raises(WriteRefused, match="user id instead of a membership id"):
        admin_tools.skool_decline_member("u1")


def test_decline_rechecks_pending_on_confirm(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    prev = admin_tools.skool_decline_member("m1")
    _with_pending(fake)  # approved in the browser between preview and confirm
    with pytest.raises(WriteRefused, match="not in the current pending list"):
        admin_tools.skool_decline_member("m1", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert fake.writes() == []
    assert _audit_lines()[-1]["event"] == "refused"


def test_decline_refuses_when_the_pending_list_shows_active_members(fake):
    """Tripwire: if Skool ever ignored ?t=pending, a decline would remove a member."""
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"),
                  _pending_user("m2", "u2", "Active", "Person", role="member"))
    with pytest.raises(WriteRefused, match="REMOVES the member"):
        admin_tools.skool_decline_member("m1")
    assert fake.writes() == []


def test_decline_refuses_an_id_that_is_on_the_active_member_list(fake):
    """Second guard, independent of the pending page's field names: even if an
    active member somehow shows up on the pending page with no role, it is not
    declined (that would remove them)."""
    _with_pending(fake, _pending_user("active-1", "ua", "Active", "Member"))
    with pytest.raises(WriteRefused, match="ACTIVE member list"):
        admin_tools.skool_decline_member("active-1")
    assert fake.writes() == []


def test_decline_rechecks_the_active_list_on_confirm(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    prev = admin_tools.skool_decline_member("m1")
    fake.pages[f"/{GROUP}/-/members"] = {"users": [{"id": "u1", "member": {"id": "m1"}}],
                                         "totalPages": 1}
    with pytest.raises(WriteRefused, match="ACTIVE member list"):
        admin_tools.skool_decline_member("m1", confirm=True,
                                         confirm_token=prev["confirm_token"])
    assert fake.writes() == []


def test_decline_warns_when_the_active_list_cannot_be_cross_checked(fake):
    fake.pages[f"/{GROUP}/-/members"] = {"users": [{"id": "u9"}], "totalPages": 1}
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    prev = admin_tools.skool_decline_member("m1")
    assert any("Could not cross-check" in w for w in prev["warnings"])


def test_approve_does_not_need_the_active_list(fake):
    fake.pages[f"/{GROUP}/-/members?t=pending"] = {
        "users": [_pending_user("m1", "u1", "Jane", "Doe")], "totalPages": 1}
    prev = admin_tools.skool_approve_member("m1")
    assert prev["preview"] is True
    assert not any(c.url.endswith("/-/members") for c in fake.calls)


def test_decline_sends_declined(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    fake.api[("POST", "/members/m1/role")] = (200, {})
    res = _confirm(admin_tools.skool_decline_member, "m1")
    assert res["status"] == "ok"
    assert fake.writes()[0].json == {"new": "declined"}


def test_same_role_error_is_already_done(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    fake.api[("POST", "/members/m1/role")] = (400, '{"error":"cannot update to same role"}')
    res = _confirm(admin_tools.skool_approve_member, "m1")
    assert res["executed"] is True and res["status"] == "already_done"
    assert _audit_lines()[-1]["status"] == "already_done"


# --- posts and comments ------------------------------------------------------------
def _post(**md):
    base = {"title": "Old title", "content": "Old body", "attachments": "att1,att2",
            "labels": "lab1", "video_links": '["https://video"]', "video_ids": "vid1",
            "comments": 3}
    base.update(md)
    return {"id": "p1", "post_type": "post", "root_id": "", "metadata": base}


def test_edit_post_sends_the_full_body_and_keeps_untouched_fields(fake):
    fake.api[("GET", "/posts/p1")] = (200, {"post": _post()})
    fake.api[("POST", "/posts/p1/update")] = (200, {"ok": True})
    prev = admin_tools.skool_edit_post("p1", content="New body")
    assert prev["warnings"] == []
    assert prev["details"]["changing"] == ["content"]
    _confirm(admin_tools.skool_edit_post, "p1", content="New body")
    assert fake.writes()[-1].json == {
        "title": "Old title", "content": "New body", "attachments": "att1,att2",
        "labels": "lab1", "video_links": '["https://video"]', "video_ids": "vid1"}


def test_edit_post_merges_from_a_fresh_read_on_confirm(fake):
    state = {"post": _post()}
    fake.api[("GET", "/posts/p1")] = lambda call: _resp(200, state)
    fake.api[("POST", "/posts/p1/update")] = (200, {})
    prev = admin_tools.skool_edit_post("p1", title="New title")
    state["post"] = _post(attachments="att3")  # someone added an attachment meanwhile
    admin_tools.skool_edit_post("p1", title="New title", confirm=True,
                                confirm_token=prev["confirm_token"])
    body = fake.writes()[-1].json
    assert body["attachments"] == "att3" and body["title"] == "New title"


def test_edit_post_warns_about_fields_it_could_not_find(fake):
    post = _post()
    del post["metadata"]["video_links"], post["metadata"]["labels"]
    fake.api[("GET", "/posts/p1")] = (200, post)  # unwrapped shape works too
    prev = admin_tools.skool_edit_post("p1", content="New")
    assert any("cleared" in w and "video_links" in w for w in prev["warnings"])
    assert prev["request"]["body"]["video_links"] == ""


def test_edit_post_with_no_real_change_is_refused(fake):
    fake.api[("GET", "/posts/p1")] = (200, {"post": _post()})
    with pytest.raises(WriteRefused, match="nothing to edit"):
        admin_tools.skool_edit_post("p1", title="Old title")
    with pytest.raises(WriteRefused, match="Nothing to change"):
        admin_tools.skool_edit_post("p1")


def test_update_post_refuses_a_partial_body():
    with pytest.raises(ValueError, match="partial"):
        sk.update_post("p1", {"title": "t", "content": "c"})


def test_delete_post_sends_a_literal_empty_json_body_and_warns(fake):
    fake.api[("GET", "/posts/p1")] = (200, {"post": _post()})
    fake.api[("DELETE", "/posts/p1")] = (200, {})
    prev = admin_tools.skool_delete_post("p1")
    assert any("deletes ALL of its comments" in w for w in prev["warnings"])
    _confirm(admin_tools.skool_delete_post, "p1")
    (call,) = fake.writes()
    assert call.method == "DELETE" and call.raw == b"{}"


def test_delete_post_that_is_already_gone_is_refused(fake):
    fake.api[("GET", "/posts/p1")] = (404, "not found")
    with pytest.raises(WriteRefused, match="not found"):
        admin_tools.skool_delete_post("p1")


def test_reply_payload_uses_post_as_root_and_comment_as_parent(fake):
    fake.api[("GET", "/posts/c1")] = (200, {"id": "c1", "post_type": "comment",
                                            "root_id": "p1", "parent_id": "p1",
                                            "metadata": {"content": "hi"}})
    fake.api[("POST", "/posts")] = (200, {"id": "r1"})
    _confirm(admin_tools.skool_reply_to_comment, "p1", "c1", "Thanks!")
    call = fake.writes()[-1]
    assert call.params == {"follow": "false"}
    assert call.json["post_type"] == "comment" and call.json["group_id"] == "gid123"
    assert call.json["root_id"] == "p1" and call.json["parent_id"] == "c1"
    assert call.json["metadata"]["content"] == "Thanks!"


def test_reply_refuses_a_third_nesting_level(fake):
    fake.api[("GET", "/posts/c2")] = (200, {"id": "c2", "post_type": "comment",
                                            "root_id": "p1", "parent_id": "c1",
                                            "metadata": {}})
    with pytest.raises(WriteRefused, match="third level"):
        admin_tools.skool_reply_to_comment("p1", "c2", "hi")
    assert fake.writes() == []


def test_reply_refuses_a_comment_from_another_post(fake):
    fake.api[("GET", "/posts/c1")] = (200, {"id": "c1", "root_id": "pX",
                                            "parent_id": "pX", "metadata": {}})
    with pytest.raises(WriteRefused, match="belongs to post pX"):
        admin_tools.skool_reply_to_comment("p1", "c1", "hi")


@pytest.mark.parametrize("bad", ["../groups/x", "a/b", "id?x=1", "", " ", "a b"])
def test_ids_that_could_change_the_url_path_are_refused(fake, bad):
    with pytest.raises(WriteRefused):
        admin_tools.skool_delete_post(bad)
    with pytest.raises(ValueError):
        sk.delete_post(bad)
    assert fake.calls == []


# --- calendar ----------------------------------------------------------------------
def test_event_location_and_privacy_are_json_strings():
    p = sk.build_event_payload("gid123", "Office hours", "Weekly Q&A",
                               "2026-12-01T10:00:00-08:00", "2026-12-01T11:00:00-08:00",
                               "America/Vancouver", 4, "https://meet.example/x")
    md = p["metadata"]
    assert isinstance(md["location"], str) and isinstance(md["privacy"], str)
    assert md["location"] == '{"location_type":4,"location_info":"https://meet.example/x"}'
    assert md["privacy"] == '{"privacy_type":0}'
    assert json.loads(md["location"]) == {"location_type": 4,
                                          "location_info": "https://meet.example/x"}
    assert md["reminder_disabled"] == 0 and md["cover_image"] == ""
    assert p["start_time"] == "2026-12-01T18:00:00Z"
    assert p["end_time"] == "2026-12-01T19:00:00Z"
    assert p["group_id"] == "gid123"


def test_reminder_disabled_is_encoded_as_one():
    p = sk.build_event_payload("g", "T", "", "2026-12-01T10:00:00Z",
                               "2026-12-01T11:00:00Z", "UTC", 5, "", reminder_disabled=True)
    assert p["metadata"]["reminder_disabled"] == 1
    assert p["metadata"]["location"] == '{"location_type":5,"location_info":""}'


@pytest.mark.parametrize("kwargs,match", [
    ({"title": "x" * 31}, "allows 30"),
    ({"description": "d" * 301}, "allows 300"),
    ({"start_time": "2026-12-01T10:00:00"}, "no UTC offset"),
    ({"start_time": "next tuesday"}, "not ISO 8601"),
    ({"end_time": "2026-12-01T09:00:00Z"}, "after start_time"),
    ({"location_type": 7}, "location_type"),
    ({"location_type": 1, "location_info": ""}, "required for Zoom"),
    ({"timezone_name": "Pacific Time"}, "IANA"),
])
def test_event_validation(kwargs, match):
    args = {"group_id": "g", "title": "Call", "description": "",
            "start_time": "2026-12-01T10:00:00Z", "end_time": "2026-12-01T11:00:00Z",
            "timezone_name": "UTC", "location_type": 4, "location_info": "https://x"}
    args.update(kwargs)
    with pytest.raises(ValueError, match=match):
        sk.build_event_payload(**args)


def _event(**md):
    base = {"title": "Office hours", "description": "d", "timezone": "America/Vancouver",
            "reminder_disabled": 0, "cover_image": "img.png",
            "location": '{"location_type":4,"location_info":"https://old"}',
            "privacy": '{"privacy_type":0}', "future_key": "keep me"}
    base.update(md)
    return {"id": "e1", "group_id": "gid123", "start_time": "2026-12-01T18:00:00Z",
            "end_time": "2026-12-01T19:00:00Z", "metadata": base}


def test_update_event_is_a_full_replace_that_keeps_everything_else(fake):
    fake.api[("GET", "/calendar-events/e1")] = (200, _event())
    fake.api[("PUT", "/calendar-events/e1")] = (200, {})
    _confirm(admin_tools.skool_update_event, "e1", title="New hours")
    body = fake.writes()[-1].json
    md = body["metadata"]
    assert md["title"] == "New hours"
    assert md["location"] == '{"location_type":4,"location_info":"https://old"}'
    assert md["privacy"] == '{"privacy_type":0}'
    assert md["future_key"] == "keep me" and md["cover_image"] == "img.png"
    assert body["start_time"] == "2026-12-01T18:00:00Z"
    assert body["end_time"] == "2026-12-01T19:00:00Z"
    assert body["group_id"] == "gid123"


def test_update_event_location_change_is_re_encoded_as_a_string(fake):
    payload = sk.build_event_update(_event(), "gid123", location_info="https://new")
    assert payload["metadata"]["location"] == \
        '{"location_type":4,"location_info":"https://new"}'


def test_update_event_moving_the_start_checks_order_against_the_kept_end():
    with pytest.raises(ValueError, match="after start_time"):
        sk.build_event_update(_event(), "g", start_time="2026-12-01T20:00:00Z")


def test_update_event_with_nothing_to_change_is_refused(fake):
    with pytest.raises(WriteRefused, match="Nothing to change"):
        admin_tools.skool_update_event("e1")


def test_delete_event_refuses_recurring_events(fake):
    fake.api[("GET", "/calendar-events/e1")] = (200, _event(recurring='{"freq":"weekly"}'))
    with pytest.raises(WriteRefused, match="recurring"):
        admin_tools.skool_delete_event("e1")
    with pytest.raises(WriteRefused, match="recurring"):
        admin_tools.skool_update_event("e1", title="x")
    assert fake.writes() == []


def test_delete_event_sends_no_body(fake):
    fake.api[("GET", "/calendar-events/e1")] = (200, {"event": _event()})
    fake.api[("DELETE", "/calendar-events/e1")] = (200, {})
    _confirm(admin_tools.skool_delete_event, "e1")
    (call,) = fake.writes()
    assert call.method == "DELETE" and call.raw is None and call.json is None


def test_create_event_posts_the_payload(fake):
    fake.pages[f"/{GROUP}/calendar"] = {"renderData": {"events": []}}
    fake.api[("POST", "/calendar-events")] = (200, {"id": "new1"})
    res = _confirm(admin_tools.skool_create_event, "Office hours",
                   "2026-12-01T10:00:00-08:00", "2026-12-01T11:00:00-08:00",
                   "America/Vancouver", 4, "https://meet.example/x")
    assert res["executed"] is True
    body = fake.writes()[-1].json
    assert body["metadata"]["location"] == \
        '{"location_type":4,"location_info":"https://meet.example/x"}'


def test_create_event_refuses_a_duplicate(fake):
    fake.pages[f"/{GROUP}/calendar"] = {"renderData": {"events": [
        _event(title="Office hours")]}}
    with pytest.raises(WriteRefused, match="duplicate"):
        admin_tools.skool_create_event("Office hours", "2026-12-01T10:00:00-08:00",
                                       "2026-12-01T11:00:00-08:00",
                                       "America/Vancouver", 4, "https://x")


def test_list_events_reads_the_calendar_page(fake):
    fake.pages[f"/{GROUP}/calendar"] = {"renderData": {"events": [_event()]}}
    (ev,) = sk.list_events()["events"]
    assert ev["id"] == "e1" and ev["title"] == "Office hours"
    assert ev["location_kind"] == "Link" and ev["location_info"] == "https://old"
    assert ev["recurring"] is False


# --- invites -----------------------------------------------------------------------
def _invite_pages(fake, member_emails=("taken@example.com",), courses=("c1",)):
    fake.pages[f"/{GROUP}/-/members"] = {
        "users": [{"id": f"u{i}", "email": e} for i, e in enumerate(member_emails)],
        "totalPages": 1}
    fake.pages[f"/{GROUP}/classroom"] = {"allCourses": [{"id": c} for c in courses]}


def test_emails_are_validated_lowercased_and_deduped():
    valid, invalid = sk.normalize_emails(["A@Example.com", "a@example.com ",
                                          "not-an-email", "b@example.co", ""])
    assert valid == ["a@example.com", "b@example.co"]
    assert invalid == ["not-an-email"]


def test_invite_refuses_invalid_emails(fake):
    with pytest.raises(WriteRefused, match="Not valid"):
        admin_tools.skool_invite_members(["ok@example.com", "nope@"])
    assert fake.calls == []


def test_invite_per_call_cap(fake, monkeypatch):
    six = [f"p{i}@example.com" for i in range(6)]
    with pytest.raises(WriteRefused, match="limit is 5 per call"):
        admin_tools.skool_invite_members(six)
    monkeypatch.setenv("SKOOL_CAP_INVITES_PER_CALL", "2")
    with pytest.raises(WriteRefused, match="limit is 2 per call"):
        admin_tools.skool_invite_members(six[:3])
    assert fake.calls == []


def test_invite_daily_cap_counts_emails_not_calls(fake):
    _invite_pages(fake)
    guard.audit("attempt", "skool_invite_members", GROUP, bucket="invites", units=18,
                now=T0 - 60)
    fake.api[("POST", f"/groups/{GROUP}/invite")] = (200, {})
    with pytest.raises(WriteRefused, match="Rate cap reached for invites"):
        _confirm(admin_tools.skool_invite_members,
                 ["a@example.com", "b@example.com", "c@example.com"])
    _confirm(admin_tools.skool_invite_members, ["a@example.com", "b@example.com"])
    assert len(fake.writes()) == 1


def test_invite_refuses_existing_members(fake):
    _invite_pages(fake)
    with pytest.raises(WriteRefused, match="Already members"):
        admin_tools.skool_invite_members(["Taken@Example.com", "new@example.com"])


def test_invite_preview_carries_the_warnings(fake):
    _invite_pages(fake)
    prev = admin_tools.skool_invite_members(["new@example.com"])
    text = " ".join(prev["warnings"])
    for phrase in ("SKIPS payment", "emails real people", "cannot be recalled",
                   "UNVERIFIED"):
        assert phrase in text
    assert prev["rate_caps"]["this_action_units"] == 1


def test_invite_refuses_unknown_course_ids(fake):
    _invite_pages(fake)
    with pytest.raises(WriteRefused, match="Unknown course ids"):
        admin_tools.skool_invite_members(["new@example.com"], course_ids=["c9"])


def test_invite_sends_emails_and_courses(fake):
    _invite_pages(fake)
    fake.api[("POST", f"/groups/{GROUP}/invite")] = (200, {})
    _confirm(admin_tools.skool_invite_members, ["New@Example.com", "new@example.com"],
             course_ids=["c1"])
    assert fake.writes()[-1].json == {"emails": ["new@example.com"], "course_ids": ["c1"]}


def test_invite_warns_when_member_emails_are_not_visible(fake):
    _invite_pages(fake, member_emails=())
    fake.pages[f"/{GROUP}/-/members"] = {"users": [{"id": "u1"}], "totalPages": 1}
    prev = admin_tools.skool_invite_members(["new@example.com"])
    assert any("Could not check for existing members" in w for w in prev["warnings"])


# --- audit failures around a write ---------------------------------------------------
def _audit_fails_on(monkeypatch, event):
    real = guard.audit

    def flaky(ev, *a, **kw):
        if ev == event:
            raise OSError("disk full")
        return real(ev, *a, **kw)

    monkeypatch.setattr(guard, "audit", flaky)


def test_a_failed_result_line_does_not_turn_a_done_write_into_an_error(fake, monkeypatch):
    """The write happened. Raising here would invite a retry, which for a reply or
    an invite does it twice."""
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    fake.api[("POST", "/members/m1/role")] = (200, {})
    _audit_fails_on(monkeypatch, "result")
    res = _confirm(admin_tools.skool_approve_member, "m1")
    assert res["executed"] is True and "audit_warning" in res
    assert len(fake.writes()) == 1


def test_a_failed_result_line_does_not_hide_the_real_error(fake, monkeypatch):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    fake.api[("POST", "/members/m1/role")] = (500, "server exploded")
    _audit_fails_on(monkeypatch, "result")
    with pytest.raises(sk.SkoolAPIError):
        _confirm(admin_tools.skool_approve_member, "m1")


def test_a_failed_attempt_line_sends_nothing(fake, monkeypatch):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    _audit_fails_on(monkeypatch, "attempt")
    with pytest.raises(WriteRefused, match="Nothing was sent"):
        _confirm(admin_tools.skool_approve_member, "m1")
    assert fake.writes() == []


# --- transport: never re-send an admin write -------------------------------------------
class _RecordingHttpx:
    calls = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, method, url, **kw):
        _RecordingHttpx.calls.append(method)
        return _resp(200, {"ok": True}, method, url)


def _curl_that_raises(message, sent):
    def boom(url, **kw):
        sent.append(url)
        raise RuntimeError(message)
    return SimpleNamespace(get=boom, post=boom, put=boom, delete=boom)


def test_admin_write_is_not_resent_over_httpx_after_a_curl_error(monkeypatch):
    """A curl timeout can happen AFTER Skool received the request. Falling back to
    httpx would send it a second time."""
    sent = []
    _RecordingHttpx.calls = []
    monkeypatch.setattr(sk, "_curl", _curl_that_raises("Operation timed out", sent))
    monkeypatch.setattr(sk.httpx, "Client", _RecordingHttpx)
    with pytest.raises(sk.WriteOutcomeUnknown, match="NOT re-sent"):
        REAL_REQUEST("post", sk.API + "/members/m1/role", headers={},
                     json_body={"new": "member"}, resend_on_error=False)
    assert len(sent) == 1 and _RecordingHttpx.calls == []


def test_reads_and_existing_tools_still_fall_back_to_httpx(monkeypatch):
    sent = []
    _RecordingHttpx.calls = []
    monkeypatch.setattr(sk, "_curl", _curl_that_raises("Operation timed out", sent))
    monkeypatch.setattr(sk.httpx, "Client", _RecordingHttpx)
    assert REAL_REQUEST("get", sk.API + "/self/groups", headers={}).status_code == 200
    assert REAL_REQUEST("post", sk.API + "/posts", headers={}, json_body={}).status_code == 200
    assert _RecordingHttpx.calls == ["GET", "POST"]


def test_an_impersonation_error_still_falls_back_for_writes(monkeypatch):
    """Raised before anything is sent, so the httpx fallback is safe."""
    sent = []
    _RecordingHttpx.calls = []
    monkeypatch.setattr(sk, "_curl", _curl_that_raises(
        "Impersonating chrome999 is not supported", sent))
    monkeypatch.setattr(sk.httpx, "Client", _RecordingHttpx)
    r = REAL_REQUEST("post", sk.API + "/x", headers={}, json_body={}, resend_on_error=False)
    assert r.status_code == 200 and _RecordingHttpx.calls == ["POST"]


def test_every_admin_write_asks_for_no_resend_and_old_tools_do_not(fake):
    _with_pending(fake, _pending_user("m1", "u1", "Jane", "Doe"))
    fake.api[("POST", "/members/m1/role")] = (200, {})
    fake.api[("GET", "/posts/p1")] = (200, {"post": _post()})
    fake.api[("POST", "/posts/p1/update")] = (200, {})
    fake.api[("DELETE", "/posts/p1")] = (200, {})
    fake.api[("POST", "/posts")] = (200, {"id": "c9"})
    _confirm(admin_tools.skool_approve_member, "m1")
    _confirm(admin_tools.skool_edit_post, "p1", content="New")
    _confirm(admin_tools.skool_delete_post, "p1")
    assert [c.resend for c in fake.writes()] == [False, False, False]
    sk.comment_on_post("p1", "existing tool")  # unchanged behaviour
    assert fake.writes()[-1].resend is True
    assert fake.writes()[-1].json["root_id"] == fake.writes()[-1].json["parent_id"] == "p1"


# --- transport: errors and stale-cookie reload --------------------------------------
def test_404_is_not_found_and_still_a_runtime_error():
    with pytest.raises(sk.NotFound) as ei:
        sk._handle(_resp(404, "nope"), "/posts/x")
    assert isinstance(ei.value, RuntimeError)


def test_other_errors_keep_the_body():
    with pytest.raises(sk.SkoolAPIError) as ei:
        sk._handle(_resp(400, "cannot update to same role"), "/members/m/role")
    assert ei.value.status == 400 and "same role" in ei.value.body


def _cookie_router(fake, good_marker):
    def handler(call):
        ok = good_marker in call.headers["Cookie"]
        return _resp(200 if ok else 401, {"groups": []} if ok else "not logged in")
    fake.api[("GET", "/self/groups")] = handler


def test_401_picks_up_a_cookie_refreshed_on_disk_and_retries_once(fake, monkeypatch,
                                                                   tmp_path):
    env = tmp_path / ".env"
    env.write_text("SKOOL_COOKIE=auth_token=NEWCOOKIE123456\nSKOOL_GROUP=test-group\n")
    monkeypatch.setattr(sk, "ENV_PATH", str(env))
    monkeypatch.setattr(sk, "_ENV_STAMP", (0, 0))  # loaded before the refresh
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=OLDCOOKIE123456")
    _cookie_router(fake, "NEWCOOKIE")
    assert sk._api_get("/self/groups") == {"groups": []}
    assert len(fake.calls) == 2
    assert os.environ["SKOOL_COOKIE"] == "auth_token=NEWCOOKIE123456"


def test_401_with_an_unchanged_env_file_does_not_retry(fake, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SKOOL_COOKIE=auth_token=SAMECOOKIE123456\n")
    monkeypatch.setattr(sk, "ENV_PATH", str(env))
    monkeypatch.setattr(sk, "_ENV_STAMP", sk._env_stamp())
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=SAMECOOKIE123456")
    _cookie_router(fake, "NEVER")
    with pytest.raises(sk.AuthError):
        sk._api_get("/self/groups")
    assert len(fake.calls) == 1


def test_refresh_verify_of_a_bad_new_cookie_still_fails(fake, monkeypatch, tmp_path):
    """refresh_cookie.verify() tests a NEW cookie before writing .env, which still
    holds the OLD one. The reload must not fall back to the old cookie and let a
    broken refresh 'pass' (it would then be persisted)."""
    env = tmp_path / ".env"
    env.write_text("SKOOL_COOKIE=auth_token=OLDGOOD123456\n")
    monkeypatch.setattr(sk, "ENV_PATH", str(env))
    monkeypatch.setattr(sk, "_ENV_STAMP", sk._env_stamp())  # imported after .env written
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=NEWBAD123456")
    _cookie_router(fake, "OLDGOOD")
    with pytest.raises(sk.AuthError):
        sk._api_get("/self/groups")
    assert len(fake.calls) == 1


def test_reload_only_touches_the_cookie(fake, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SKOOL_COOKIE=auth_token=NEWCOOKIE123456\nSKOOL_WRITE_TOOLS=0\n")
    monkeypatch.setattr(sk, "ENV_PATH", str(env))
    monkeypatch.setattr(sk, "_ENV_STAMP", (0, 0))
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=OLDCOOKIE123456")
    _cookie_router(fake, "NEWCOOKIE")
    sk._api_get("/self/groups")
    assert os.environ["SKOOL_WRITE_TOOLS"] == "1"


def test_an_edge_block_is_never_retried(fake, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SKOOL_COOKIE=auth_token=NEWCOOKIE123456\n")
    monkeypatch.setattr(sk, "ENV_PATH", str(env))
    monkeypatch.setattr(sk, "_ENV_STAMP", (0, 0))
    fake.api[("GET", "/self/groups")] = lambda call: httpx.Response(
        403, text="<HTML><BODY>Request blocked.</BODY></HTML>",
        headers={"server": "CloudFront"}, request=httpx.Request("GET", "https://x"))
    with pytest.raises(sk.EdgeBlocked):
        sk._api_get("/self/groups")
    assert len(fake.calls) == 1


def test_reload_also_covers_page_reads(fake, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SKOOL_COOKIE=auth_token=NEWCOOKIE123456\n")
    monkeypatch.setattr(sk, "ENV_PATH", str(env))
    monkeypatch.setattr(sk, "_ENV_STAMP", (0, 0))
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=OLDCOOKIE123456")
    real = fake.__call__

    def gate(method, url, **kw):
        if "OLDCOOKIE" in kw["headers"]["Cookie"]:
            fake.calls.append(SimpleNamespace(method="GET", url=url))
            return _resp(401, "not logged in")
        return real(method, url, **kw)

    monkeypatch.setattr(sk, "_request", gate)
    fake.pages[f"/{GROUP}/calendar"] = {"renderData": {"events": []}}
    assert sk.list_events()["events"] == []
    assert len(fake.calls) == 2


# --- doctor ----------------------------------------------------------------------
def test_doctor_reports_write_tools_off_by_default():
    c = doctor.check_write_tools({}, environ={})
    assert c.status == doctor.INFO and c.detail.startswith("disabled")


def test_doctor_reports_write_tools_on_from_the_shell_or_env_file():
    c = doctor.check_write_tools({}, environ={"SKOOL_WRITE_TOOLS": "1"})
    assert c.status == doctor.INFO and "ENABLED via shell environment" in c.detail
    c = doctor.check_write_tools({"SKOOL_WRITE_TOOLS": "true"}, environ={})
    assert "ENABLED via .env" in c.detail
    c = doctor.check_write_tools({"SKOOL_WRITE_TOOLS": "0"}, environ={})
    assert c.detail.startswith("disabled")
