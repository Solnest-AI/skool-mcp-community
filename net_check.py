#!/usr/bin/env python3
"""Why can't this computer reach Skool? Answers in about five seconds.

    uv run net_check.py

Run this the moment you see a 403. It reads no cookie, needs no login, and sends
nothing about you anywhere. It asks Skool's API three questions and compares the
answers.

WHY THIS EXISTS
api2.skool.com sits behind AWS CloudFront + WAF, which produces THREE different
failures that look identical from the outside and have completely different fixes:

  * HTTP 401, short text body   -> Skool saw the request and rejected the cookie.
                                   Fix: `uv run connect_skool.py`.
  * HTTP 403, and ONLY over
    Python's own TLS            -> the WAF is inspecting the TLS handshake
                                   fingerprint (JA3/JA4) on your IP. Plain Python
                                   does not handshake like Chrome. Fix: the
                                   bundle's Chrome-impersonating transport
                                   (curl_cffi), which is automatic once installed.
                                   Turning off a VPN does NOT fix this one.
  * HTTP 403, over BOTH         -> your egress IP itself is refused. Nothing about
                                   the request can change that. Fix: change how
                                   you reach the internet (VPN off, other network).

Telling those last two apart is the entire point. They are both "403 from
CloudFront", and the remedy for one is useless for the other.

The probes are deliberately UNAUTHENTICATED, which is what makes them decisive:
with no cookie at all the only honest answer is 401 "not logged in". A machine
that gets 403 instead was blocked before authentication was ever considered, so it
cannot be a cookie problem. No credential is involved, so none can be blamed.
"""
import os
import sys

PROBE = "https://api2.skool.com/self/groups"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
LIBRARY_UA = "python-httpx/0.28.1"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy")

# Verdicts. TLS_BLOCKED and IP_BLOCKED are both CloudFront 403s and are the pair
# this tool exists to separate.
REACHABLE, TLS_BLOCKED, IP_BLOCKED, UNREACHABLE, UNKNOWN = (
    "REACHABLE", "TLS_BLOCKED", "IP_BLOCKED", "UNREACHABLE", "UNKNOWN")


def looks_like_cloudfront_block(status: int, headers: dict, body: str) -> bool:
    """A 403 served by CloudFront itself rather than by Skool.

    Matches the CDN's own fingerprint (server/via headers, or its HTML error page)
    instead of the status code alone, so a genuine plain-text 403 from Skool's
    application is not mistaken for a CDN block.
    """
    if status != 403:
        return False
    fingerprint = f"{headers.get('server', '')} {headers.get('via', '')}".lower()
    return "cloudfront" in fingerprint or "<html" in (body or "")[:400].lower()


def _blocked(result: dict) -> bool:
    return looks_like_cloudfront_block(result.get("status", 0),
                                       result.get("headers", {}),
                                       result.get("body", ""))


def classify(python_tls: dict, chrome_tls: dict) -> str:
    """The verdict, from the two browser-UA probes.

    Both probes send an identical browser User-Agent and differ ONLY in the TLS
    stack underneath. So any disagreement between them isolates the TLS handshake
    as the thing being judged, and agreement points at the IP instead.
    """
    if python_tls.get("error") and chrome_tls.get("error"):
        return UNREACHABLE
    py_blocked, cr_blocked = _blocked(python_tls), _blocked(chrome_tls)

    if py_blocked and cr_blocked:
        return IP_BLOCKED          # nothing about the request helps
    if py_blocked and chrome_tls.get("status") == 401:
        return TLS_BLOCKED         # Chrome's handshake gets through, Python's does not
    if python_tls.get("status") == 401 or chrome_tls.get("status") == 401:
        return REACHABLE
    return UNKNOWN


def probe_httpx(url: str, ua: str, timeout: int = 20) -> dict:
    """Request over Python's own TLS stack (OpenSSL)."""
    import httpx
    try:
        # No cookie. trust_env stays on so a proxy configured in the environment
        # is exercised here exactly as the MCP server would use it.
        r = httpx.get(url, headers={"user-agent": ua} if ua else {}, timeout=timeout)
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)[:160]}
    return {"status": r.status_code, "headers": dict(r.headers), "body": r.text[:400]}


def probe_chrome_tls(url: str, ua: str, timeout: int = 20) -> dict:
    """Same request over a real Chrome TLS fingerprint, if curl_cffi is installed."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return {"error": "curl_cffi_missing"}
    try:
        r = creq.get(url, headers={"user-agent": ua} if ua else {},
                     timeout=timeout, impersonate="chrome")
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)[:160]}
    return {"status": r.status_code, "headers": dict(r.headers), "body": r.text[:400]}


def egress_ip() -> str:
    """Public IP as the internet sees it. Best-effort; never fatal."""
    import httpx
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            r = httpx.get(url, timeout=8)
            if r.status_code == 200 and r.text.strip():
                return r.text.strip()
        except Exception:
            continue
    return ""


def active_proxies() -> dict:
    return {v: os.environ[v] for v in PROXY_VARS if os.environ.get(v)}


def verdict_lines(python_tls: dict, chrome_tls: dict, proxies: dict) -> list:
    """The conclusion. A pure function of the probe results, so every branch is
    testable without touching the network."""
    state = classify(python_tls, chrome_tls)
    out = []

    if state == UNREACHABLE:
        out.append("RESULT: Could not reach api2.skool.com at all.")
        out.append("")
        out.append("That is a plain connectivity problem, not a Skool problem.")
        out.append("Check your internet connection, then re-run this.")
        return out

    if state == TLS_BLOCKED:
        out.append("RESULT: Skool's firewall is rejecting Python's TLS fingerprint.")
        out.append("")
        out.append("Chrome's handshake got through from this exact machine and IP;")
        out.append("plain Python's did not. So this is NOT your network and NOT your")
        out.append("login. Turning off a VPN will not change it.")
        out.append("")
        if chrome_tls.get("error") == "curl_cffi_missing":
            out.append("Fix: install the Chrome-impersonating transport, then retry:")
            out.append("    uv sync")
        else:
            out.append("Fix: already installed. This bundle uses the Chrome transport")
            out.append("automatically, so the tools should work. If they still fail,")
            out.append("run `uv run doctor.py` — that would be a login problem instead.")
        return out

    if state == IP_BLOCKED:
        out.append("RESULT: Skool's CDN is BLOCKING this computer's IP address.")
        out.append("")
        out.append("Both a Chrome handshake and a Python one were refused, and this")
        out.append("probe sent NO cookie and NO login. The block happened before Skool")
        out.append("looked at a session, so your Skool login is not the problem and")
        out.append("running connect_skool.py again cannot fix it.")
        out.append("")
        out.append("Fix it by changing how your traffic leaves this machine:")
        out.append("  1. Turn OFF any VPN, then re-run this. CloudFront blocks IP")
        out.append("     ranges known for VPNs and hosting providers.")
        out.append("  2. Switch networks: phone hotspot instead of wifi. A shared or")
        out.append("     flagged office/ISP address blocks everyone behind it.")
        if proxies:
            out.append("  3. Unset these proxy variables — your Python traffic is going")
            out.append("     somewhere your browser may not:")
            for key in proxies:
                out.append(f"       {key}")
        else:
            out.append("  3. Disable any corporate/security proxy or filtering tool.")
        return out

    if state == REACHABLE:
        out.append("RESULT: Skool is reachable from this computer. ")
        out.append("")
        out.append("Skool answered 401 'not logged in', which is the correct answer to")
        out.append("a probe that deliberately sent no cookie. Network and TLS are fine.")
        out.append("")
        out.append("So if the MCP server is still failing, it is a LOGIN problem:")
        out.append("  Run:  uv run connect_skool.py")
        out.append("  Then: uv run doctor.py")
        return out

    out.append("RESULT: Unexpected answer. Skool may have changed something.")
    out.append("")
    out.append("Post the FINDINGS above in the community.")
    return out


def _fmt(result: dict) -> str:
    if result.get("error") == "curl_cffi_missing":
        return "not installed (run `uv sync`)"
    if result.get("error"):
        return f"FAILED ({result['error']})"
    return f"HTTP {result['status']}"


def render(python_tls: dict, chrome_tls: dict, library_ua: dict,
           proxies: dict, ip: str) -> str:
    lines = ["", "=" * 68, " Skool MCP — network check (no login required)", "=" * 68, ""]
    lines.append("FINDINGS")
    lines.append(f"  Python TLS + browser UA : {_fmt(python_tls)}")
    lines.append(f"  Chrome TLS + browser UA : {_fmt(chrome_tls)}")
    lines.append(f"  Python TLS + library UA : {_fmt(library_ua)}"
                 "   <- 403 here is NORMAL and expected")
    lines.append(f"  public IP               : {ip or 'could not determine'}")
    lines.append(f"  proxy env vars          : "
                 f"{', '.join(proxies) if proxies else 'none set'}")
    lines.append("")
    lines.extend(verdict_lines(python_tls, chrome_tls, proxies))
    lines.append("")
    lines.append("-" * 68)
    lines.append("Copy this whole output if you need to post it in the community.")
    lines.append("(The public IP is yours — redact it if you would rather not share.)")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("FAILED: httpx is not installed.\n  -> Run `uv sync` and try again.",
              file=sys.stderr)
        return 1

    print("Checking how this computer reaches Skool...", flush=True)
    # The two browser-UA probes differ ONLY in the TLS stack, which is what makes
    # their disagreement meaningful. The library-UA probe is the control: it SHOULD
    # be blocked, proving the WAF rule is live and that a clean 401 above is a real
    # result rather than an accident.
    python_tls = probe_httpx(PROBE, BROWSER_UA)
    chrome_tls = probe_chrome_tls(PROBE, BROWSER_UA)
    library_ua = probe_httpx(PROBE, LIBRARY_UA)
    proxies = active_proxies()
    ip = egress_ip()

    text = render(python_tls, chrome_tls, library_ua, proxies, ip)
    try:
        print(text)
    except UnicodeEncodeError:  # legacy Windows console codepage
        print(text.encode("ascii", "ignore").decode())

    return 0 if classify(python_tls, chrome_tls) == REACHABLE else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(1)
