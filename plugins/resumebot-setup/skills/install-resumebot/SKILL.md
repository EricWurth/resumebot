---
name: install-resumebot
description: Install the resumebot app on this computer, finish an install that stopped half way, or update it to the newest release, and connect it to this Claude Code. Use when the user says "set up resumebot", "install resumebot", "update resumebot", "connect resumebot", or when the session-start status says resumebot is not installed, not set up or not running.
---

# Set up resumebot (install it, finish it, or update it, and connect it)

One script does the whole install: `install.py`, kept in the app's repository at the newest release tag. It looks before
every step and skips what already works, so the same run does a fresh install, finishes one that stopped half way and
updates an old one. It backs up the data before it replaces the code, never deletes the data folder, registers the two
doors with this Claude Code, puts **resumebot** on the desktop and ends with the app's own check. **You run every
command yourself and never give the user a command to type or paste.** Say each step in one sentence before you do it,
and never print a token or a password. Never stop at "already installed": run the script; it says what is already done.

## What the user does

Only these things; tell them so up front, and get one yes to the plan (install missing programs, back up the data, put
the newest release in place, start the app, register it with this Claude Code; their data stays):

1. **Answer one question, only when the install has no settings yet:** their name (the roles are kept under it). An update
   asks nothing: it keeps the name.
2. **Approve the sign-in, only if the agents are not signed in yet:** approve once in the browser tab that opens.
3. **Restart Claude Code** at the end.

To start the app later, double-click **resumebot** on the desktop.

| What | Where (Windows; macOS in brackets) |
|---|---|
| The app's code | `%LOCALAPPDATA%\Programs\resumebot` [`~/.local/share/resumebot-app`] |
| The user's data | the user variable `RESUMEBOT_DATA_DIR` if set, else `%LOCALAPPDATA%\resumebot` [`~/.local/share/resumebot`] |
| Where the script is fetched to | `%LOCALAPPDATA%\resumebot_fetch` [`~/.local/share/resumebot_fetch`] |

## How the commands are written

Every command runs as written in Windows PowerShell 5, in Git Bash and in macOS shells: no `&&`, no pipes. A quoted
program is called with `& ` before it in PowerShell (`& "$PY" ...`), without it in sh (`"$PY" ...`). Each command block is
its own shell: set the variables again at the top of each.

PowerShell:

```powershell
$BOOT = "$env:LOCALAPPDATA\resumebot_fetch"
$PY = uv python find --managed-python 3.12
```

Git Bash on Windows:

```sh
BOOT="$LOCALAPPDATA/resumebot_fetch"
PY=$(uv python find --managed-python 3.12)
```

sh on macOS:

```sh
BOOT="$HOME/.local/share/resumebot_fetch"
PY=$(uv python find --managed-python 3.12)
```

## 1. Prerequisites

Check each; install only what is missing, and say what you install. Do not pipe a downloaded script into a shell. The
script checks them again and names any it cannot find.

| Need | Check | Install |
|---|---|---|
| Claude Code | `claude --version` | nothing |
| winget (Windows) | `winget --version` | if missing, say "Install 'App Installer' from the Microsoft Store, then I will carry on." and wait |
| Homebrew (macOS) | `brew --version` | if missing, say "Install Homebrew from brew.sh, then I will carry on." and wait |
| git | `git --version` | `winget install --id Git.Git -e` [`brew install git`] |
| uv | `uv --version` | `winget install --id astral-sh.uv -e` [`brew install uv`] |
| Python 3.12, uv's own | `uv python find --managed-python 3.12` | `uv python install 3.12 --upgrade --no-bin` (run it every time: the newest build carries the newest SQLite) |
| Node 22 or newer | `node --version` | `winget install OpenJS.NodeJS.LTS` [`brew install node`] |

Never use a Python from python.org, the Store or the system for the app: its SQLite may carry a known bug. On Windows,
winget may raise a UAC prompt for Git and Node: tell the user to approve it. A program just installed may not be on this
shell's PATH yet; call it by its full path (the script rebuilds its own PATH). ARM64 Windows is untested.

## 2. The name

If the data folder has no `.env` (a fresh install), ask the user for their name now: one question. Pass it as `--owner`.
On an update the script ignores it.

## 3. Fetch the script

Find the repository address, in this order, and call it `$REPO`:

1. **The folder Claude was started in,** if it is a git clone whose origin serves resumebot (it has `AGENTS.md` and
   `plugins/resumebot-setup`): the output of `git remote get-url origin` there. If `git ls-remote --tags --refs "<that
   address>" 'app-v*'` lists no tag, go on to 2.
2. **The marketplace this plugin came from:** `claude plugin marketplace list`, the source of the one holding
   `resumebot-setup` (`owner/repo` means `https://github.com/owner/repo`); else `~/.claude/plugins/known_marketplaces.json`.
3. Only if both fail, ask the user.

Print the newest tag (the highest version number, not the latest by date; it exits with an error when there is none):

```
& "$PY" -c "import re,subprocess,sys; out=subprocess.run(['git','ls-remote','--tags','--refs',sys.argv[1],'app-v*'],capture_output=True,text=True).stdout; tags=re.findall(r'refs/tags/(app-v[0-9.]+)\s',out); print(max(tags,key=lambda t:[int(n) for n in t[5:].split('.')]))" "$REPO"
```

If the download fails, or no `app-v*` tag exists, say "Git could not download resumebot. Check the repository address and
your network, or ask whoever gave you the address, then tell me and I will try again." and stop. Ask for no password or
token. Otherwise put that tag's copy of the script in a folder of its own, outside the code folder (the script
replaces the code folder). `<TAG>` is the tag printed above; the first command clears an earlier copy:

```
& "$PY" -c "import os,sys,shutil,stat; s=sys.argv[1]; os.path.isdir(s) and shutil.rmtree(s,onexc=lambda f,p,e:(os.chmod(p,stat.S_IWRITE),f(p)))" "$BOOT/src"
git clone --depth 1 --branch <TAG> "$REPO" "$BOOT/src"
```

The script is `$BOOT/src/app/scripts/local/install.py`. If a newer release tag exists by the time it runs, it hands over to
that copy itself.

## 4. Run it

Run it in the background with the longest timeout the tool allows (in Claude Code, `run_in_background`): an install takes
several minutes, a first one longer.

```
uv run --python 3.12 --no-project "$BOOT/src/app/scripts/local/install.py" --owner "<name>"
```

Leave `--owner` off on an update. `--help` lists the other options (`--data`, `--code`, `--timezone`, `--repo`); pass none
unless the user asks. The script prints one line per step, `[step] what happened`; the same lines with more detail go to
`install/install.log` in the data folder. It never prints a token; never ask for one, never read
`secrets/claude-oauth-token`, never let the user paste a token into this chat.

While it runs, read `install/install.json` in the data folder now and then. It has `state` (`running`,
`waiting-for-user`, `done` or `failed`) and one entry per step.

- **`waiting-for-user`:** its `action` is "approve the sign-in in the browser tab that opened". Tell the user that one
  sentence, then keep waiting: the script waits up to ten minutes. Only if the user says no tab opened and gives you a
  code from the page, run `<code folder>/app/backend/.venv/Scripts/resumebot.exe signin --code <code>` (macOS
  `.venv/bin/resumebot`).
- **`done`:** go on to step 5.
- **`failed`, or the script exits non-zero:** its last lines are one sentence of the form `<step>: <what failed>: <what to
  do>`. Say it in plain words, do what it says, and run the script again: every step looks first, so a second run picks
  up where the first stopped. Exit 3 is a missing program (install it, step 1), 4 is a data backup that did not work
  (nothing was changed), 10 is another install already running (wait for it). Do not work around a failure by running
  the app's own commands one by one.

## 5. Finish

Tell the user to **restart Claude Code**, strictly after the script has printed that resumebot is installed: a restart
while the sign-in still waits ends the script. In the desktop app, say that the whole app, not only the conversation,
must be closed and opened again, because the doors load when it starts. (When the `getting-started` agent runs this
skill, do not say so: the agent says it after it has written the helper agents into the project.)

After the restart, run once `<code folder>/app/backend/.venv/Scripts/resumebot.exe doctor` (macOS
`~/.local/share/resumebot-app/app/backend/.venv/bin/resumebot doctor`). It prints one line per piece, `ok` or `MISSING`
with what to do; say what any MISSING line asks. The script ran the same check before, so this one mostly proves the
restarted Claude Code sees the doors.

Then say one line: what the script did ("updated to <version>; your data was backed up and kept") and what is left. When
the `getting-started` agent runs this skill, return to it after `done`: it writes the helper agents, asks for the restart
and does the hand-over. Otherwise hand over to `resumebot-setup:onboarding` and say in one sentence what its first step
is.

Backups (in `backups/` in the data folder) and an `-old-` code folder the script set aside can be deleted once the user is
happy. To go back after an update, stop the app, and copy the newest backup's contents over the data folder (ask the user
first). The data folder holds the user's work: delete it only if they ask.
