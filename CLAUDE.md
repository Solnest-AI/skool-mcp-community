# Skool MCP

Setting this up? Read `SETUP.md` and follow its seven phases in order. It is a runbook for you to
execute, not a doc for the member to read. Do not skip a gate.

Credential hard rule: never ask the member to paste their Skool cookie into the chat (it is a full
account-takeover token), and never print or read the contents of `.env`. `connect_skool.py` writes
it, `doctor.py` checks it.

## Your own changes (read first, every time)

Before anything else, read `~/.solnest/skool-mcp/my-instructions.md` if it exists and
follow it. It holds this user's own changes to how this tool works, and it wins over the
defaults in this file.

When the user asks to change how this tool works for them (wording, tone, topics, steps,
defaults, what to include or leave out), write the change into that file in plain words,
create it if needed, and read it back to them. **Never edit this tool's own files for
that.** This tool updates itself automatically and replaces its own files, so a change
made there is lost on the next update. Their Skool login (`.env`) is never touched by an
update either.

## Updates

Updates install themselves (SETUP.md Phase 7): once a day, in the background when Claude Code
starts. When the member asks to update now, or asks whether it is updating itself, follow
SETUP.md > "Updating".
