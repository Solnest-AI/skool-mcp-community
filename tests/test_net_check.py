"""net_check has to separate THREE failures that all present as "403 from
CloudFront", because the fix for each is useless for the others:

  * IP block  -> change your network (VPN off).
  * TLS block -> use the Chrome-impersonating transport. VPN advice is WRONG here
                 and actively wastes the member's time.
  * login     -> reconnect.

An earlier version of this tool collapsed the first two and told everyone to
disable their VPN. That is the specific regression these tests exist to prevent.
The network is never touched; probe results are passed in as plain dicts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import net_check  # noqa: E402

CF_HTML = ('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'
           "<HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE>"
           "</HEAD><BODY><H1>403 ERROR</H1>Request blocked.</BODY></HTML>")
CF_HEADERS = {"server": "CloudFront", "via": "1.1 abc.cloudfront.net (CloudFront)"}


def blocked():
    return {"status": 403, "headers": CF_HEADERS, "body": CF_HTML}


def reachable():
    return {"status": 401, "headers": {"content-type": "text/plain"},
            "body": "not logged in"}


def missing_curl():
    return {"error": "curl_cffi_missing"}


# --- CDN detection ------------------------------------------------------------
def test_cloudfront_403_is_recognised():
    assert net_check.looks_like_cloudfront_block(403, CF_HEADERS, CF_HTML) is True


def test_401_is_never_a_cdn_block():
    assert net_check.looks_like_cloudfront_block(
        401, {"content-type": "text/plain"}, "not logged in") is False


def test_plain_text_403_is_not_a_cdn_block():
    """Only CloudFront serves an HTML error page here."""
    assert net_check.looks_like_cloudfront_block(
        403, {"content-type": "text/plain"}, "forbidden") is False


# --- the three-way verdict ----------------------------------------------------
def test_both_transports_blocked_means_the_IP_is_blocked():
    assert net_check.classify(blocked(), blocked()) == net_check.IP_BLOCKED


def test_only_python_tls_blocked_means_TLS_fingerprinting():
    """The two probes send an identical User-Agent and differ only in TLS stack,
    so disagreement can only be about the handshake."""
    assert net_check.classify(blocked(), reachable()) == net_check.TLS_BLOCKED


def test_neither_blocked_means_reachable():
    assert net_check.classify(reachable(), reachable()) == net_check.REACHABLE


def test_reachable_even_when_curl_cffi_is_not_installed():
    """A missing optional transport must not read as a failure on a healthy box."""
    assert net_check.classify(reachable(), missing_curl()) == net_check.REACHABLE


def test_both_transports_erroring_is_plain_connectivity():
    err = {"error": "ConnectError"}
    assert net_check.classify(err, err) == net_check.UNREACHABLE


# --- the advice each verdict gives -------------------------------------------
def test_tls_block_does_NOT_tell_the_member_to_disable_their_vpn():
    """THE regression. A TLS-fingerprint block is identical on every network, so
    VPN advice sends the member chasing something that cannot change the result."""
    text = "\n".join(net_check.verdict_lines(blocked(), reachable(), {})).lower()
    assert "turning off a vpn will not change it" in text
    assert "not your network" in text


def test_tls_block_without_curl_cffi_says_to_install_it():
    text = "\n".join(net_check.verdict_lines(blocked(), missing_curl(), {})).lower()
    # curl_cffi missing means we cannot have proven Chrome TLS works, so this must
    # not claim a TLS verdict it did not measure.
    assert net_check.classify(blocked(), missing_curl()) != net_check.TLS_BLOCKED


def test_ip_block_DOES_tell_the_member_to_change_networks():
    text = "\n".join(net_check.verdict_lines(blocked(), blocked(), {})).lower()
    assert "vpn" in text
    assert "cannot fix it" in text


def test_ip_block_names_the_proxy_variables_it_found():
    text = "\n".join(
        net_check.verdict_lines(blocked(), blocked(), {"HTTPS_PROXY": "http://p:8080"}))
    assert "HTTPS_PROXY" in text


def test_reachable_points_at_the_login_not_the_network():
    text = "\n".join(net_check.verdict_lines(reachable(), reachable(), {})).lower()
    assert "connect_skool.py" in text
    assert "vpn" not in text


# --- rendering ----------------------------------------------------------------
def test_render_marks_the_library_ua_403_as_expected():
    out = net_check.render(reachable(), reachable(), blocked(), {}, "1.2.3.4")
    assert "NORMAL and expected" in out


def test_render_reports_a_missing_curl_cffi_as_installable_not_as_a_crash():
    out = net_check.render(reachable(), missing_curl(), blocked(), {}, "1.2.3.4")
    assert "not installed" in out
    assert "FAILED" not in out.split("proxy env vars")[0]


def test_render_survives_probes_that_errored():
    err = {"error": "ConnectTimeout"}
    out = net_check.render(err, err, err, {}, "")
    assert "FAILED" in out and "could not determine" in out


def test_no_cookie_is_ever_sent_by_either_probe():
    """The probes' authority comes from carrying no credential at all."""
    import inspect
    for fn in (net_check.probe_httpx, net_check.probe_chrome_tls):
        src = inspect.getsource(fn)
        assert "Cookie" not in src and "SKOOL_COOKIE" not in src


def test_active_proxies_reads_the_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    assert net_check.active_proxies().get("HTTPS_PROXY") == "http://proxy.local:3128"
