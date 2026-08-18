"""Keep 'the CDN blocked you' and 'your cookie expired' strictly apart.

Background: api2.skool.com is behind CloudFront + AWS WAF. The client used to
raise the same "your cookie has expired" error for 401 and 403 alike. That is
wrong for 403 in the way that costs the most time — a member hits a CDN block,
is told to reconnect, reconnects successfully, hits the same block, and concludes
the tool is broken. Verified live 2026-08-13: api2 returns 200 with auth_token
alone, and 403 CloudFront for a library User-Agent with that same valid cookie.
So the 403 says nothing whatsoever about the cookie.

These tests pin that separation. No network access — responses are constructed.
"""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skool_client  # noqa: E402

CF_BODY = ('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'
           "<HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE>"
           "</HEAD><BODY><H1>403 ERROR</H1>Request blocked.</BODY></HTML>")


def _response(status: int, body: str, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code=status, text=body, headers=headers or {},
                          request=httpx.Request("GET", "https://api2.skool.com/self/groups"))


def _cloudfront_403() -> httpx.Response:
    return _response(403, CF_BODY, {"server": "CloudFront", "content-type": "text/html",
                                    "via": "1.1 abc.cloudfront.net (CloudFront)"})


# --- detection ----------------------------------------------------------------
def test_cloudfront_403_is_an_edge_block():
    assert skool_client.is_edge_block(_cloudfront_403()) is True


def test_skool_401_is_not_an_edge_block():
    assert skool_client.is_edge_block(_response(401, "not logged in")) is False


def test_plain_text_403_from_skool_is_not_an_edge_block():
    """Only CloudFront serves HTML here. A short plain-text 403 is Skool's own
    answer and does concern the session, so it must stay an AuthError."""
    assert skool_client.is_edge_block(_response(403, "forbidden")) is False


def test_200_is_not_an_edge_block():
    assert skool_client.is_edge_block(_response(200, "{}")) is False


# --- what _handle raises ------------------------------------------------------
def test_handle_raises_edge_blocked_not_auth_error_for_cloudfront():
    """EdgeBlocked must NOT be an AuthError — callers branch on that type to
    decide whether re-authenticating could possibly help."""
    with pytest.raises(skool_client.EdgeBlocked):
        skool_client._handle(_cloudfront_403(), "/self/groups")
    assert not issubclass(skool_client.EdgeBlocked, skool_client.AuthError)


def test_handle_still_raises_auth_error_for_a_real_401():
    with pytest.raises(skool_client.AuthError):
        skool_client._handle(_response(401, "not logged in"), "/self/groups")


def test_edge_block_message_does_not_tell_the_member_to_reconnect():
    """The regression that matters. 'Run connect_skool.py' in response to a CDN
    block is the advice that sends people in circles."""
    with pytest.raises(skool_client.EdgeBlocked) as excinfo:
        skool_client._handle(_cloudfront_403(), "/self/groups")
    message = str(excinfo.value).lower()
    assert "vpn" in message
    assert "will not help" in message
    assert "your login is fine" in message


def test_auth_error_message_still_names_the_status_code():
    with pytest.raises(skool_client.AuthError) as excinfo:
        skool_client._handle(_response(401, "not logged in"), "/self/groups")
    assert "401" in str(excinfo.value)


# --- headers ------------------------------------------------------------------
def test_headers_carry_a_browser_user_agent(monkeypatch):
    """A library UA is blocked at the edge, so this is load-bearing, not cosmetic."""
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=x")
    ua = skool_client._headers()["user-agent"]
    assert ua.startswith("Mozilla/5.0")
    assert "httpx" not in ua.lower() and "python" not in ua.lower()


def test_headers_look_like_the_skool_web_app(monkeypatch):
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=x")
    headers = skool_client._headers()
    assert headers["origin"] == "https://www.skool.com"
    assert headers["sec-fetch-mode"] == "cors"


# --- transport ----------------------------------------------------------------
def test_chrome_tls_transport_is_available_and_preferred():
    """The bundle ships curl_cffi so the Chrome TLS fingerprint is the default.

    Measured 2026-08-17: AWS WAF gates api2 on the User-Agent AND, on some source
    IPs, the TLS handshake. httpx clears the first but never the second, and the
    failure is invisible until you land on an IP where it matters. Preferring
    curl_cffi clears both.
    """
    assert skool_client.tls_impersonation_available() is True
    assert skool_client._curl is not None


def test_httpx_remains_the_fallback_when_curl_cffi_is_absent(monkeypatch):
    """An install without the native wheel must degrade to httpx, not crash."""
    monkeypatch.setattr(skool_client, "_curl", None)
    assert skool_client.tls_impersonation_available() is False


def test_impersonation_target_is_a_chrome_profile():
    assert "chrome" in skool_client.IMPERSONATE.lower()


def test_impersonation_target_is_overridable_by_env(monkeypatch):
    """Skool's WAF may start rejecting whichever Chrome build curl_cffi mimics.
    SKOOL_IMPERSONATE lets a member move off it without editing code."""
    import importlib
    monkeypatch.setenv("SKOOL_IMPERSONATE", "chrome131")
    importlib.reload(skool_client)
    assert skool_client.IMPERSONATE == "chrome131"
    monkeypatch.delenv("SKOOL_IMPERSONATE")
    importlib.reload(skool_client)
