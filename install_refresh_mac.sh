#!/usr/bin/env bash
# Install the launchd job that refreshes your Skool cookie every 2 days (macOS).
#
#   bash install_refresh_mac.sh
#
# Run it from wherever you put this folder — the script finds itself, so there is
# no path to edit.
#
# ─────────────────────────────────────────────────────────────────────────────
# macOS TCC / launchd knowledge encoded below. Learned the hard way; don't
# "simplify" any of it away.
#
#  1. launchd itself has NO Full Disk Access. If any path launchd must open on the
#     job's behalf — StandardOutPath, StandardErrorPath, WorkingDirectory — sits in
#     a TCC-protected folder (~/Documents, ~/Desktop, ~/Downloads), launchd aborts
#     BEFORE Python ever starts, with EX_CONFIG (exit 78) and no useful error.
#     So: the log always lives in ~/Library/Logs, and this plist sets no
#     WorkingDirectory key at all. refresh_cookie.py derives every path it needs
#     from __file__, so it doesn't want one.
#
#  2. The interpreter DOES need Full Disk Access to read this folder's .env — but
#     ONLY if this folder is inside a protected location. Keeping the repo at
#     ~/skool-mcp skips that entire ordeal, so the FDA instructions below print
#     only when they actually apply.
#
#  3. Full Disk Access is granted to one specific binary path. A Homebrew symlink
#     (.venv/bin/python3.x -> ../Cellar/python@3.x/<version>/...) points somewhere
#     new after every `brew upgrade`, silently revoking the grant. When we're in
#     the protected case we replace that symlink with a real copy at the stable
#     in-repo path, so the grant keeps matching.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# pwd -P resolves symlinks: a symlinked folder must not hide that the real
# location is under ~/Documents, or check #2 below would miss it.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LABEL="com.skoolmcp.cookie-refresh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGFILE="$HOME/Library/Logs/skool-cookie-refresh.log"   # OUTSIDE ~/Documents (see #1)
SCRIPT="$REPO/refresh_cookie.py"

die() { echo "ERROR: $*" >&2; exit 1; }

# Portable `readlink -f`: older macOS readlink has no -f. Returns a normalized
# absolute path — the FDA target gets pasted into Finder's "Go to Folder", where a
# leftover ".." is at best confusing.
resolve_path() {
  local p="$1" target dir base
  while [ -L "$p" ]; do
    target="$(readlink "$p")"
    case "$target" in
      /*) p="$target" ;;
      *)  p="$(cd "$(dirname "$p")" && pwd -P)/$target" ;;
    esac
  done
  dir="$(dirname "$p")"; base="$(basename "$p")"
  if dir="$(cd "$dir" 2>/dev/null && pwd -P)"; then
    printf '%s/%s\n' "$dir" "$base"
  else
    printf '%s\n' "$p"
  fi
}

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

[ -f "$SCRIPT" ] || die "refresh_cookie.py not found next to this script (looked in $REPO)."

# ── Is this folder somewhere macOS TCC protects? ─────────────────────────────
# Compare physical path to physical path. REPO went through `pwd -P`, so $HOME must
# too: if $HOME contains a symlinked component the prefix test silently fails, and
# we'd skip the FDA instructions for a member who genuinely needs them.
HOME_REAL="$(cd "$HOME" 2>/dev/null && pwd -P || printf '%s' "$HOME")"
case "$REPO/" in
  "$HOME_REAL/Documents/"*|"$HOME_REAL/Desktop/"*|"$HOME_REAL/Downloads/"*) PROTECTED=1 ;;
  "$HOME/Documents/"*|"$HOME/Desktop/"*|"$HOME/Downloads/"*)                 PROTECTED=1 ;;
  *) PROTECTED=0 ;;
esac

# ── Resolve the interpreter: prefer uv, fall back to the venv's python ───────
find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
  local c
  for c in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    [ -x "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  return 1
}

PROGRAM_ARGS=()
if UV_BIN="$(find_uv)"; then
  # --directory makes uv resolve the project and the script from this folder,
  # which matters because the plist deliberately sets no WorkingDirectory (#1).
  PROGRAM_ARGS=("$UV_BIN" run --directory "$REPO" refresh_cookie.py)
  RUNNER="uv  ($UV_BIN)"
  # TCC matches the real binary, not the Homebrew symlink that points at it.
  FDA_TARGET="$(resolve_path "$UV_BIN")"
  FDA_EXTRA="Because that is uv's real (versioned) path, upgrading uv can revoke the grant.
     Moving this folder to ~/skool-mcp avoids the problem permanently."
  [ -f "$REPO/pyproject.toml" ] || echo "WARNING: no pyproject.toml here — 'uv run' may not resolve dependencies."
else
  PYBIN=""
  for c in "$REPO/.venv/bin/python" "$REPO/.venv/bin/python3"; do
    [ -x "$c" ] && { PYBIN="$c"; break; }
  done
  [ -n "$PYBIN" ] || die "No uv on PATH and no .venv here.
  Install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh
  then run:    uv sync   (from $REPO)"

  # Pin the interpreter only where it buys something: a protected folder needs a
  # stable FDA target that survives `brew upgrade python` (see #3).
  if [ "$PROTECTED" -eq 1 ] && [ -L "$PYBIN" ]; then
    REAL="$(resolve_path "$PYBIN")"
    cp "$REAL" "$PYBIN.tmp" && mv "$PYBIN.tmp" "$PYBIN"
    echo "Pinned interpreter -> real copy at: $PYBIN"
  fi
  PROGRAM_ARGS=("$PYBIN" "$SCRIPT")
  RUNNER="venv python  ($PYBIN)"
  FDA_TARGET="$PYBIN"
  FDA_EXTRA=""
fi

# ── Write and load the LaunchAgent ───────────────────────────────────────────
ARGS_XML=""
for a in "${PROGRAM_ARGS[@]}"; do
  ARGS_XML+="    <string>$(xml_escape "$a")</string>"$'\n'
done

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$(xml_escape "$LABEL")</string>
  <key>ProgramArguments</key>
  <array>
${ARGS_XML}  </array>
  <key>StartInterval</key><integer>172800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOGFILE")</string>
</dict>
</plist>
EOF

# No WorkingDirectory key on purpose — see #1 in the header.

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Installed $LABEL — runs at login and every 2 days."
echo "  Folder: $REPO"
echo "  Runner: $RUNNER"
echo "  Log:    $LOGFILE"

[ -f "$REPO/.env" ] || echo "  WARNING: no .env here yet. Run connect_skool.py first, or the job will fail."

if [ "$PROTECTED" -eq 1 ]; then
  echo
  echo "⚠️  This folder is inside a macOS-protected location (Documents/Desktop/Downloads),"
  echo "    so one extra one-time step is required — macOS blocks background jobs from"
  echo "    reading .env there."
  echo
  echo "    EASIEST FIX: move this folder to your home directory and re-run this script:"
  echo "      mv \"$REPO\" ~/skool-mcp && bash ~/skool-mcp/install_refresh_mac.sh"
  echo
  echo "    OR grant Full Disk Access:"
  echo "      1. System Settings > Privacy & Security > Full Disk Access"
  echo "      2. Click +  (authenticate)"
  echo "      3. Press Cmd+Shift+G, paste this exact path, then Open:"
  echo "           $FDA_TARGET"
  echo "      4. Make sure its toggle is ON."
  if [ -n "$FDA_EXTRA" ]; then echo "     $FDA_EXTRA"; fi
fi

echo
echo "Test it now, in the same context launchd will use:"
echo "  launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "  tail -n 3 \"$LOGFILE\"   # expect: OK: cookie refreshed"
echo
echo "Uninstall:"
echo "  launchctl bootout gui/\$(id -u)/$LABEL; rm -f \"$PLIST\""
