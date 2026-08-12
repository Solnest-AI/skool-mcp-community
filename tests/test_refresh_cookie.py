#!/usr/bin/env python3
"""Unit tests for refresh_cookie pure helpers — no network.
Run: .venv/bin/python test_refresh_cookie.py
"""
import time
import refresh_cookie as rc


def test_parse_rebuild_roundtrip():
    header = "auth_token=abc.def.ghi; client_id=xyz; aws-waf-token=OLD"
    cookies = rc.parse_cookie_header(header)
    assert cookies["auth_token"] == "abc.def.ghi"
    assert cookies["aws-waf-token"] == "OLD"
    # rebuild preserves insertion order
    assert rc.rebuild_cookie_header(cookies) == header


def test_parse_handles_equals_in_value():
    cookies = rc.parse_cookie_header("a=1; token=ab=cd==")
    assert cookies["token"] == "ab=cd=="  # only split on first '='


def test_replace_cookie_line_swaps_only_that_line():
    env = "SKOOL_COOKIE=OLDHEADER\nSKOOL_GROUP=solnest-ai\nSKOOL_GROUP_ID=abc123\n"
    out = rc.replace_cookie_line(env, "NEWHEADER")
    assert out == "SKOOL_COOKIE=NEWHEADER\nSKOOL_GROUP=solnest-ai\nSKOOL_GROUP_ID=abc123\n"


def test_replace_cookie_line_appends_if_missing():
    env = "SKOOL_GROUP=solnest-ai\n"
    out = rc.replace_cookie_line(env, "NEWHEADER")
    assert "SKOOL_GROUP=solnest-ai\n" in out
    assert "SKOOL_COOKIE=NEWHEADER\n" in out


def test_auth_token_days_left():
    import base64 as b64
    import json as j
    exp = int(time.time()) + 10 * 86400  # 10 days out
    payload = b64.urlsafe_b64encode(j.dumps({"exp": exp, "user_id": "u"}).encode()).decode().rstrip("=")
    fake_jwt = f"head.{payload}.sig"
    days = rc.auth_token_days_left({"auth_token": fake_jwt})
    assert 9.5 < days < 10.5, days


def test_auth_token_days_left_non_jwt_returns_none():
    assert rc.auth_token_days_left({"auth_token": "not-a-jwt"}) is None
    assert rc.auth_token_days_left({}) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
