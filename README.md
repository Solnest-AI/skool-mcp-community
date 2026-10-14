# Skool MCP

Give Claude access to your Skool community. Posts, comments, members, classroom, and your DM inbox.

Ask Claude things like "what did people post this week?", "draft a reply to the last 5 DMs",
or "pull my member list into a spreadsheet". It runs on your computer, using your own Skool login.

Unofficial. Not built by Skool, not affiliated with Skool.

## What you can do with it

13 tools. The read ones are safe to fire off. The write ones post publicly under your name, so
Claude should confirm the wording with you first.

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

## Before you start

- macOS or Windows. (Linux is untested.)
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

## Getting help

Run `uv run doctor.py` (or `uv run net_check.py` for anything that looks like a 403 or "blocked"),
then paste the output into Claude and ask it to fix what failed. The output is safe to share: it
prints check results, cookie lengths, and expiry dates, never the cookie itself.

Still stuck? Start a fresh Claude chat, paste the doctor output, and say what you were trying to do.
Never paste your `.env`.

## License

MIT. See [LICENSE](LICENSE).
