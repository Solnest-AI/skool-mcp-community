#!/usr/bin/env bash
# Makes a student's copy a plain folder with no link back to GitHub.
#
#   bash detach_from_github.sh
#
# Part of turning on automatic updates (SETUP.md Phase 7). Members install with
# `git clone`, and a clone stays attached to our repo (Solnest-AI's comping agent
# learned this at the 2026-09-29 summit: "save" turned into a push to our GitHub).
# update.py replaces files by release and refuses a folder that git still owns,
# so an untouched member copy becomes a plain folder. Ported from
# Solnest-AI/solnest-str-comping-agent scripts/detach_from_github.sh.
#
# It removes .git only from an UNTOUCHED copy of the public repo. It keeps it,
# and says why, when the copy is someone's working copy: another origin, local
# edits to tracked files, commits, branches or stashes not on GitHub. A
# developer keeps git on a clean checkout with `touch .git/skool-keep-attached`
# (permanent) or SKOOL_KEEP_GIT=1 (one run).
#
# Always exits 0: a copy it keeps attached must not stop setup.
set -uo pipefail

cd "$(dirname "$0")"
say() { printf '[setup] %s\n' "$*"; }
REPO="solnest-ai/skool-mcp-community"   # compared lower-case: GitHub ignores case

if [ ! -e .git ]; then
    say "GitHub: not attached (plain folder)."
    exit 0
fi
if [ -n "${SKOOL_KEEP_GIT:-}" ] || [ -e .git/skool-keep-attached ]; then
    say "GitHub: still attached (a developer's copy: SKOOL_KEEP_GIT or .git/skool-keep-attached)."
    exit 0
fi

# A Mac without developer tools has a /usr/bin/git stub that opens an install
# dialog instead of answering (2026-09-28). Only trust git when it answers.
git_works() { git --version >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; }

if ! git_works; then
    # Nothing can use this .git without git, and it cannot be checked for
    # someone's work, so it is left alone rather than deleted blind.
    say "GitHub: left as is. Git does not answer on this computer, so nothing can use the link."
    exit 0
fi
origin="$(git remote get-url origin 2>/dev/null || true)"
case "$(printf '%s' "$origin" | tr '[:upper:]' '[:lower:]')" in
    *"$REPO"|*"$REPO".git|*"$REPO"/) ;;
    *)  say "GitHub: kept attached. This folder's git is not a copy of $REPO (origin: ${origin:-none})."
        exit 0 ;;
esac
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    say "GitHub: kept attached. The code here has local edits (a working copy, not a fresh download)."
    exit 0
fi
if [ "$(git branch --format='%(refname:short)' 2>/dev/null | grep -vxc main)" != "0" ] \
   || [ -n "$(git log --branches --not --remotes --oneline 2>/dev/null | head -1)" ] \
   || [ -n "$(git stash list 2>/dev/null | head -1)" ]; then
    say "GitHub: kept attached. This copy has branches, commits or stashed work that are not on GitHub."
    exit 0
fi
# Last chance to ask git which files are the tool's. update.py reads
# this manifest to know what it may replace or remove on the first update, and
# which files the member edited since. git writes it, so this works before
# Python is set up: "<blob hash> <path>" per line, hashed as the bytes on disk.
MANIFEST=".skool-mcp-files.txt"
write_manifest() {
    local tmp="$MANIFEST.tmp" paths hashes
    paths="$(git ls-files -z | tr '\0' '\n')" || return 1
    [ -n "$paths" ] || return 1
    hashes="$(printf '%s\n' "$paths" | git hash-object --no-filters --stdin-paths)" || return 1
    [ "$(printf '%s\n' "$paths" | wc -l | tr -d ' ')" = "$(printf '%s\n' "$hashes" | wc -l | tr -d ' ')" ] || return 1
    {
        echo "# Files the Skool MCP shipped (update.py). Do not edit."
        paste -d' ' <(printf '%s\n' "$hashes") <(printf '%s\n' "$paths")
    } > "$tmp" && mv -f "$tmp" "$MANIFEST"
}
if ! write_manifest; then
    rm -f "$MANIFEST.tmp" "$MANIFEST"
    say "GitHub: could not list the tool's files; the first update will replace files but remove none."
fi
git remote remove origin >/dev/null 2>&1 || true

# Git makes its object files read-only; on Windows plain rm refuses them.
chmod -R u+w .git 2>/dev/null || true
rm -rf .git 2>/dev/null
if [ -e .git ]; then
    say "GitHub: could not remove the .git folder (another program has it open). The GitHub link is removed;"
    say "        close other windows on this folder and run: bash detach_from_github.sh"
    exit 0
fi
say "GitHub: detached. This is now a plain folder, not linked to GitHub. It updates itself"
say "        from published releases."
exit 0
