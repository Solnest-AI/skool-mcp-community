#!/usr/bin/env python3
"""Unit tests for connect_skool.py — no browser, no network.

Covers the things that must never regress:
  1. The .env merge preserves every unrelated key and lands atomically, at 0600,
     with no readable temp file surviving any failure (including Ctrl-C).
  2. Nothing in the module can emit a cookie value or write a credential file.
  3. The login poll accepts a real login instead of stalling until timeout.

The browser leg itself (Playwright driving a real login) is verified manually —
see SETUP.md phase 2.
"""
import ast
import os

import pytest

import connect_skool as cs
import refresh_cookie as rc

HEADER = "auth_token=aa.bb.cc; aws-waf-token=WAFVALUE==; client_id=xyz"


# --- upsert_env_var --------------------------------------------------------------
def test_upsert_replaces_only_the_target_line():
    env = "SKOOL_COOKIE=OLD\nSKOOL_GROUP=old-slug\nSKOOL_GROUP_ID=abc123\n"
    out = cs.upsert_env_var(env, "SKOOL_GROUP", "new-slug")
    assert out == "SKOOL_COOKIE=OLD\nSKOOL_GROUP=new-slug\nSKOOL_GROUP_ID=abc123\n"


def test_upsert_does_not_confuse_group_with_group_id():
    env = "SKOOL_GROUP_ID=abc123\n"
    assert cs.upsert_env_var(env, "SKOOL_GROUP", "slug") == \
        "SKOOL_GROUP_ID=abc123\nSKOOL_GROUP=slug\n"


def test_upsert_preserves_unrelated_keys_comments_and_blanks():
    env = (
        "# my notes\n"
        "\n"
        "OPENAI_API_KEY=sk-keepme\n"
        "SKOOL_COOKIE=OLD\n"
        "SOME_OTHER=value with spaces = and equals\n"
    )
    out = cs.upsert_env_var(env, "SKOOL_COOKIE", "NEW")
    assert "# my notes\n" in out
    assert "OPENAI_API_KEY=sk-keepme\n" in out
    assert "SOME_OTHER=value with spaces = and equals\n" in out
    assert "SKOOL_COOKIE=NEW\n" in out
    assert "OLD" not in out


def test_upsert_appends_when_key_absent():
    assert cs.upsert_env_var("SKOOL_GROUP=slug\n", "SKOOL_COOKIE", "NEW") == \
        "SKOOL_GROUP=slug\nSKOOL_COOKIE=NEW\n"


def test_upsert_appends_to_file_without_trailing_newline():
    assert cs.upsert_env_var("SKOOL_GROUP=slug", "SKOOL_COOKIE", "NEW") == \
        "SKOOL_GROUP=slug\nSKOOL_COOKIE=NEW\n"


def test_upsert_handles_empty_file():
    assert cs.upsert_env_var("", "SKOOL_GROUP", "slug") == "SKOOL_GROUP=slug\n"


@pytest.mark.parametrize("env", [
    "SKOOL_COOKIE=OLD\nSKOOL_GROUP=slug\n",
    "SKOOL_GROUP=slug\n",
    "",
    "SKOOL_GROUP=slug",
    "SKOOL_COOKIE=OLD\nSKOOL_COOKIE=OLD2\n",
])
def test_upsert_is_byte_identical_to_refresh_cookie_helper(env):
    """connect_skool and the refresh job must agree on .env's shape, or one will
    quietly corrupt what the other wrote."""
    assert cs.upsert_env_var(env, "SKOOL_COOKIE", "NEW") == rc.replace_cookie_line(env, "NEW")


# --- read_env_var ------------------------------------------------------------------
def test_read_env_var_handles_quotes_export_and_equals(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# c\nexport SKOOL_GROUP_ID="gid=123"\nSKOOL_COOKIE=a=1; b=2\n',
                 encoding="utf-8")
    assert cs.read_env_var(str(p), "SKOOL_GROUP_ID") == "gid=123"
    assert cs.read_env_var(str(p), "SKOOL_COOKIE") == "a=1; b=2"


def test_read_env_var_missing_file_and_key(tmp_path):
    assert cs.read_env_var(str(tmp_path / "nope"), "SKOOL_COOKIE") == ""
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert cs.read_env_var(str(tmp_path / ".env"), "SKOOL_COOKIE") == ""


# --- write_env_atomic ------------------------------------------------------------
def test_write_env_atomic_creates_file_and_leaves_no_temp(tmp_path):
    env_path = str(tmp_path / ".env")
    cs.write_env_atomic(env_path, {"SKOOL_COOKIE": HEADER, "SKOOL_GROUP": "my-slug"})

    assert sorted(os.listdir(tmp_path)) == [".env"]  # no .env.tmp, no cookie json
    text = open(env_path, encoding="utf-8").read()
    assert f"SKOOL_COOKIE={HEADER}\n" in text
    assert "SKOOL_GROUP=my-slug\n" in text


def test_write_env_atomic_preserves_pre_existing_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep me\nANTHROPIC_API_KEY=sk-ant-keep\nSKOOL_COOKIE=STALE\nTRAILING=1\n",
        encoding="utf-8")

    cs.write_env_atomic(str(env_path), {"SKOOL_COOKIE": HEADER, "SKOOL_GROUP": "my-slug"})

    text = env_path.read_text(encoding="utf-8")
    assert "# keep me\n" in text
    assert "ANTHROPIC_API_KEY=sk-ant-keep\n" in text
    assert "TRAILING=1\n" in text
    assert "STALE" not in text
    assert f"SKOOL_COOKIE={HEADER}\n" in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_write_env_atomic_is_owner_only(tmp_path):
    env_path = str(tmp_path / ".env")
    cs.write_env_atomic(env_path, {"SKOOL_COOKIE": HEADER})
    assert oct(os.stat(env_path).st_mode & 0o777) == oct(0o600)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_write_env_atomic_tightens_a_previously_world_readable_env(tmp_path):
    """refresh_cookie.py writes .env without a chmod, so reconnecting must not
    inherit a loose mode from the file it replaces."""
    env_path = tmp_path / ".env"
    env_path.write_text("SKOOL_COOKIE=OLD\n", encoding="utf-8")
    os.chmod(env_path, 0o644)

    cs.write_env_atomic(str(env_path), {"SKOOL_COOKIE": HEADER})
    assert oct(os.stat(env_path).st_mode & 0o777) == oct(0o600)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_the_cookie_is_never_on_disk_at_a_loose_mode(tmp_path):
    """The temp file must be created 0600, not chmod-ed after the write: any gap
    exposes a full account-takeover token to every local account."""
    env_path = tmp_path / ".env"
    observed = {}
    real_fdopen = cs.os.fdopen

    def spy(fd, *a, **k):
        observed["mode_at_open"] = oct(os.fstat(fd).st_mode & 0o777)
        return real_fdopen(fd, *a, **k)

    cs.os.fdopen = spy
    try:
        cs.write_env_atomic(str(env_path), {"SKOOL_COOKIE": HEADER})
    finally:
        cs.os.fdopen = real_fdopen
    assert observed["mode_at_open"] == oct(0o600)


def test_write_env_atomic_failure_keeps_original_and_removes_temp(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("SKOOL_COOKIE=ORIGINAL\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(cs.os, "replace", boom)
    with pytest.raises(OSError):
        cs.write_env_atomic(str(env_path), {"SKOOL_COOKIE": HEADER})

    assert env_path.read_text(encoding="utf-8") == "SKOOL_COOKIE=ORIGINAL\n"
    assert sorted(os.listdir(tmp_path)) == [".env"]  # temp file cleaned up


def test_ctrl_c_mid_write_leaves_no_readable_temp_cookie(tmp_path, monkeypatch):
    """KeyboardInterrupt is a BaseException. An `except Exception` cleanup would
    miss it and strand .env.tmp holding the live cookie — and Ctrl-C is the
    expected reflex during a script that waits up to ten minutes."""
    env_path = tmp_path / ".env"

    def interrupt(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(cs.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cs.write_env_atomic(str(env_path), {"SKOOL_COOKIE": HEADER})

    assert os.listdir(tmp_path) == []


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_write_env_atomic_refuses_a_symlinked_temp_path(tmp_path):
    """A pre-planted .env.tmp symlink would redirect the cookie to the attacker's
    file AND leave .env permanently pointing there after os.replace."""
    env_path = tmp_path / ".env"
    target = tmp_path / "attacker.txt"
    target.write_text("", encoding="utf-8")
    os.symlink(target, str(env_path) + ".tmp")

    with pytest.raises(RuntimeError, match="symlink"):
        cs.write_env_atomic(str(env_path), {"SKOOL_COOKIE": HEADER})

    assert target.read_text(encoding="utf-8") == ""
    assert not env_path.exists()


def test_write_env_atomic_recovers_from_a_stale_temp_file(tmp_path):
    env_path = tmp_path / ".env"
    (tmp_path / ".env.tmp").write_text("junk from a killed run\n", encoding="utf-8")

    cs.write_env_atomic(str(env_path), {"SKOOL_COOKIE": HEADER})
    assert sorted(os.listdir(tmp_path)) == [".env"]
    assert HEADER in env_path.read_text(encoding="utf-8")


# --- SKOOL_GROUP_ID handling ------------------------------------------------------
def test_plan_writes_only_cookie_and_group_on_a_fresh_install(tmp_path):
    updates = cs.plan_env_updates(str(tmp_path / ".env"), "my-slug", "gid-1", HEADER)
    assert set(updates) == {"SKOOL_COOKIE", "SKOOL_GROUP"}


def test_plan_leaves_a_matching_group_id_alone(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SKOOL_GROUP_ID=gid-1\n", encoding="utf-8")
    updates = cs.plan_env_updates(str(env_path), "my-slug", "gid-1", HEADER)
    assert set(updates) == {"SKOOL_COOKIE", "SKOOL_GROUP"}


def test_plan_corrects_a_stale_group_id_from_another_community(tmp_path):
    """Reconnecting to a different community must not leave the old id behind —
    every MCP tool would keep reading and writing the previous group."""
    env_path = tmp_path / ".env"
    env_path.write_text("SKOOL_GROUP_ID=gid-OLD\n", encoding="utf-8")
    updates = cs.plan_env_updates(str(env_path), "my-slug", "gid-NEW", HEADER)
    assert updates["SKOOL_GROUP_ID"] == "gid-NEW"


def test_plan_blanks_a_stale_group_id_when_the_new_one_is_unknown(tmp_path):
    """If Skool stops returning an id, leaving the old one is the dangerous
    outcome. Blank it so the client resolves from the slug instead."""
    env_path = tmp_path / ".env"
    env_path.write_text("SKOOL_GROUP_ID=gid-OLD\n", encoding="utf-8")
    updates = cs.plan_env_updates(str(env_path), "my-slug", "", HEADER)
    assert updates["SKOOL_GROUP_ID"] == ""


# --- cookie harvesting ------------------------------------------------------------
def test_is_skool_domain_is_a_suffix_match_not_a_substring_match():
    assert cs.is_skool_domain(".skool.com")
    assert cs.is_skool_domain("www.skool.com")
    assert cs.is_skool_domain("SKOOL.COM")
    assert not cs.is_skool_domain("notskool.com")
    assert not cs.is_skool_domain("skool.com.evil.net")
    assert not cs.is_skool_domain("")


def test_cookie_header_from_keeps_only_skool_cookies():
    cookies = [
        {"name": "auth_token", "value": "aa.bb.cc", "domain": ".skool.com"},
        {"name": "aws-waf-token", "value": "WAF==", "domain": "www.skool.com"},
        {"name": "_ga", "value": "leak", "domain": ".google.com"},
        {"name": "spoof", "value": "leak2", "domain": "skool.com.evil.net"},
        {"name": "", "value": "nameless", "domain": ".skool.com"},
    ]
    header = cs.cookie_header_from(cookies)
    assert header == "auth_token=aa.bb.cc; aws-waf-token=WAF=="
    assert "leak" not in header
    assert cs.parse_cookie_header(header)["aws-waf-token"] == "WAF=="


AUTHED = [{"name": "auth_token", "value": "aa.bb.cc"}]


def test_is_logged_in_needs_an_auth_token():
    assert cs.is_logged_in([], ["https://www.skool.com/my-slug"]) is False
    assert cs.is_logged_in([{"name": "auth_token", "value": ""}],
                           ["https://www.skool.com/my-slug"]) is False


def test_is_logged_in_waits_while_every_page_is_still_on_login():
    assert cs.is_logged_in(AUTHED, ["https://www.skool.com/login"]) is False


def test_is_logged_in_accepts_when_any_page_has_moved_off_login():
    """The last tab is not the focused tab. An SSO popup parked on /login must
    not block a main window that is already showing the feed."""
    assert cs.is_logged_in(AUTHED, ["https://www.skool.com/my-slug",
                                    "https://accounts.google.com/login"]) is True


def test_is_logged_in_accepts_a_token_when_no_page_url_is_readable():
    """Member closed the tab once their feed loaded. The cookie is already valid;
    stalling to the timeout would throw away a completed login."""
    assert cs.is_logged_in(AUTHED, []) is True
    assert cs.is_logged_in(AUTHED, [""]) is True


def test_is_logged_in_does_not_iterate_a_bare_string_as_characters():
    assert cs.is_logged_in(AUTHED, "https://www.skool.com/login") is False


# --- group payload / picker ----------------------------------------------------------
def test_parse_groups_payload_extracts_slug_id_and_label():
    payload = {"groups": [
        {"id": "gid-a", "name": "alpha", "metadata": {"display_name": "Alpha Club"}},
        {"id": "gid-b", "name": "beta"},
        {"id": "gid-c"},  # no slug -> unusable
    ]}
    assert cs.parse_groups_payload(payload) == [
        {"slug": "alpha", "id": "gid-a", "label": "Alpha Club"},
        {"slug": "beta", "id": "gid-b", "label": ""},
    ]
    assert cs.parse_groups_payload({}) == []
    assert cs.parse_groups_payload(None) == []


GROUPS = [
    {"slug": "alpha", "id": "gid-a", "label": "Alpha Club"},
    {"slug": "beta", "id": "gid-b", "label": ""},
]


def test_choose_group_honours_preselection():
    assert cs.choose_group(GROUPS, "beta")["id"] == "gid-b"


def test_choose_group_rejects_an_unknown_slug(capsys):
    with pytest.raises(SystemExit) as e:
        cs.choose_group(GROUPS, "nope")
    assert e.value.code == 1
    assert "alpha" in capsys.readouterr().err  # tells the member their real options


def test_choose_group_auto_selects_a_lone_community():
    assert cs.choose_group(GROUPS[:1])["slug"] == "alpha"


def test_choose_group_fails_actionably_with_no_tty(monkeypatch, capsys):
    """Agent-driven runs have no stdin. Say how to finish instead of hanging, and
    say the login is already saved so it is not a wasted trip."""
    class NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(cs.sys, "stdin", NoTTY())
    with pytest.raises(SystemExit) as e:
        cs.choose_group(GROUPS)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "--group" in err
    assert "already saved" in err


def test_choose_group_rejects_an_account_with_no_communities():
    with pytest.raises(SystemExit):
        cs.choose_group([])


# --- verification ---------------------------------------------------------------
def test_check_membership_accepts_a_consistent_answer():
    info = {"active_group": "alpha", "active_group_id": "gid-a",
            "groups": [{"slug": "alpha", "id": "gid-a"}]}
    assert cs.check_membership(info, "alpha") == ""


def test_check_membership_catches_a_non_member_slug():
    """whoami's active_group is just the env var we set, so only the server's own
    group list can actually prove membership."""
    info = {"active_group": "ghost", "active_group_id": "gid-x",
            "groups": [{"slug": "alpha", "id": "gid-a"}]}
    assert "not a member" in cs.check_membership(info, "ghost")


def test_check_membership_catches_an_unresolved_group_id():
    info = {"active_group": "alpha", "active_group_id": "",
            "groups": [{"slug": "alpha"}]}
    assert "no group id" in cs.check_membership(info, "alpha")


# --- main(): never waste a completed login -----------------------------------------
def _stub_main(monkeypatch, tmp_path, groups=GROUPS):
    """Wire main() to tmp .env with the browser and network legs stubbed out."""
    env_path = tmp_path / ".env"
    monkeypatch.setattr(cs, "ENV", str(env_path))
    monkeypatch.setattr(cs, "verify", lambda slug, header: None)

    calls = {"browser": 0}

    def no_browser(timeout=0):
        calls["browser"] += 1
        return HEADER

    monkeypatch.setattr(cs, "wait_for_login", no_browser)
    monkeypatch.setattr(cs, "fetch_groups", lambda header, fatal=True: groups)
    return env_path, calls


def test_group_flag_reuses_a_saved_session_instead_of_logging_in_again(
        monkeypatch, tmp_path):
    """The whole point of saving the cookie early: the --group re-run must not
    drag the member back through a second browser login."""
    env_path, calls = _stub_main(monkeypatch, tmp_path)
    env_path.write_text(f"SKOOL_COOKIE={HEADER}\n", encoding="utf-8")

    assert cs.main(["--group", "beta"]) == 0
    assert calls["browser"] == 0

    text = env_path.read_text(encoding="utf-8")
    assert "SKOOL_GROUP=beta\n" in text


def test_a_login_is_persisted_even_when_the_picker_cannot_prompt(
        monkeypatch, tmp_path, capsys):
    """Member logs in for up to ten minutes, then the picker finds no tty. The
    cookie must already be on disk so the --group re-run is instant."""
    env_path, calls = _stub_main(monkeypatch, tmp_path)

    class NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(cs.sys, "stdin", NoTTY())
    with pytest.raises(SystemExit):
        cs.main([])

    assert calls["browser"] == 1
    assert f"SKOOL_COOKIE={HEADER}\n" in env_path.read_text(encoding="utf-8")
    assert "--group" in capsys.readouterr().err


def test_a_stale_saved_cookie_falls_back_to_a_fresh_login(monkeypatch, tmp_path):
    env_path, calls = _stub_main(monkeypatch, tmp_path)
    env_path.write_text("SKOOL_COOKIE=EXPIRED\n", encoding="utf-8")
    # fatal=False probe returns None for a rejected cookie
    monkeypatch.setattr(cs, "fetch_groups",
                        lambda header, fatal=True: None if header == "EXPIRED" else GROUPS)

    assert cs.main(["--group", "alpha"]) == 0
    assert calls["browser"] == 1
    assert f"SKOOL_COOKIE={HEADER}\n" in env_path.read_text(encoding="utf-8")


def test_main_rejects_a_nonsense_timeout(monkeypatch, tmp_path):
    _stub_main(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        cs.main(["--timeout", "0"])


def test_humanize_seconds_reads_sensibly():
    assert cs.humanize_seconds(30) == "30 seconds"
    assert cs.humanize_seconds(600) == "10 minutes"
    assert "0 minutes" not in cs.humanize_seconds(45)


# --- the security contract -------------------------------------------------------
def test_progress_and_success_lines_never_contain_the_cookie(capsys):
    sentinel = "SUPERSECRETCOOKIEVALUE12345"
    header = f"auth_token=aa.{sentinel}.cc; aws-waf-token={sentinel}"

    print(cs.summarize_capture(header))
    print(cs.report_success("my-slug"))
    out = capsys.readouterr().out

    assert sentinel not in out
    assert header not in out
    assert "2 cookies" in out
    assert "Connected to my-slug" in out


def test_success_line_matches_the_spec_string_exactly():
    assert cs.report_success("my-slug") == "✅ Connected to my-slug. Wrote .env (cookie hidden)."


def _module_tree():
    return ast.parse(open(cs.__file__, encoding="utf-8").read())


def _code_strings(tree):
    """String literals that are actual code, docstrings excluded — so these tests
    assert about behaviour rather than about prose."""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docs.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docs]


# Every mechanism that could dump the live session somewhere other than .env.
BANNED_CALLS = {"storage_state", "record_har_path", "screenshot", "write_text",
                "write_bytes", "dump", "dumps_to_file", "copyfile", "copy2"}
BANNED_KWARGS = {"storage_state", "record_har_path", "record_video_dir",
                 "user_data_dir", "downloads_path"}
BANNED_LITERALS = ("skool-cookies", "skool-capture", "storage_state", ".har",
                   "cookies.json")


def test_no_code_literal_names_a_credential_dump_file():
    """Regression guard on the bug this script replaces: capture_skool.py wrote
    the live session to skool-cookies.json."""
    for literal in _code_strings(_module_tree()):
        low = literal.lower()
        for banned in BANNED_LITERALS:
            assert banned not in low, f"connect_skool.py must not reference {banned}"


def test_no_call_can_persist_the_session_outside_dot_env():
    """The likeliest regression is a Playwright keyword (storage_state=...) or a
    Path.write_text, neither of which is a plain string literal."""
    tree = _module_tree()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        assert name not in BANNED_CALLS, f"connect_skool.py must not call {name}()"
        for kw in node.keywords:
            assert kw.arg not in BANNED_KWARGS, \
                f"connect_skool.py must not pass {kw.arg}="


def test_the_only_files_it_opens_are_dot_env_and_its_temp():
    tree = _module_tree()
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fn in ("open",) and node.args:
                targets.append(getattr(node.args[0], "id", ast.dump(node.args[0])))
    assert targets, "expected connect_skool to open at least one file"
    assert set(targets) <= {"env_path", "tmp"}, targets


def test_no_die_call_interpolates_an_exception_value():
    """`die(f"...{e}")` would put a third-party message — potentially quoting the
    request headers — in front of the member. Only `type(e).__name__` is allowed."""
    tree = _module_tree()
    caught = {h.name for h in ast.walk(tree)
              if isinstance(h, ast.ExceptHandler) and h.name}
    assert caught, "expected connect_skool to bind at least one exception variable"

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "die"):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.JoinedStr):
                continue
            for part in arg.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                # `{e}` is a leak risk; `{type(e).__name__}` is fine.
                if isinstance(part.value, ast.Name) and part.value.id in caught:
                    offenders.append(part.value.id)
    assert not offenders, f"die() interpolated a raw exception value: {offenders}"
