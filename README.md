# Skool MCP

Give Claude access to your Skool community. Posts, comments, members, classroom, and your DM inbox.

Ask Claude things like "what did people post this week?", "draft a reply to the last 5 DMs",
or "pull my member list into a spreadsheet". It runs on your computer, using your own Skool login.

Unofficial. Not built by Skool, not affiliated with Skool.

## What you can do with it

15 tools out of the box. The read ones are safe to fire off. The write ones post publicly under
your name, so Claude should confirm the wording with you first.

If you run the community, there are 9 more admin tools (approve or decline join requests, edit or
delete posts, manage calendar events, invite by email). Those are off until you turn them on. See
[Admin write tools](#admin-write-tools-opt-in) below.

| Tool | What it does | Read or write |
|---|---|---|
| `skool_whoami` | Confirms your Skool login works and lists the communities you belong to. Run this first when something breaks. | read |
| `skool_list_posts` | Lists recent posts in your community feed: title, snippet, comment and upvote counts, author. | read |
| `skool_get_post` | Reads one full post and its comments. | read |
| `skool_create_post` | Publishes a new post to your community feed. | write |
| `skool_comment_on_post` | Adds a comment to a post. | write |
| `skool_list_members` | Lists members of your community: id, name, email, short bio. | read |
| `skool_list_courses` | Lists the classroom courses in your community. | read |
| `skool_get_lesson` | Reads a course's modules and lessons, and the content of one lesson. | read |
| `skool_list_conversations` | Your DM inbox: unread counts and last-message previews. | read |
| `skool_get_conversation` | Reads the messages in one DM conversation. | read |
| `skool_send_message` | Sends a DM into an existing conversation. | write |
| `skool_mark_read` | Marks a DM conversation as read. | write |
| `skool_start_chat` | Opens a new DM with a member. | write |
| `skool_list_pending_members` | Lists join requests waiting for approval: name, answers to your join questions, risk score when Skool gives one. Admins only. | read |
| `skool_list_events` | Lists your community's calendar events. | read |

## Before you start

- macOS, Windows, or Linux. Linux was tested on a Grok Bot computer on Nov 19, 2026: the doctor
  passed 10 out of 10 checks (it has 11 now, the new one is informational), and both a
  `refresh_cookie.py --dry-run` and a real refresh worked.
- Claude Code or Claude Desktop.
- A Skool account that is actually a member of the community you want to connect.
- That's it. You do not need Python installed. The setup installs everything it needs.

## Install

**Step 1. Get the folder onto your computer.** Either way works.

Option A, if you have git:

```bash
cd ~
git clone https://github.com/Solnest-AI/skool-mcp-community.git skool-mcp
```

That drops it at `~/skool-mcp`, which is exactly where you want it.

Option B, the ZIP:

1. Download the ZIP (on the repo page: green **Code** button, then **Download ZIP**).
2. Unzip it.
3. Rename the folder to `skool-mcp` and move it so it sits directly in your home folder.

**Step 2. Put it in the right place.** This matters more than it sounds like it should.

- Mac: `~/skool-mcp` (that is `/Users/yourname/skool-mcp`)
- Windows: `C:\Users\yourname\skool-mcp`

Not Documents. Not Desktop. Not Downloads. macOS locks down those folders, and background tasks
that try to read them fail in a way that is genuinely annoying to debug. Home folder, done.

**Step 3. Point Claude at the folder and say "set this up".**

In Claude Code:

```bash
cd ~/skool-mcp
claude
```

Then type: `Set this up.`

Claude reads `SETUP.md` and runs the whole thing: installs the tools, opens a browser so you can
log into Skool yourself, verifies the connection, registers the server, and tests it. It stops and
asks you when it needs you.

Setting up for Claude Desktop instead? Same thing. Run the setup from Claude Code (or by hand),
and at the registration step tell Claude you want to use it in Claude Desktop.

## How this works and what to know

Read this part. It is short and it is the honest version.

**Skool has no official API.** There is no developer program, no API key, no supported way in. So
this does what your browser does: it uses your own logged-in session to talk to the same internal
endpoints the Skool website uses.

**Your login stays on your machine.** During setup, a real browser window opens and you log into
Skool yourself, exactly like normal (Google sign-in and 2FA both work). The script reads the
session cookie out of that browser and writes it to a local file called `.env`. Nothing is sent to
Solnest, to Anthropic, or anywhere else.

**Never paste your cookie into a chat.** Not to Claude, not to a friend, not to anyone online
offering to help. That cookie is your entire Skool account: your posts, your DMs, your billing, your
members. Anyone holding it is logged in as you. Setup is built so the cookie never has to appear
in a conversation, and Claude is instructed to refuse if you offer it.

**`.env` is your password file.** Treat it exactly like one. Don't open it in a screenshare, don't
screenshot it, don't paste it anywhere, don't drop it in Drive or Slack. The bundle is already set
up to keep it out of any code you upload, so you mostly just have to not go out of your way to
share it. If you ever think it leaked, log out of Skool everywhere and re-run setup.

**The tools act as you.** A post created by `skool_create_post` is your post. A DM is from you. It
looks identical to typing it yourself, because functionally it is. Have Claude read the draft back
to you before anything goes out.

**Only use it on communities you actually belong to.** These are your own credentials against your
own memberships. Don't get cute.

**Skool can break it.** No official API means no stability promise. If Skool changes their site,
tools can stop working overnight. When something breaks, run the doctor first:

```bash
cd ~/skool-mcp
uv run doctor.py
```

It tells you exactly which piece is broken and how to fix it. It never prints your cookie.

**Your session expires eventually.** The Skool login token lasts about a year, so this is rare.
Re-run `uv run connect_skool.py` when the tools start failing with a login error.

**A 403 is a different problem, and logging in again will not fix it.** Skool's API sits behind a
firewall (AWS WAF) that can refuse your computer before Skool ever sees the request. Your login is
completely fine when this happens. There are two versions of it, and they need opposite fixes:

- **Your connection's fingerprint.** The firewall inspects the technical signature of the
  connection itself, and plain Python does not look like a browser. The bundle handles this
  automatically now by borrowing Chrome's signature. Changing networks does nothing for it.
- **Your IP address.** The firewall refuses the address you are coming from, usually a VPN, a
  proxy, or an address you share with a lot of other people. Only changing your network fixes it.

You do not have to work out which one you have. Run:

```bash
cd ~/skool-mcp
uv run net_check.py
```

It needs no login, takes five seconds, and tells you which of the two it is and exactly what to do.
Do not start by reconnecting your Skool login, that cannot fix either one.

## Admin write tools (opt-in)

For people who own or admin a community. Off by default. With the switch off, Claude cannot even
see these tools.

| Tool | What it does |
|---|---|
| `skool_approve_member` | Approves a pending join request. |
| `skool_decline_member` | Declines a pending join request. Refuses anything that is not currently pending, because the same call on an active member removes them. |
| `skool_reply_to_comment` | Replies to a comment on a post. |
| `skool_edit_post` | Edits a post's or comment's title or text. Keeps attachments, labels and videos. |
| `skool_delete_post` | Deletes a post (and all its comments) or a comment (and its replies). |
| `skool_create_event` | Creates a calendar event. |
| `skool_update_event` | Changes a calendar event. |
| `skool_delete_event` | Deletes a calendar event. Recurring events are refused, do those in Skool. |
| `skool_invite_members` | Invites people by email. Unverified endpoint, see below. |

**Turning them on.** Set `SKOOL_WRITE_TOOLS=1` in the environment the server runs in, then restart
the app. The clean way is the server registration, so you never touch `.env`:

- Claude Code: remove and re-add the server with the variable:
  `claude mcp remove skool` then
  `claude mcp add skool -e SKOOL_WRITE_TOOLS=1 -- uv --directory "$HOME/skool-mcp" run server.py`
- Claude Desktop: add `"env": {"SKOOL_WRITE_TOOLS": "1"}` inside the `"skool"` entry of
  `claude_desktop_config.json`, next to `"command"` and `"args"`. Then fully quit and reopen.

To turn them off, take the variable out and restart. `uv run doctor.py` shows an "Admin write
tools" line. It can only see your shell and `.env`, not the Claude app's config, so "disabled"
there does not prove they are off in the app.

**Every write is two steps.** The first call never changes anything. It reads the target fresh,
then shows you exactly what would be sent (the member's name and answers, the post's current
text, the event before and after), any warnings, and a one-time confirm token. Only a second call
with that token, the same arguments, and within 10 minutes actually does it. Right before writing
it reads the target again, so a request someone already approved in the browser, or a post that
is already gone, gets caught instead of acted on blind. Tell Claude to show you the preview and
wait for your yes.

**A network error is never retried automatically.** If the connection drops while an admin write
is in flight, it may or may not have reached Skool. The tool says so and stops instead of
re-sending, because re-sending a reply or an invite that did land would do it twice. Check in
Skool, then preview again if it is still needed.

**Rate caps.** Counted from the audit log, so restarting does not reset them. Change any of them
with the environment variable (same place as `SKOOL_WRITE_TOOLS`):

| What | Default | Variable |
|---|---|---|
| Approvals + declines per hour | 30 | `SKOOL_CAP_MEMBERS_PER_HOUR` |
| Approvals + declines per day | 200 | `SKOOL_CAP_MEMBERS_PER_DAY` |
| Emails per invite call | 5 | `SKOOL_CAP_INVITES_PER_CALL` |
| Invite emails per day | 20 | `SKOOL_CAP_INVITES_PER_DAY` |
| Calendar creates, updates, deletes per hour | 10 | `SKOOL_CAP_CALENDAR_PER_HOUR` |
| Replies, post edits, deletes per hour | 20 | `SKOOL_CAP_POSTS_PER_HOUR` |
| Seconds between any two writes | 1 (can only go up) | `SKOOL_WRITE_MIN_INTERVAL` |

**Audit log.** Every write attempt, its result, and every refused confirm is appended as one JSON
line: time, tool, community, the ids it touched, and the outcome. Never your cookie, and never a
message body (free text is clipped to 200 characters). If the log cannot be written, the write is
refused.

- Mac: `~/Library/Logs/skool-mcp-audit.jsonl`
- Windows: `%LOCALAPPDATA%\skool-mcp\audit.jsonl`
- Linux: `~/.local/state/skool-mcp/audit.jsonl`

**The honest risk.** Skool has no official API and does not bless automation. Accounts that act
like bots can get limited, and admin actions are the ones a platform watches. Keep a human
approving every write, keep it slow, and do not loop these tools over your whole member list. The
caps are there to stop a runaway agent, not to tell you how much is safe.

**What has not been tested live.** These tools were built and tested offline against recorded
request shapes from public sources. Before you rely on them, do one approve, one post edit, and one
event create and delete on a community where a mistake does not matter, and check the result in
Skool. The invite endpoint has a single public source and is the least proven of the lot.
`endpoints.md` lists exactly what is and is not verified.

## Running it on Linux

Setup is the same as macOS. Two Linux notes:

- If `uv run playwright install --with-deps chromium` fails because apt fails, skip `--with-deps`.
  Plain `uv run playwright install chromium` works.
- There is no installer for the auto-refresh job on Linux. Schedule this yourself to run about
  every 2 days, with full paths (cron and systemd do not read your shell profile):
  `cd /full/path/to/skool-mcp && /full/path/to/uv run refresh_cookie.py`.
  A cron line, a systemd user timer, or a Grok Bot scheduled routine all work. `SETUP.md` Phase 6
  has copy-paste versions. `.env` stays mode 0600, and the refresh keeps it that way.

## Getting help

Run `uv run doctor.py` (or `uv run net_check.py` for anything that looks like a 403 or "blocked"),
then paste the output into Claude and ask it to fix what failed. The output is safe to share: it
prints check results, cookie lengths, and expiry dates, never the cookie itself.

Still stuck? Start a fresh Claude chat, paste the doctor output, and say what you were trying to do.
Never paste your `.env`.

## License

MIT. See [LICENSE](LICENSE).
