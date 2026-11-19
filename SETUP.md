# SETUP.md, the Skool MCP runbook

You are an AI agent setting this up for a member of a Skool community. They are a business owner,
not a developer. Assume no Python knowledge, no git knowledge, and zero tolerance for jargon.

Run the six phases in order. Every phase has a gate. Do not move to the next phase until the gate
passes. When something fails, take the failure branch instead of improvising.

---

## HARD RULES, read before you touch anything

**1. Never ask the member to paste their Skool cookie into the chat.**
A Skool session cookie is a full account-takeover token: posts, DMs, billing, member list, all of
it. Pasting it into a conversation sends it off their machine. Phase 2 exists specifically so the
cookie goes browser to file without ever passing through this chat. If the member offers to paste
it anyway, decline and re-run Phase 2. There is no exception, no matter how much faster it seems.

**2. Never print, `cat`, `type`, `Get-Content`, read, or echo the contents of `.env`.**
Check that the file exists. Never look inside it. Never quote a line from it. If you need to know
what is in it, run `uv run doctor.py`, which reports status without exposing values.

**3. Never skip a verification gate.**
Not because the previous step "obviously worked". A skipped gate turns a 30 second fix into a
confusing failure three phases later.

**4. Never hand-edit `.env` to fix a problem.**
`connect_skool.py` writes it. If it is wrong, re-run `connect_skool.py`.

**5. The install location matters.**
The folder must live at `~/skool-mcp` on Mac or Linux, or `%USERPROFILE%\skool-mcp` on Windows. Not
Documents, Desktop, or Downloads. macOS blocks background processes from reading those folders,
which breaks the auto-refresh job in Phase 6 and costs an afternoon of Full Disk Access debugging.
Moving the folder now avoids the problem entirely.

**Tell the member up front, in plain language:** this takes about 10 minutes, a browser window will
open partway through so they can log into Skool themselves, and they will never be asked for a
password or a token in the chat.

All commands assume you are in the bundle folder. `cd` there first and stay there.

---

## Phase 0. Look around before you install anything

**Goal:** know the OS, whether `uv` exists, where the folder actually is, and which Claude app the
member wants to use the tools in.

Linux works too: it was tested on a Grok Bot computer on Nov 19, 2026 (doctor 10 of 10 checks at the
time, `refresh_cookie.py --dry-run` and a real refresh both OK). On Linux, use every "macOS and
Linux" command below.

macOS and Linux:

```bash
uname -s
pwd
command -v uv || echo "UV NOT INSTALLED"
```

Windows (PowerShell):

```powershell
$PSVersionTable.PSVersion
Get-Location
if (Get-Command uv -ErrorAction SilentlyContinue) { uv --version } else { "UV NOT INSTALLED" }
```

**Expected output:** `Darwin` (Mac), `Linux`, or a PowerShell version, an absolute folder path, and either a uv
path/version or `UV NOT INSTALLED`. Both are fine at this stage.

**GATE:** you can state the OS, the absolute folder path, and whether uv is installed.

**Then check the folder location.** If the path contains `Documents`, `Desktop`, `Downloads`, or
`OneDrive`, stop and fix it before going further:

Check the destination is free first:

```bash
[ -e ~/skool-mcp ] && echo "DESTINATION ALREADY EXISTS, rename or remove it first"
```

```powershell
if (Test-Path "$env:USERPROFILE\skool-mcp") { "DESTINATION ALREADY EXISTS, rename or remove it first" }
```

If it already exists and is an empty leftover, delete it. If it is a real copy of the bundle, use
that one instead and `cd` into it.

Then step out of the folder before moving it. A shell sitting inside a folder will block the move
on Windows.

macOS and Linux:

```bash
SRC="$(pwd)"; cd ~ && mv "$SRC" ~/skool-mcp && cd ~/skool-mcp && pwd
```

Windows:

```powershell
$src = (Get-Location).Path
Set-Location $env:USERPROFILE
Move-Item -Path $src -Destination "$env:USERPROFILE\skool-mcp"
Set-Location "$env:USERPROFILE\skool-mcp"
Get-Location
```

Tell the member why in one sentence: macOS protects those folders and background jobs cannot read
them. Do not argue the point, just move it.

**Then ask the member one question:** "Do you want to use the Skool tools in Claude Code, in Claude
Desktop, or both?" Write the answer down. Phase 4 branches on it. You are almost certainly running
inside Claude Code right now, since these commands need a terminal, but that is separate from where
they want to *use* the tools.

**If it fails:**
- No terminal access at all (you are Claude Desktop with no shell): tell the member you cannot run
  the setup from here. Two options: install Claude Code and run `Set this up.` from the folder, or
  run each command yourself and paste the output back. Do not fake progress.
- `pwd` shows a folder with no `server.py` in it: you are in the wrong place. `ls` (or `dir`),
  find the folder containing `server.py` and `SETUP.md`, and `cd` into it.

---

## Phase 1. Install the runtime and the dependencies

**Goal:** `uv` installed, project dependencies synced, Playwright's Chromium downloaded.

One line for the member first: uv is a small tool that installs the right Python and the libraries
automatically, so they never have to think about Python versions.

**1a. Install uv** (skip if Phase 0 found it).

macOS and Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
uv --version
```

**1b. Install the project dependencies and the browser.**

Both OSes, same commands:

```bash
uv sync
uv run playwright install chromium
```

The Chromium download is roughly 150 MB and can take a couple of minutes. Say so, so the member
does not think it hung.

Linux: if you reach for `uv run playwright install --with-deps chromium` and it fails because apt
fails (no sudo, locked package manager), skip `--with-deps`. Plain `uv run playwright install
chromium` is what worked on the tested Linux machine.

**Expected output:** uv prints a version. `uv sync` prints a list of installed packages and
finishes without an error. `playwright install chromium` ends with a downloaded/installed message.

**GATE:**

```bash
uv run python -c "import mcp, httpx, dotenv, playwright, curl_cffi; print('DEPS OK')"
```

Must print exactly `DEPS OK`. Anything else means Phase 1 is not done.

**If it fails:**
- `uv: command not found` right after installing it: the PATH in this shell is stale. Use the full
  path, `$HOME/.local/bin/uv` on Mac or `$env:USERPROFILE\.local\bin\uv.exe` on Windows, for every
  command from here on, or open a fresh terminal and `cd` back to the folder.
- Windows says running scripts is disabled: use the exact `-ExecutionPolicy ByPass` command above.
  Do not tell the member to change their machine-wide execution policy.
- `uv sync` says there is no `pyproject.toml`: you are in the wrong folder. Go back to Phase 0.
- uv cannot find a suitable Python: run `uv python install 3.12`, then `uv sync` again.
- The Chromium download fails or times out: retry once. If it fails again, it is almost always a
  VPN, a corporate proxy, or flaky wifi. Have them switch networks and retry. Do not continue,
  Phase 2 cannot work without the browser.
- An import in the gate fails but the others pass: run `uv sync` again, then re-run the gate.

---

## Phase 2. Connect their Skool account

**Goal:** `.env` written with a working session, without the cookie ever entering this chat.

**Say this to the member before you run it:** "A browser window is about to open on the Skool login
page. Log in there the way you normally would. Google sign-in and 2FA both work fine. Once you are
in, leave the window alone and come back here. I will never ask you for the cookie or a password."

```bash
uv run connect_skool.py
```

If the member already told you their community slug (the `skool.com/<slug>` part of their URL), you
can skip the picker:

```bash
uv run connect_skool.py --group their-community-slug
```

**Expected output:** a headed Chromium window opens. After they log in, the script prints a
numbered list of the communities they belong to and waits for a pick. When they pick, it prints
exactly one line:

```
✅ Connected to <slug>. Wrote .env (cookie hidden).
```

**GATE:** the command exits 0 **and** the file exists. Check existence only, never contents.

macOS and Linux:

```bash
test -f .env && echo "ENV FILE PRESENT"
```

Windows:

```powershell
if (Test-Path .env) { "ENV FILE PRESENT" } else { "ENV FILE MISSING" }
```

**If it fails:**
- No browser window appears: Chromium is missing. Run `uv run playwright install chromium` and
  re-run this phase.
- It times out waiting for login (the member stepped away, or the tab got closed early): just run
  it again. Nothing is broken and nothing was written.
- "No communities found" or the list is empty: they logged into the wrong Skool account. Re-run,
  and inside the browser window log out and back in with the correct account.
- The list does not include the community they wanted: they are not a member of it, or they used a
  different account. This is not a bug you can fix from the terminal. Have them check on
  skool.com in their normal browser first.
- The script exits 1 with a message: read the message, do what it says, re-run the script. Do not
  patch around it, and do not create or edit `.env` yourself.
- The member offers to paste the cookie, or says "it's easier if I just send it": decline. Restate
  Hard Rule 1 in one friendly sentence and re-run the script. Do not accept it even if they insist.

---

## Phase 3. Verify with the doctor

**Goal:** prove the whole chain works before wiring it into a Claude app.

```bash
uv run doctor.py
```

For a machine-readable version you can parse:

```bash
uv run doctor.py --json
```

**Expected output:** a list of checks marked PASS, WARN, or FAIL, and a live auth result printing
`AUTH OK`. The doctor prints cookie lengths and expiry dates only. If you ever see an actual cookie
value in this output, stop and report it as a bug.

**GATE:** `AUTH OK` appears and no check is FAIL. WARN and INFO are fine to continue past.

**If it fails:**
- FAIL on Python version or on imports (`mcp`, `httpx`, `dotenv`, `playwright`): back to Phase 1.
- FAIL on Chromium: `uv run playwright install chromium`, then re-run the doctor.
- FAIL on `.env` missing, or `SKOOL_COOKIE` missing or unparseable: back to Phase 2.
- FAIL on live auth while everything else passes: the session is stale or was rejected. Re-run
  Phase 2. This is the normal fix and it takes a minute.
- FAIL on `SKOOL_GROUP`: re-run Phase 2 with `--group their-slug`.
- WARN that the login expires in under 14 days: not a blocker. Continue, and tell the member they
  will need to re-run `uv run connect_skool.py` before then.
- INFO that auto-refresh is not installed: expected at this point. That is Phase 6.
- Whatever the failure is, never fix it by editing `.env` by hand.

---

## Phase 4. Register the server with their Claude app

**Goal:** the app can see and start the `skool` server.

Use the answer from Phase 0. Branch A for Claude Code, Branch B for Claude Desktop. If they want
both, do both.

First, get the absolute folder path and keep it handy. Relative paths and `~` do not work here.

```bash
pwd
```

```powershell
(Get-Location).Path
```

### Branch A. Claude Code

macOS and Linux:

```bash
claude mcp add skool -- uv --directory "$HOME/skool-mcp" run server.py
```

Windows:

```powershell
claude mcp add skool -- uv --directory "$env:USERPROFILE\skool-mcp" run server.py
```

If the folder is not in the default location, substitute the real absolute path from `pwd`.

**Expected output:** a confirmation that the server `skool` was added.

**GATE:**

```bash
claude mcp list
```

`skool` must appear in the list. Some versions of the CLI also health-check each server and print a
connected or failed status. If yours does and `skool` shows as failed, take the failure branch
below. If it only lists the name, that is a pass. The real end-to-end proof is Phase 5.

Then have the member exit and relaunch Claude Code so the tools load into a fresh session.

**If it fails:**
- `claude: command not found`: the Claude Code CLI is not installed on this machine. Either install
  it, or use Branch B instead.
- "already exists": `claude mcp remove skool`, then add it again.
- Listed but failing to connect: start the server by hand for a few seconds:
  `uv run server.py`. If it prints an error, fix that error. If it just sits there silently, that
  is correct, MCP servers wait for input over stdin. Press Ctrl+C and re-check the registration
  path, it is almost always a wrong or relative directory.

### Branch B. Claude Desktop

Claude Desktop does not inherit your terminal's PATH, so the config needs the **absolute path to
uv** and the **absolute path to the folder**.

Get the uv path:

```bash
command -v uv
```

```powershell
(Get-Command uv).Source
```

The config file lives here:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
  (usually `C:\Users\yourname\AppData\Roaming\Claude\claude_desktop_config.json`)

**If the file does not exist,** create it with exactly this, substituting the two real paths.

macOS:

```json
{
  "mcpServers": {
    "skool": {
      "command": "/Users/YOURNAME/.local/bin/uv",
      "args": ["--directory", "/Users/YOURNAME/skool-mcp", "run", "server.py"]
    }
  }
}
```

Windows (backslashes must be doubled inside JSON):

```json
{
  "mcpServers": {
    "skool": {
      "command": "C:\\Users\\YOURNAME\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\Users\\YOURNAME\\skool-mcp", "run", "server.py"]
    }
  }
}
```

**If the file already exists,** add the `"skool"` entry inside the existing `"mcpServers"` object
and change nothing else. Do not overwrite the file and do not print its contents into the chat.
Other servers in there may hold API keys that have nothing to do with Skool.

Then have the member fully quit and reopen Claude Desktop. Closing the window is not enough:

- Mac: Cmd+Q, then reopen.
- Windows: right-click the Claude icon in the system tray, Quit, then reopen.

**GATE:** after the restart, the member can see the `skool_*` tools listed in Claude Desktop's tool
menu. Ask them to confirm out loud. You cannot verify this one yourself.

**If it fails:**
- Tools do not appear: validate the JSON without printing it. Works on both OSes, substitute the
  config path for that OS.

  macOS:

  ```bash
  uv run python -c "import json,sys; json.load(open(sys.argv[1])); print('JSON VALID')" "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
  ```

  Windows:

  ```powershell
  uv run python -c "import json,sys; json.load(open(sys.argv[1])); print('JSON VALID')" "$env:APPDATA\Claude\claude_desktop_config.json"
  ```

  A trailing comma or a missing brace is the usual culprit. This prints only `JSON VALID` or an
  error, never the file's contents.
- Still nothing: read the tail of the Claude Desktop MCP log.
  - macOS: `~/Library/Logs/Claude/mcp*.log`
  - Windows: `%APPDATA%\Claude\logs\mcp*.log`
- `spawn uv ENOENT` in the log: the `"command"` value is wrong. It must be the full absolute path
  to the uv executable, not just `uv`.
- Paths with `~` or `%USERPROFILE%` in the JSON: those are not expanded. Replace them with real
  absolute paths.

---

## Phase 5. Prove it works end to end

**Goal:** a real tool call returns their real community.

Ask the member to start a fresh chat in whichever app they registered and send:

> Who am I on Skool?

That triggers `skool_whoami`.

**Expected output:** the active group is their community slug, plus the list of communities they
belong to.

**GATE:** the returned slug matches the community they picked back in Phase 2. This happens in a
different session, so you cannot see it yourself. Ask the member to confirm what came back, and
wait for their answer before calling setup done.

**If it fails:**
- "No such tool" or Claude does not offer the tool: the app was not restarted, or Phase 4 did not
  take. Restart the app, then re-check Phase 4's gate.
- An auth error: the session went stale between phases. Re-run Phase 2, then Phase 3.
- It returns the wrong community: `uv run connect_skool.py --group correct-slug`, then re-run
  Phase 3 and try again.

**Then hand it over.** Give them three prompts they can use right now:

- "Summarize the last 10 posts in my Skool community."
- "Show me my unread Skool DMs and draft replies. Do not send anything yet."
- "List my Skool members and give me a count."

And one warning, said plainly: the write tools post publicly and DM as them. Ask Claude to show the
draft before it sends anything.

---

## Phase 6. Optional, keep it alive automatically

**Goal:** a background job refreshes the short-lived security token so the tools do not die every
few days.

Explain the tradeoff in one sentence: their Skool login lasts about a year, but the AWS firewall
token bundled with it expires in about 4 days, and this job re-mints it quietly in the background.
Skipping it is fine, it just means re-running `uv run connect_skool.py` when things stop working.

macOS:

```bash
bash install_refresh_mac.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_refresh_windows.ps1
```

Linux: there is no installer, you schedule it. Use full paths everywhere (cron and systemd do not
read the shell profile, so a bare `uv` is not found). Get them first:

```bash
pwd; command -v uv
```

Prove one refresh works before scheduling it (writes nothing):

```bash
uv run refresh_cookie.py --dry-run
```

Then pick one of these, substituting the two real paths. Run it about every 2 days.

- **cron** (`crontab -e`, add one line; 04:00 on every other day of the month):

  ```
  0 4 */2 * * cd /home/YOURNAME/skool-mcp && /home/YOURNAME/.local/bin/uv run refresh_cookie.py
  ```

- **systemd user timer.** Create `~/.config/systemd/user/skool-refresh.service`:

  ```ini
  [Unit]
  Description=Refresh the Skool MCP cookie

  [Service]
  Type=oneshot
  WorkingDirectory=/home/YOURNAME/skool-mcp
  ExecStart=/home/YOURNAME/.local/bin/uv run refresh_cookie.py
  ```

  and `~/.config/systemd/user/skool-refresh.timer`:

  ```ini
  [Unit]
  Description=Refresh the Skool MCP cookie every 2 days

  [Timer]
  OnCalendar=*-*-01/2 04:00:00
  Persistent=true

  [Install]
  WantedBy=timers.target
  ```

  then `systemctl --user daemon-reload && systemctl --user enable --now skool-refresh.timer`. On a
  machine nobody stays logged into, also run `loginctl enable-linger "$USER"` or the timer only
  runs while someone is logged in.

- **A Grok Bot (or other agent) scheduled routine:** have it run
  `cd /home/YOURNAME/skool-mcp && /home/YOURNAME/.local/bin/uv run refresh_cookie.py` every 2 days.

`.env` stays mode 0600: the refresh writes it that way. The refresh log is
`~/.local/state/skool-mcp/refresh.log`. These schedules are templates: the refresh itself was tested
on Linux, the cron and systemd wiring above was not.

**Expected output:** the installer confirms the job is registered and prints the uninstall command.
On Mac the job is `com.skoolmcp.cookie-refresh`. On Windows it is `SkoolMCP-CookieRefresh`. Give the
member the uninstall command the installer printed, so they can remove it later without you.

**GATE:**

macOS:

```bash
launchctl print "gui/$(id -u)/com.skoolmcp.cookie-refresh" | head -n 5
```

Windows:

```powershell
Get-ScheduledTask -TaskName "SkoolMCP-CookieRefresh" | Select-Object TaskName, State
```

The job must be listed. If the installer printed a different name, use that one instead.

Linux: `crontab -l | grep refresh_cookie` (cron) or `systemctl --user list-timers skool-refresh.timer`
(systemd) must show it. For a bot routine, ask the member to confirm it is saved.

**If it fails:**
- The Mac installer prints Full Disk Access instructions: that only happens when the folder sits
  under a protected folder. The clean fix is to move it to `~/skool-mcp` and redo Phase 4 (the
  registered paths changed). Only follow the Full Disk Access steps if they refuse to move it.
- Windows "Access is denied": run PowerShell as the member's normal user, not as Administrator. A
  task registered as Administrator will not run in their session.
- You are not sure whether it was installed already: just run the installer again. Both installers
  clear and replace an existing job, so reinstalling is safe and never errors on a duplicate.
- Anything else: this phase is optional. Do not let it block a working setup. Tell the member to
  re-run `uv run connect_skool.py` whenever the tools start failing, and move on.

---

## Phase 7. Optional, admin write tools. Only if the member asks

**Goal:** a community owner or admin can approve and decline join requests, edit and delete posts,
manage calendar events, and invite by email from Claude.

Do not offer this to someone who is just a member of a community. Do not turn it on by default.

**Say this to the member, plainly, before enabling anything:**

- Every admin write is two steps. The first call only shows a preview. Nothing happens until they
  see it and say yes.
- Skool has no official API. Automated admin actions can get an account limited. Keep a human
  approving every write, keep it slow.
- These tools were tested offline only. They should do one approve, one post edit, and one event
  create and delete on a community where a mistake does not matter before relying on them. The
  invite tool is the least proven (a single public source).

**Enable it in the server registration, never by editing `.env`.**

Claude Code (macOS and Linux):

```bash
claude mcp remove skool
claude mcp add skool -e SKOOL_WRITE_TOOLS=1 -- uv --directory "$HOME/skool-mcp" run server.py
```

Windows:

```powershell
claude mcp remove skool
claude mcp add skool -e SKOOL_WRITE_TOOLS=1 -- uv --directory "$env:USERPROFILE\skool-mcp" run server.py
```

Claude Desktop: in the existing `"skool"` entry of `claude_desktop_config.json`, add
`"env": {"SKOOL_WRITE_TOOLS": "1"}` next to `"command"` and `"args"`. Change nothing else. Validate
the JSON with the Phase 4 command. Then fully quit and reopen the app.

**GATE:** in a fresh session the member sees `skool_approve_member` and the other admin tools.
Then have them ask: "Show me my pending Skool join requests." That is a read. If they want to test
a write, ask for a preview only ("preview approving the first one, do not confirm") and check the
preview names the right person.

**If it fails:**
- The admin tools do not appear: the variable did not reach the server. Re-check the registration
  and restart the app. `uv run doctor.py` cannot see app-level env, so its "Admin write tools: disabled"
  line is not proof either way.
- A write is refused with "Rate cap reached": working as designed. Wait, or raise that cap's
  variable (README, "Admin write tools").
- A write is refused because the audit log is not writable: fix permissions on the log path the
  error names. The tool will not write without an audit trail.
- "not in the current pending list" on a decline: the request was already handled, or a user id
  was passed instead of the membership id from `skool_list_pending_members`. This refusal is the
  safety rail that stops a decline from removing an active member. Never work around it.

To turn it off: re-register without `-e SKOOL_WRITE_TOOLS=1` (or remove the `env` line) and restart.

---

## Done. Confirm all of it

Before you tell them it is finished, confirm every one of these:

1. The folder is at `~/skool-mcp` (Mac, Linux) or `%USERPROFILE%\skool-mcp` (Windows).
2. `uv run python -c "import mcp, httpx, dotenv, playwright, curl_cffi; print('DEPS OK')"` prints `DEPS OK`.
3. `.env` exists. (You never looked inside it.)
4. `uv run doctor.py` prints `AUTH OK` with no FAIL.
5. The server is registered and the app was restarted.
6. `skool_whoami` returned their real community.
7. Auto-refresh is either installed or consciously skipped.
8. Admin write tools are off, unless the member asked for them and heard the risks (Phase 7).

Then tell them, in one short paragraph: what they can ask for, that writes are public and go out
under their name, and that `uv run doctor.py` is the answer to "it stopped working".

## When it breaks later

Symptom to action, in order:

**Any 403, or any "blocked" message: run `uv run net_check.py` FIRST.** It needs no login and
answers in five seconds. `api2.skool.com` sits behind CloudFront, which produces two failures that
look identical from the outside and have opposite fixes:

- **`401`, short text.** Skool rejected the cookie. Reconnecting fixes it (Phase 2).
- **`403`, CloudFront HTML.** The firewall refused *this computer's connection*. Skool never saw
  the request, so the login is irrelevant and **reconnecting can never fix it.** There are two
  distinct causes with opposite fixes, and `net_check.py` reports which one it is:
  - **TLS fingerprint.** AWS WAF inspects the TLS handshake on some source IPs, and plain Python
    does not handshake like Chrome. The bundle now sends every request over a Chrome fingerprint
    (`curl_cffi`), so `uv sync` is the fix. **Changing networks does nothing for this one.** Do not
    tell the member to disconnect a VPN when `net_check.py` reports TLS_BLOCKED.
  - **Egress IP.** The address itself is refused (VPN, proxy, hosting provider, or Amazon's IP
    reputation list). Only a different network fixes it.

Do not skip that check. Reading a CloudFront 403 as an expired session is the single most expensive
wrong turn with this bundle. It sends the member through login loops that cannot possibly work.

| What they see | What to do |
|---|---|
| Any 403, or "blocked" / "could not be satisfied" | `uv run net_check.py` first. If BLOCKED: VPN off, switch network, unset proxy vars. Do NOT reconnect |
| Tools error with an auth or permission message | `uv run doctor.py`, then Phase 2 if live auth fails |
| Tools vanished from the app | Phase 4, and restart the app |
| Everything worked yesterday, nothing today | `uv run doctor.py`. If live auth fails, Phase 2; if it reports a CDN block, `net_check.py` |
| Doctor warns the login expires soon | Phase 2 before it does |
| One specific tool returns odd or empty data | Skool may have changed their site. Note which tool, check for a bundle update |

A missing `aws-waf-token` is **not** a fault and never needs fixing. Measured against live api2 on
2026-08-13, `auth_token` alone authenticates fine. Doctor reports the token as INFO only. Do not
chase it.

Never solve any of these by asking for a pasted cookie or by editing `.env` by hand.
