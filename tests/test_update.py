"""update.py: a member's copy updates by published release, keeps their own files, can go back.

Ported from Solnest-AI/solnest-str-comping-agent tests/test_update.py and
tests/test_detach_from_github.py, for the same updater in this repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import update  # noqa: E402

V1 = {"VERSION": "1.1.0\n", "server.py": "print('v1')\n", "scripts/old.py": "print('old')\n",
      "tests/test_old.py": "def test_old(): pass\n", "scripts/legacy/helper.py": "x = 1\n",
      ".env.example": "SKOOL_COOKIE=\n"}
V2 = {"VERSION": "1.2.0\n", "server.py": "print('v2')\n", "scripts/new.py": "print('new')\n",
      ".env.example": "SKOOL_COOKIE=\nSKOOL_GROUP=\n"}
# Everything the member owns. None of it is in a release, and no release may touch it.
USER = {".env": "SKOOL_COOKIE=keep-me\nSKOOL_GROUP=mine\n", ".env.local": "SECRET=1\n", ".env.tmp": "x\n",
        "refresh.log": "OK\n", "cookie.txt": "c", "key.txt": "k", "session.txt": "s",
        "skool-capture.json": "{}", "skool-cookies.json": "{}", "storage_state.json": "{}",
        "my-capture.json": "{}", "x-cookies.json": "{}", "network.har": "{}",
        ".venv/bin/python": "py", ".claude/settings.local.json": "{}"}


def _tarball(files: dict[str, str], top: str = "skool-mcp-community-1.2.0", extra=None) -> bytes:
    """What github.com/<repo>/archive/refs/tags/<tag>.tar.gz serves: one top folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return buf.getvalue()


def _plain_copy(tmp_path: Path, files=V1, manifest: bool = True, user=USER) -> Path:
    """A member's copy as a tarball or zip install leaves it: byte for byte what the
    release shipped (write_bytes, because write_text writes CRLF on Windows)."""
    root = tmp_path / "skool-mcp-community"
    for rel, body in {**files, **user}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body.encode())
    if manifest:
        update.write_manifest(root, list(files))
    return root


def _fake_release(monkeypatch, releases=None, latest="v1.2.0", blob=None) -> list[str]:
    """GitHub, offline: `latest` is the newest tag, `releases` maps each tag to its files."""
    releases = releases or {"v1.2.0": V2}
    calls: list[str] = []
    monkeypatch.setattr(update, "latest_tag", lambda timeout=20: latest)

    def download(tag, timeout=60):
        calls.append(tag)
        if blob is not None:
            return blob
        if tag not in releases:
            raise update.UpdateError(f"could not download {tag} from GitHub (HTTPError)")
        return _tarball(releases[tag], top=f"skool-mcp-community-{tag.lstrip('v')}")
    monkeypatch.setattr(update, "download", download)
    return calls


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)).replace(os.sep, "/"): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _without_backups(snap: dict[str, bytes]) -> dict[str, bytes]:
    return {k: v for k, v in snap.items() if not k.startswith(update.BACKUP_DIR + "/")}


# ── Updating ───────────────────────────────────────────────────────────
def test_an_update_replaces_the_tool_and_keeps_every_member_file(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    _fake_release(monkeypatch)
    report = update.update(root)
    assert report["status"] == "updated" and report["from"] == "1.1.0" and report["to"] == "1.2.0"
    assert (root / "server.py").read_text(encoding="utf-8") == "print('v2')\n"
    assert (root / "scripts/new.py").exists()
    assert "SKOOL_GROUP" in (root / ".env.example").read_text(encoding="utf-8"), "the shipped example is the tool's"
    for gone in ("scripts/old.py", "tests/test_old.py", "scripts/legacy/helper.py"):
        assert not (root / gone).exists(), f"{gone}: a file the release dropped would linger and can break setup"
    assert not (root / "scripts/legacy").exists(), "a folder the release emptied goes too"
    for rel, body in USER.items():
        assert (root / rel).read_text(encoding="utf-8") == body, f"{rel} must survive an update"
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == "1.2.0"
    assert set(update.load_manifest(root)) == set(V2)
    assert sorted(report["removed"]) == ["scripts/legacy/helper.py", "scripts/old.py", "tests/test_old.py"]
    assert report["added"] == ["scripts/new.py"]


def test_a_release_can_never_write_over_the_members_files(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    hostile = {**V2, **{rel: "from the release\n" for rel in USER}}
    _fake_release(monkeypatch, releases={"v1.2.0": hostile})
    update.update(root)
    for rel, body in USER.items():
        assert (root / rel).read_text(encoding="utf-8") == body, f"{rel} was overwritten by a release"
    assert not set(USER) & set(update.load_manifest(root)), "member files never enter the manifest"


def test_an_edited_file_is_backed_up_and_named(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    (root / "server.py").write_text("print('my tweak')\n", encoding="utf-8")
    _fake_release(monkeypatch)
    report = update.update(root)
    assert report["edited"] == ["server.py"]
    assert (Path(report["backup"]) / "server.py").read_text(encoding="utf-8") == "print('my tweak')\n"


def test_undo_puts_back_exactly_the_previous_version(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch)
    update.update(root)
    report = update.undo(root)
    assert report["to"] == "1.1.0" and report["from"] == "1.2.0"
    assert _snapshot(root) == before, "one step back is exactly where the member was"
    assert not (root / update.BACKUP_DIR).exists()


def test_undo_with_nothing_to_undo_says_so(tmp_path):
    root = _plain_copy(tmp_path)
    with pytest.raises(update.UpdateError, match="no earlier version"):
        update.undo(root)


def test_already_current_downloads_nothing(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path, files={**V1, "VERSION": "1.2.0\n"})
    calls = _fake_release(monkeypatch)
    assert update.update(root)["status"] == "current" and calls == []


def test_a_folder_still_linked_to_git_is_not_overwritten(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    (root / ".git").mkdir()
    calls = _fake_release(monkeypatch)
    with pytest.raises(update.UpdateError, match="git pull"):
        update.update(root)
    assert calls == [] and (root / "server.py").read_text(encoding="utf-8") == "print('v1')\n"


@pytest.mark.parametrize("evil", ["../escape.py", "/etc/passwd", "link"])
def test_a_tarball_that_reaches_outside_the_folder_is_refused(tmp_path, monkeypatch, evil):
    info = tarfile.TarInfo(evil if evil.startswith("/") else f"skool-mcp-community-1.2.0/{evil}")
    if evil == "link":
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
    else:
        info.size = 1
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch, blob=_tarball(V2, extra=[info]))
    with pytest.raises(update.UpdateError):
        update.update(root)
    assert _snapshot(root) == before and not (tmp_path / "escape.py").exists()


def test_a_release_without_a_version_file_is_refused(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch, releases={"v1.2.0": {k: v for k, v in V2.items() if k != "VERSION"}})
    with pytest.raises(update.UpdateError, match="VERSION"):
        update.update(root)
    assert _snapshot(root) == before


def test_a_release_labelled_with_the_wrong_version_is_refused(tmp_path, monkeypatch):
    """Tagged v1.2.0 but VERSION still says 1.1.0 (a forgotten bump): installing it would
    leave the copy 'behind' forever, re-downloading on every update and nagging every run."""
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch, releases={"v1.2.0": {**V2, "VERSION": "1.1.0\n"}})
    with pytest.raises(update.UpdateError, match="1.1.0"):
        update.update(root)
    assert _snapshot(root) == before


def test_a_failure_halfway_rolls_back(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch)
    real = update._write

    def flaky(path, data):
        if path.name == "new.py":
            raise OSError("disk full")
        real(path, data)
    monkeypatch.setattr(update, "_write", flaky)
    with pytest.raises(update.UpdateError, match="put back"):
        update.update(root)
    assert _without_backups(_snapshot(root)) == before


def test_no_network_is_a_clear_message_and_changes_nothing(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)

    def offline(timeout=20):
        raise update.UpdateError("could not reach GitHub (URLError)")
    monkeypatch.setattr(update, "latest_tag", offline)
    with pytest.raises(update.UpdateError, match="could not reach GitHub"):
        update.update(root)
    assert _snapshot(root) == before


def test_a_copy_without_a_manifest_learns_its_files_from_its_own_release(tmp_path, monkeypatch):
    """A tarball or zip install never had .git, so detach never wrote a manifest. Its VERSION
    names the release it came from, and that release lists exactly what it shipped."""
    root = _plain_copy(tmp_path, manifest=False)
    (root / "my-notes.md").write_text("mine\n", encoding="utf-8")
    calls = _fake_release(monkeypatch, releases={"v1.2.0": V2, "v1.1.0": V1})
    (root / "server.py").write_text("print('my tweak')\n", encoding="utf-8")
    report = update.update(root)
    assert "v1.1.0" in calls
    assert not (root / "scripts/old.py").exists() and not (root / "tests/test_old.py").exists()
    assert (root / "my-notes.md").read_text(encoding="utf-8") == "mine\n", "a file no release shipped is the member's"
    assert report["edited"] == ["server.py"]


def test_a_copy_from_before_versions_removes_nothing_it_cannot_prove_is_ours(tmp_path, monkeypatch):
    """Copies from before VERSION existed (the 2026-09-29 summit) have no manifest and no
    version: the update replaces the tool's files and leaves everything else alone."""
    v0 = {k: v for k, v in V1.items() if k != "VERSION"}
    root = _plain_copy(tmp_path, files=v0, manifest=False)
    (root / "my-notes.md").write_text("mine\n", encoding="utf-8")
    calls = _fake_release(monkeypatch)
    report = update.update(root)
    assert calls == ["v1.2.0"] and report["from"] == "unknown" and report["removed"] == []
    assert (root / "my-notes.md").exists() and (root / "scripts/old.py").exists()
    assert (root / "server.py").read_text(encoding="utf-8") == "print('v2')\n"
    update.undo(root)
    assert not (root / "VERSION").exists() and (root / "server.py").read_text(encoding="utf-8") == "print('v1')\n"


def test_only_the_last_three_backups_are_kept(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    for minor in (2, 3, 4, 5):
        tag = f"v1.{minor}.0"
        _fake_release(monkeypatch, latest=tag, releases={tag: {**V2, "VERSION": f"1.{minor}.0\n"}})
        update.update(root)
    assert len([p for p in (root / update.BACKUP_DIR).iterdir() if p.is_dir()]) == update.KEEP_BACKUPS
    assert update.undo(root)["to"] == "1.4.0"


def test_the_latest_release_comes_from_the_github_redirect_not_the_api(monkeypatch):
    """The API allows 60 requests an hour per network, and a classroom shares one."""
    seen = []

    class Answer:
        def __init__(self, url):
            self.url = url

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return Answer("https://github.com/Solnest-AI/skool-mcp-community/releases/tag/v1.4.2")
    monkeypatch.setattr(update.urllib.request, "urlopen", fake_urlopen)
    assert update.latest_tag() == "v1.4.2"
    assert seen == ["https://github.com/Solnest-AI/skool-mcp-community/releases/latest"]
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda req, timeout=None: Answer("https://github.com/Solnest-AI/skool-mcp-community/releases"))
    with pytest.raises(update.UpdateError, match="no published release"):
        update.latest_tag()


# ── The manifest ───────────────────────────────────────────────────────
def test_the_manifest_round_trips_paths_with_spaces(tmp_path):
    root = tmp_path / "copy"
    (root / "docs").mkdir(parents=True)
    (root / "docs/read me.md").write_bytes(b"hi\n")
    (root / "server.py").write_bytes(b"x\n")
    update.write_manifest(root, ["docs/read me.md", "server.py", ".env"])
    assert update.load_manifest(root) == {"docs/read me.md": update.file_hash(b"hi\n"),
                                          "server.py": update.file_hash(b"x\n")}


@pytest.mark.skipif(shutil.which("git") is None, reason="git needed")
def test_the_manifest_hash_is_gits_own(tmp_path):
    """detach_from_github.sh runs before Python is installed, so it hashes with git
    (`git hash-object --no-filters`). The updater must compute the same value."""
    f = tmp_path / "a.py"
    f.write_bytes(b"print('hi')\r\nemoji \xf0\x9f\x8f\xa0\n")
    git = subprocess.run(["git", "hash-object", "--no-filters", str(f)], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert update.file_hash(f.read_bytes()) == git


def test_a_damaged_manifest_counts_as_none(tmp_path):
    root = tmp_path / "copy"
    root.mkdir()
    (root / update.MANIFEST).write_text("not a manifest\n\x00\n", encoding="utf-8")
    assert update.load_manifest(root) == {}


# ── detach_from_github.sh (a fresh `git clone` becomes a plain folder) ─
SCRIPT = ROOT / "detach_from_github.sh"
PUBLIC = "https://github.com/Solnest-AI/skool-mcp-community.git"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git_bash():
    """Git's bash, which is what Claude Code runs (a bare "bash" on Windows can be WSL's)."""
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    for parent in (Path(git).resolve().parents if git else []):
        if (parent / "bin" / "bash.exe").exists():
            return str(parent / "bin" / "bash.exe")
    return None


needs_git = pytest.mark.skipif(_git_bash() is None or shutil.which("git") is None, reason="git and Git Bash needed")


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _member_clone(tmp_path: Path, origin: str = PUBLIC) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "skool-mcp"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    shutil.copy(SCRIPT, work / SCRIPT.name)
    (work / "server.py").write_text("print('hi')\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "remote", "set-url", "origin", origin)
    return work


def _detach(work: Path, **env: str) -> str:
    r = subprocess.run([_git_bash(), str(work / SCRIPT.name)], cwd=work, env={**ENV, **env},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "setup must never stop on this step"
    return r.stdout


@needs_git
def test_a_fresh_clone_is_detached_and_remembers_its_files(tmp_path):
    work = _member_clone(tmp_path)
    (work / ".env").write_text("SKOOL_COOKIE=keep-me\n", encoding="utf-8")  # gitignored in real life
    assert "detached" in _detach(work)
    assert not (work / ".git").exists()
    files = update.load_manifest(work)
    assert set(files) == {"server.py", "detach_from_github.sh"}, "git's tracked files, never .env"
    assert (work / ".env").read_text(encoding="utf-8") == "SKOOL_COOKIE=keep-me\n"
    assert "not attached" in _detach(work), "running it again is a no-op"


@needs_git
@pytest.mark.parametrize("change", ["edit", "other_origin"])
def test_a_working_copy_is_never_touched(tmp_path, change):
    work = _member_clone(tmp_path, origin="https://github.com/someone/fork.git" if change == "other_origin" else PUBLIC)
    if change == "edit":
        (work / "server.py").write_text("print('changed')\n", encoding="utf-8")
    assert "kept attached" in _detach(work)
    assert (work / ".git").is_dir() and not (work / update.MANIFEST).exists()


@needs_git
def test_a_developer_can_opt_out(tmp_path):
    work = _member_clone(tmp_path)
    (work / ".git" / "skool-keep-attached").touch()
    assert "still attached" in _detach(work) and (work / ".git").is_dir()


def test_the_repo_ships_a_version_and_scripts_keep_lf_endings():
    assert update.parse_version((ROOT / "VERSION").read_text(encoding="utf-8"))
    assert b"\r\n" not in SCRIPT.read_bytes()
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".update-backup/" in ignored and update.MANIFEST in ignored


def test_the_updater_and_runner_need_nothing_but_python():
    import ast
    for name in ("update.py", "solnest_autoupdate.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert mods <= set(sys.stdlib_module_names) | {"__future__"}, (name, mods - set(sys.stdlib_module_names))
