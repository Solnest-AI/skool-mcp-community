#!/usr/bin/env python3
"""Unit tests for doctor.py — each check against a synthetic env state, no network.

The load-bearing test is test_no_cookie_value_can_reach_the_output: doctor's whole
reason for existing is that a member (or an agent reading its output) can diagnose
a broken install without the credential ever being displayed.
"""
import base64
import json
import os
import time
from urllib.parse import quote

import pytest

import doctor

WAF = "WAFTOKENSENTINEL999"
JWT_SIG = "SIGNATURESENTINEL42"


def _jwt(days_out: float, sig: str = JWT_SIG) -> str:
    """A decodable auth_token whose exp is `days_out` days from now."""
    claims = {"exp": int(time.time() + days_out * 86400), "user_id": "u"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"HEADERSEGMENT.{payload}.{sig}"


def _env(cookie: str = "", group: str = "my-slug", **extra) -> dict:
    env = {"SKOOL_COOKIE": cookie, "SKOOL_GROUP": group}
    env.update(extra)
    return env


def _header(auth: str = "", waf: str = WAF) -> str:
    parts = []
    if auth:
        parts.append(f"auth_token={auth}")
    if waf:
        parts.append(f"aws-waf-token={waf}")
    parts.append("client_id=abc")
    return "; ".join(parts)


def _stub_runner(stdout=""):
    class Proc:
        pass

    Proc.stdout = stdout
    return lambda *a, **k: Proc()


def _offline_checks(env_path, **kw):
    """run_checks with the two subprocess-spawning checks stubbed, so tests do not
    shell out to the developer's real launchd or Playwright install."""
    kw.setdefault("live", False)
    return doctor.run_checks(env_path=env_path,
                             chromium_runner=_stub_runner(""),
                             job_runner=_stub_runner(""), **kw)


# --- .env parsing ----------------------------------------------------------------
def test_read_env_file_missing_returns_empty(tmp_path):
    assert doctor.read_env_file(str(tmp_path / "nope.env")) == {}


def test_read_env_file_handles_comments_quotes_and_equals_in_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\n\nSKOOL_COOKIE="a=1; b=2=="\nexport SKOOL_GROUP=slug\n',
                 encoding="utf-8")
    env = doctor.read_env_file(str(p))
    assert env["SKOOL_COOKIE"] == "a=1; b=2=="  # split on the FIRST '=' only
    assert env["SKOOL_GROUP"] == "slug"


# --- individual checks ------------------------------------------------------------
def test_check_python():
    assert doctor.check_python((3, 12, 0)).status == doctor.PASS
    assert doctor.check_python((3, 10, 0)).status == doctor.PASS
    bad = doctor.check_python((3, 9, 18))
    assert bad.status == doctor.FAIL and bad.hint


def test_check_imports_reports_every_missing_module():
    ok = doctor.check_imports(finder=lambda m: object())
    assert ok.status == doctor.PASS

    missing = doctor.check_imports(
        finder=lambda m: None if m in ("mcp", "playwright") else object())
    assert missing.status == doctor.FAIL
    assert "mcp" in missing.detail and "playwright" in missing.detail
    assert "uv sync" in missing.hint


def test_check_imports_survives_a_finder_that_raises():
    assert doctor.check_imports(
        finder=lambda m: (_ for _ in ()).throw(ValueError())).status == doctor.FAIL


def test_check_chromium_reads_the_probe_subprocess(tmp_path):
    fake_browser = tmp_path / "chrome"
    fake_browser.write_text("", encoding="utf-8")

    assert doctor.check_chromium(
        runner=_stub_runner(f"{fake_browser}\n")).status == doctor.PASS

    absent = doctor.check_chromium(runner=_stub_runner("/nowhere/chromium\n"))
    assert absent.status == doctor.FAIL
    assert "playwright install" in absent.hint

    def boom(*a, **k):
        raise OSError("driver crashed")

    assert doctor.check_chromium(runner=boom).status == doctor.FAIL


def test_subprocess_children_never_inherit_the_credentials(monkeypatch):
    """Child processes are handed an environment scrubbed of SKOOL_*: process
    environments are readable via `ps eww` and are swept up by telemetry agents."""
    monkeypatch.setenv("SKOOL_COOKIE", "auth_token=LIVE_SESSION_TOKEN")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    seen = {}

    def spy(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})

        class Proc:
            stdout = ""
        return Proc()

    doctor.check_refresh_job(runner=spy, system="Darwin")
    assert "SKOOL_COOKIE" not in seen
    assert "PATH" in seen  # still a usable environment

    seen.clear()
    doctor.check_chromium(runner=spy)
    assert "SKOOL_COOKIE" not in seen


def test_system_binaries_are_resolved_absolutely():
    """A bare `schtasks` resolves against the current directory first on Windows,
    so an executable unzipped alongside the bundle would run instead."""
    import ntpath

    assert doctor._system_binary("Darwin", "launchctl") == "/bin/launchctl"
    win = doctor._system_binary("Windows", "schtasks")
    assert win.lower().endswith("system32\\schtasks.exe")
    # ntpath, not os.path: the assertion must hold when the suite runs on macOS.
    assert ntpath.isabs(win)
    assert "/" not in win  # no mixed separators from a host-rules join


def test_check_env_file(tmp_path):
    missing = doctor.check_env_file(str(tmp_path / ".env"))
    assert missing.status == doctor.FAIL
    assert "connect_skool" in missing.hint

    (tmp_path / ".env").write_text("SKOOL_GROUP=x\n", encoding="utf-8")
    assert doctor.check_env_file(str(tmp_path / ".env")).status == doctor.PASS


def test_check_cookie_states():
    assert doctor.check_cookie(_env("")).status == doctor.FAIL
    assert doctor.check_cookie({}).status == doctor.FAIL
    assert doctor.check_cookie(_env("this-is-not-a-cookie-header")).status == doctor.FAIL

    good = doctor.check_cookie(_env(_header(_jwt(300))))
    assert good.status == doctor.PASS
    assert "3 cookies" in good.detail


def test_check_auth_token_states():
    assert doctor.check_auth_token({}).status == doctor.FAIL
    assert doctor.check_auth_token({"auth_token": _jwt(300)}).status == doctor.PASS

    soon = doctor.check_auth_token({"auth_token": _jwt(5)})
    assert soon.status == doctor.WARN
    assert "5." in soon.detail and "15" not in soon.detail  # not a loose substring

    assert doctor.check_auth_token({"auth_token": _jwt(-3)}).status == doctor.FAIL
    assert doctor.check_auth_token({"auth_token": "not-a-jwt"}).status == doctor.WARN


def test_check_auth_token_warn_boundary_is_14_days():
    assert doctor.check_auth_token({"auth_token": _jwt(13.5)}).status == doctor.WARN
    assert doctor.check_auth_token({"auth_token": _jwt(14.5)}).status == doctor.PASS


def test_check_waf_token_states():
    assert doctor.check_waf_token({"aws-waf-token": WAF}).status == doctor.PASS
    # INFO, never WARN/FAIL. Verified against live api2 on 2026-08-13: auth_token
    # alone returns 200 with no aws-waf-token present, so its absence is not a
    # problem to report. Flagging it sent a member chasing a phantom token for
    # hours while the real block (a CloudFront rule on User-Agent / caller IP)
    # went unexamined.
    assert doctor.check_waf_token({}).status == doctor.INFO


def test_missing_waf_token_never_reads_as_a_problem():
    """Regression guard for the misdiagnosis above — no scary wording, no hint
    telling the member to go re-mint something that was never required."""
    check = doctor.check_waf_token({})
    assert check.status not in (doctor.WARN, doctor.FAIL)
    assert "block" not in (check.detail or "").lower()
    assert not (check.hint or "")


def test_check_group_states():
    assert doctor.check_group(_env(group="")).status == doctor.FAIL
    ok = doctor.check_group(_env(group="my-slug"))
    assert ok.status == doctor.PASS and ok.detail == "my-slug"


# --- live auth ----------------------------------------------------------------------
GOOD_WHOAMI = {"active_group": "my-slug", "active_group_id": "gid-1",
               "groups": [{"slug": "my-slug", "id": "gid-1"}]}


def test_check_live_auth_skips_when_credentials_are_missing():
    assert doctor.check_live_auth(_env("", group="")).status == doctor.SKIP


def test_check_live_auth_passes_with_a_resolving_group():
    ok = doctor.check_live_auth(_env(_header(_jwt(300))), whoami=lambda: GOOD_WHOAMI)
    assert ok.status == doctor.PASS


def test_check_live_auth_fails_on_a_rejected_cookie():
    bad = doctor.check_live_auth(
        _env(_header(_jwt(300))),
        whoami=lambda: (_ for _ in ()).throw(RuntimeError("401 from Skool")))
    assert bad.status == doctor.FAIL
    assert "connect_skool" in bad.hint


def test_check_live_auth_fails_when_the_account_is_not_a_member():
    """The real proof of membership is the server-returned group list, not
    active_group (which is just the env var doctor set a moment earlier)."""
    info = {"active_group": "my-slug", "active_group_id": "gid-STALE",
            "groups": [{"slug": "someone-elses-community", "id": "gid-x"}]}
    wrong = doctor.check_live_auth(_env(_header(_jwt(300))), whoami=lambda: info)
    assert wrong.status == doctor.FAIL
    assert "not a member" in wrong.detail


def test_check_live_auth_fails_when_no_group_id_resolves():
    info = {"active_group": "my-slug", "active_group_id": "", "groups": []}
    assert doctor.check_live_auth(
        _env(_header(_jwt(300))), whoami=lambda: info).status == doctor.FAIL


def test_check_live_auth_restores_the_environment_it_borrowed(monkeypatch):
    monkeypatch.delenv("SKOOL_COOKIE", raising=False)
    monkeypatch.setenv("SKOOL_GROUP", "pre-existing")

    doctor.check_live_auth(_env(_header(_jwt(300))), whoami=lambda: GOOD_WHOAMI)

    assert "SKOOL_COOKIE" not in os.environ
    assert os.environ["SKOOL_GROUP"] == "pre-existing"


def test_check_live_auth_restores_the_environment_even_when_whoami_raises(monkeypatch):
    monkeypatch.delenv("SKOOL_COOKIE", raising=False)
    doctor.check_live_auth(_env(_header(_jwt(300))),
                           whoami=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "SKOOL_COOKIE" not in os.environ


def test_check_refresh_job_is_informational_only():
    found = doctor.check_refresh_job(
        runner=_stub_runner("-\t0\tcom.skoolmcp.cookie-refresh\n"), system="Darwin")
    assert found.status == doctor.INFO and "registered" in found.detail

    absent = doctor.check_refresh_job(
        runner=_stub_runner("-\t0\tcom.apple.something\n"), system="Darwin")
    assert absent.status == doctor.INFO and "not installed" in absent.detail

    def boom(*a, **k):
        raise OSError("launchctl missing")

    assert doctor.check_refresh_job(runner=boom, system="Darwin").status == doctor.INFO
    assert doctor.check_refresh_job(runner=boom, system="Linux").status == doctor.INFO


def test_check_refresh_job_detects_the_windows_task():
    csv = '"TaskName","Next Run Time","Status"\n"\\SkoolMCP-CookieRefresh","N/A","Ready"\n'
    found = doctor.check_refresh_job(runner=_stub_runner(csv), system="Windows")
    assert found.status == doctor.INFO and "registered" in found.detail


def test_check_refresh_job_ignores_an_unrelated_skool_job():
    """A loose substring match used to claim 'registered' for any job with 'skool'
    in the name, including a different tool's. Only our own label counts."""
    other = doctor.check_refresh_job(
        runner=_stub_runner("-\t0\tcom.someoneelse.skool-cookie-refresh\n"), system="Darwin")
    assert "not installed" in other.detail


@pytest.mark.parametrize("system,installer", [
    ("Darwin", "install_refresh_mac.sh"),
    ("Windows", "install_refresh_windows.ps1"),
])
def test_job_label_matches_what_the_installer_registers(system, installer):
    """Drift guard. doctor.py and the installers must agree on the job label, or a
    correct install reports 'not installed' and the setup gate fails on a good setup.
    That is exactly the bug this test was added for.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), installer)
    with open(path, encoding="utf-8") as f:
        source = f.read()
    assert doctor._JOB_LABELS[system] in source, (
        f"{installer} does not contain {doctor._JOB_LABELS[system]!r}")


# --- orchestration ------------------------------------------------------------------
def test_run_checks_on_a_machine_with_no_env_file_reports_cleanly(tmp_path):
    """The very first thing a member runs. It must diagnose, not traceback."""
    checks = _offline_checks(str(tmp_path / ".env"))
    by_name = {c.name: c for c in checks}

    assert by_name[".env file"].status == doctor.FAIL
    assert by_name["SKOOL_COOKIE"].status == doctor.FAIL
    assert by_name["SKOOL_GROUP"].status == doctor.FAIL
    assert doctor.exit_code(checks) == 1
    assert doctor.auth_ok(checks) is False
    assert all(c.hint for c in checks if c.status == doctor.FAIL)  # every FAIL actionable


def test_run_checks_prints_nothing_itself(tmp_path, capsys):
    """All output goes through the renderers, which scrub. A stray print() inside
    a check would bypass that entirely."""
    _offline_checks(str(tmp_path / ".env"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_fully_configured_install_exits_zero_and_says_auth_ok(tmp_path):
    p = tmp_path / ".env"
    p.write_text(f"SKOOL_COOKIE={_header(_jwt(300))}\nSKOOL_GROUP=my-slug\n",
                 encoding="utf-8")
    browser = tmp_path / "chrome"
    browser.write_text("", encoding="utf-8")

    checks = doctor.run_checks(env_path=str(p), whoami=lambda: GOOD_WHOAMI,
                               chromium_runner=_stub_runner(f"{browser}\n"),
                               job_runner=_stub_runner(""))

    assert [c.status for c in checks if c.status == doctor.FAIL] == []
    assert doctor.exit_code(checks) == 0
    assert doctor.auth_ok(checks) is True
    assert "AUTH OK" in doctor.render_human(checks)


def test_exit_is_nonzero_when_auth_was_never_tested(tmp_path):
    """`--no-live` has no FAILs to report but proves nothing. An agent gating on
    the exit code must not read that as a healthy install."""
    p = tmp_path / ".env"
    p.write_text(f"SKOOL_COOKIE={_header(_jwt(300))}\nSKOOL_GROUP=my-slug\n",
                 encoding="utf-8")
    browser = tmp_path / "chrome"
    browser.write_text("", encoding="utf-8")

    checks = doctor.run_checks(env_path=str(p), live=False,
                               chromium_runner=_stub_runner(f"{browser}\n"),
                               job_runner=_stub_runner(""))
    assert not any(c.status == doctor.FAIL for c in checks)
    assert doctor.exit_code(checks) == 1
    assert "AUTH NOT OK" in doctor.render_human(checks)


def test_auth_ok_gate_string_has_no_substring_collision():
    """SETUP.md phase 3 greps for 'AUTH OK'. 'AUTH NOT OK' must not contain it."""
    assert "AUTH OK" not in "AUTH NOT OK"


def test_render_json_is_parseable_and_carries_the_gates(tmp_path):
    checks = _offline_checks(str(tmp_path / ".env"))
    payload = json.loads(doctor.render_json(checks))

    assert payload["ok"] is False
    assert payload["auth_ok"] is False
    assert len(payload["checks"]) == len(checks)
    assert {"name", "status", "detail", "hint"} <= set(payload["checks"][0])


# --- the security contract -------------------------------------------------------
def test_no_cookie_value_can_reach_the_output(tmp_path):
    """No cookie value, in whole or in part, may appear in ANY doctor output.

    Two layers are asserted: the checks never embed a value in the first place,
    and the scrubber catches it even when an exception message carries the raw
    header out of skool_client.
    """
    auth = _jwt(300)
    header = _header(auth)
    p = tmp_path / ".env"
    p.write_text(f"SKOOL_COOKIE={header}\nSKOOL_GROUP=my-slug\n", encoding="utf-8")
    env = doctor.read_env_file(str(p))

    checks = _offline_checks(str(p))
    # Layer 1: no scrubbing at all — the checks themselves must be clean. Include
    # the live check, which is the one that renders a third-party message.
    checks.append(doctor.check_live_auth(env, whoami=lambda: GOOD_WHOAMI))
    unscrubbed = doctor.render_human(checks) + doctor.render_json(checks)

    # Layer 2: a hostile check that echoes the entire header, plus the scrubber.
    checks.append(doctor.check_live_auth(
        env, whoami=lambda: (_ for _ in ()).throw(RuntimeError(f"rejected: {header}"))))
    secrets = doctor.secrets_from(env)
    scrubbed = (doctor.render_human(checks, secrets)
                + doctor.render_json(checks, secrets))

    for blob, layer in ((unscrubbed, "raw checks"), (scrubbed, "scrubbed render")):
        assert header not in blob, layer
        assert auth not in blob, layer
        assert WAF not in blob, layer
        assert JWT_SIG not in blob, layer
        for segment in auth.split("."):
            assert segment not in blob, f"{layer} leaked a JWT segment"

    # ...while still being useful: lengths and expiry survive.
    assert str(len(header)) in unscrubbed
    assert "days" in unscrubbed


def test_redaction_survives_percent_encoding(tmp_path):
    """Exact-substring scrubbing alone is defeated by any transformation of the
    value, and an encoded header is exactly what a URL-quoting client would log."""
    real_waf = "cf1a2b3c-0000-4d5e-8f90-abcdef123456:EQoAaBcD+eF/gHiJ:kLmNoPqRsTuV=="
    header = f"auth_token=eyJhbGciOiJIUzI1NiJ9.PAYLOADSEGMENT.SIGSEG; aws-waf-token={real_waf}"
    env = {"SKOOL_COOKIE": header, "SKOOL_GROUP": "my-slug"}

    leaked = doctor.check_live_auth(
        env, whoami=lambda: (_ for _ in ()).throw(RuntimeError(quote(header))))
    out = doctor.render_human([leaked], doctor.secrets_from(env))

    assert real_waf not in out
    assert quote(real_waf) not in out
    assert "cf1a2b3c-0000-4d5e-8f90-abcdef123456" not in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "<redacted>" in out


def test_redaction_catches_a_short_value_the_substring_pass_would_skip():
    """secrets_from ignores values under 8 chars (redacting 'true' would blank out
    ordinary words), so the structural pass has to cover them."""
    out = doctor.scrub("Skool said: auth_token=abc123; aws-waf-token=xy1", secrets=[])
    assert "abc123" not in out
    assert "xy1" not in out


def test_secrets_from_ignores_short_values_that_would_over_redact():
    env = {"SKOOL_COOKIE": f"tiny=ab; auth_token={_jwt(10)}"}
    assert all(len(s) >= 8 for s in doctor.secrets_from(env))
    assert "ab" not in doctor.secrets_from(env)


def test_scrub_replaces_every_occurrence():
    out = doctor.scrub("x LONGSECRETVALUE y LONGSECRETVALUE", ["LONGSECRETVALUE"])
    assert out == "x <redacted> y <redacted>"


def test_scrub_leaves_ordinary_diagnostic_text_alone():
    """Over-redaction would make doctor useless. The details it actually prints
    must survive a scrub against a real secrets list."""
    env = {"SKOOL_COOKIE": _header(_jwt(300))}
    secrets = doctor.secrets_from(env)
    for detail in ("3 cookies, 143 chars", "valid for 300 more days",
                   "browser installed", "authenticated, 'my-slug' resolved",
                   "no auth_token cookie"):
        assert doctor.scrub(detail, secrets) == detail
