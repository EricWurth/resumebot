# Install

resumebot installs on Windows 10 and 11, and on macOS (community-tested), for one user, with no administrator rights. Your own Claude Code does the
install by following a skill; this page says what it does, where everything goes, and how to check it.

## What you need

- Claude Code, installed and signed in, on a paid Claude subscription.
- A Windows or macOS account of your own. The install touches only your user's folders and settings.
- `git`, `uv` (with its own Python 3.12), Node 22 or newer, and Chrome. The install checks each and installs the missing
  ones with `winget` (git, uv and Node). If `winget` is missing, Claude tells you to install "App Installer" from the
  Microsoft Store and carries on when you have. On a Mac it uses Homebrew (`brew install uv node git`); install
  Homebrew yourself first if it is missing. Chrome is needed only for the optional browser jobs
  ([`browser-reader.md`](browser-reader.md)).

The app uses uv's own Python and no other. A Python from python.org, the Store or the system can carry an older SQLite
with a known bug that can damage a database, and the install refuses to go on with one.

## The two routes

**The plugin.** In Claude Code:

```
/plugin marketplace add EricWurth/resumebot
/plugin install resumebot-setup@resumebot
```

Then say `set up resumebot`.

**A clone.** In a terminal:

```
git clone https://github.com/EricWurth/resumebot
cd resumebot
claude
```

Then say `set up resumebot`. [`AGENTS.md`](../AGENTS.md) sends Claude to the same skill. The clone is only the place Claude
reads the instructions from; the app is installed at a release tag in its own folder, never from the clone or a branch.

## What the install does, in order

The install is one script, `install.py` (in `app/scripts/local/`; the skill fetches the copy at the newest release tag and
runs it with `uv run --python 3.12 --no-project`). The skill
([`plugins/resumebot-setup/skills/install-resumebot/SKILL.md`](../plugins/resumebot-setup/skills/install-resumebot/SKILL.md))
checks the programs, asks for your name, runs the script and tells you the one thing it needs. Claude runs every command
itself; you never type or paste one. Your only actions are: answer one question up front (the name your roles are kept
under), approve the sign-in in the browser tab that opens, and restart Claude Code. `install.py --help` lists its options
(`--owner`, `--timezone`, `--data`, `--code`, `--repo`).

Every step looks first. Done and working: it says `already` and skips. Present but broken: it repairs only that piece.
Missing: it does it. So the same run installs, finishes an install that stopped half way and updates. A second run on a
finished install changes nothing under the code folder or the data folder.

| # | Step | What it does |
|---|---|---|
| 1 | prelude | rebuild PATH, find git, uv and Node 22 or newer; list any that is missing and stop |
| 2 | look | find the data folder, the code folder, the installed release and the newest `app-v<version>` tag |
| 3 | fetch | when the code is not the newest release, download it next to the old copy; the app keeps running |
| 4 | stop, backup | only when the code is replaced: stop the app, then copy the database (checked), `.env` and `secrets\` to `backups\` in the data folder; if that fails nothing has changed |
| 5 | replace | the old code folder is set aside as `-old-<time>`, the new one takes its path; the data folder is never touched |
| 6 | setup | `stack.py setup` (Python environment, locked packages, the page build) unless the environment already answers; refuses an unsafe SQLite |
| 7 | init | `resumebot init`, only when the settings are incomplete |
| 8 | start, smoke | `resumebot start` unless this version already runs, then `resumebot smoke --lanes` |
| 9 | signin | `resumebot signin` unless a token is stored: a browser tab asks you to approve; that is all you do ([`connect-claude.md`](connect-claude.md)) |
| 10 | connect | `resumebot connect`: register the app's two doors with your Claude Code |
| 11 | shortcut | put **resumebot** on your desktop (`resumebot.lnk` on Windows, `resumebot.command` on macOS) |
| 12 | doctor | `resumebot doctor`: one line per piece |

Progress is in `install\install.json` in the data folder (each step's state, and `waiting-for-user` with the one action
needed) and `install\install.log` (the same lines with the tail of any child's output, tokens removed). A failure is one
sentence, `<step>: <what failed>: <what to do>`, and an exit code: 3 a program is missing, 4 the data could not be backed
up (nothing changed), 5 setup failed, 6 the app would not start, 7 sign-in failed or timed out, 8 a door could not be
registered, 9 `doctor` found a problem, 10 another install is running. Run the script again after fixing it.

After the script, the skill has you restart Claude Code (in the desktop app, close and open the whole app) and runs
`resumebot doctor` once. For the getting-started agent, `resumebot project` then writes three helper agents (career coach,
master resume help, network-profile draft) into your project folder, and the restart loads the doors and the helper agents;
onboarding takes over ([`onboarding.md`](onboarding.md)). Rough times: setup about a minute, start 20 seconds or so, sign-in a minute.

The project folder is your own: start Claude Code in an empty folder and say `get me started` (the setup plugin's
`getting-started` agent follows these steps for you). The app is not in that folder; it is in the two folders below.
`resumebot project [folder]` refuses the app's own folders, writes only `.claude/agents/` in the folder, and never
changes a helper file that is already there.

You may see coloured text and a warning about a large page file while the page builds. Both are harmless.

The install never starts the app from inside the Claude session, because an app started there stops when Claude updates
or closes. It never reads or asks for your token: the sign-in window reads it off its own screen and stores it without passing it through Claude.

## Where everything goes

| What | Where |
|---|---|
| The app's code (replaced at an update) | `%LOCALAPPDATA%\Programs\resumebot` |
| Your data (never replaced) | `%LOCALAPPDATA%\resumebot` |
| Resumes and cover letters it builds | `Documents\resumebot`, one folder per role (made on the first save) |

On macOS the code is in `~/.local/share/resumebot-app`, the data in `~/.local/share/resumebot`, the documents in
`~/Documents/resumebot`, and the `resumebot` command at `~/.local/share/resumebot-app/app/backend/.venv/bin/resumebot`.
Wherever this page writes a Windows path, read the macOS one.

The data folder holds `.env` (your name, time zone, the database address and a few settings), `resumebot.db` (the
database, one SQLite file), `secrets\` (the doors' tokens and the agents' sign-in), `logs\`, and `browser-profile\` if you
use the browser jobs. The two folders are separate: neither is inside the other.

The page listens on `127.0.0.1:8430`, this machine only; the engine behind it is on 8530. To use another port, set
`RESUMEBOT_APP_URL` in the data folder's `.env` (the engine takes that port plus 100). `stack.py setup` first tests every port the install will use (the page's, the engine's, their UDP notice ports, the five lanes' and, if the stack runs its own Postgres, its port); when one is taken it stops and says which port, what holds it and which setting moves it.

`resumebot init` sets `RESUMEBOT_DATA_DIR` to the data folder for your user, so later commands find it in a new terminal.
Until then, every command also takes `--data "<data folder>"`.

## Checking it

The `resumebot` command is in the app's environment, at
`%LOCALAPPDATA%\Programs\resumebot\app\backend\.venv\Scripts\resumebot.exe`. It is on your PATH only if the install put
it there; if it is not, call it by that full path. Add `--data "<data folder>"` if you did not set `RESUMEBOT_DATA_DIR`.

```
resumebot doctor
resumebot status
```

**`doctor`** prints one line for each piece, `ok` or `MISSING` with what to do, and exits 1 if anything is missing:

| Line | What it checks |
|---|---|
| Claude Code is installed | it can find `claude`, on the PATH or where Windows installs put it |
| Claude Code is signed in | `claude auth status` succeeds |
| agents sign in | a token is stored (`resumebot signin`); until that step this line is `MISSING` |
| Node is there | Node 22 or newer |
| SQLite is clear of the WAL-reset bug | the version is a safe one |
| settings | the data folder has its `.env` |
| the app answers | the page answers at its address |
| `resumebot-owner`, `resumebot-browser` | each door answers, and your Claude Code has it registered at this install's address |

The last line, `logs`, is not a check: it names the logs folder (`<data folder>\logs`) and the newest `page`, `engine` and
`supervisor` log file in it, by name only.

**When something fails**, send the last 200 lines of `page.log`, `engine.log` and `supervisor.log` from that folder, and
read them yourself first. They hold folder paths, which include your Windows user name. The app never writes tokens or
your resume text to them.

A door line that says "your Claude Code does not have it" means `resumebot connect` has not run yet: run it. One that says
your Claude Code has it registered at a different address means it points at another install: run `resumebot connect`
for this one.

**`status`** says where onboarding stands, in plain lines, and always exits 0. `resumebot status --for-claude` prints the
same lines; it changes nothing. A fresh install reads like this:

```
resumebot 1.193.0
install: set up at <data folder>
app: running at http://127.0.0.1:8430
resume: not imported yet
tracker: not imported (optional: only if they have one)
employer list: not imported (optional: only if they have one)
targets: not set yet
first search: not run yet
mail: not connected
next: import the master resume (...)
```

The `next:` line names the first step still to do. If the app is not running it says so and how to start it.

In your project folder, `.claude/agents/` should hold `career-coach.md`, `master-resume-help.md` and
`profile-draft.md`. To take a newer copy of one after an update, delete your file and run
`resumebot project` again.

`stack.py status` and `stack.py smoke --lanes` (see [Starting and stopping](#starting-and-stopping)) say what is running and run read-only
checks of the app and the five agent lanes.

## Starting and stopping

Double-click **resumebot** on the desktop (`resumebot shortcut` makes it; Windows and macOS). It starts the app if it
is not running, waits until it answers, and opens the page in your default browser; if the app already runs it only
opens the page. On Windows it opens no console window; on macOS the file is a shell script, `resumebot.command`, and
Finder runs it in a Terminal window you can close afterwards. Pin it to the taskbar or the Start menu if you like. If it fails, the reason is written
to `logs\open.log` in the data folder.

Nothing starts the app at sign-in. `resumebot start` starts the app and the five lanes now, outside the Claude session,
and waits until the app answers; `resumebot smoke --lanes` runs the read-only checks. An older install that had the
sign-in task has it removed the next time `resumebot start` or `resumebot shortcut` runs.

Two installs on one PC (a second data folder) do not take over each other: the start task `resumebot-start` and the desktop
shortcut `resumebot.lnk` are machine-wide, so an install whose task or shortcut already starts another data folder leaves
it as it is and makes its own beside it, `resumebot-start-<tag>` and `resumebot-<tag>.lnk`. A single install keeps the plain names.

`stack.py` is the app's own launcher. Run it with the app's own Python, and name the data folder every time:

```
cd %LOCALAPPDATA%\Programs\resumebot
app\backend\.venv\Scripts\python.exe app\scripts\local\stack.py --data "%LOCALAPPDATA%\resumebot" start
```

| Command | What it does |
|---|---|
| `start` | starts the page, the engine and the five lanes in the background, with no window, and returns when `/health` answers |
| `start --hold` | the same, with the scheduled searches and the lanes off: the page works, nothing runs by itself |
| `stop` | stops the lanes, then the engine, then the page |
| `status` | says what is running, and what `/health` says |
| `smoke` | read-only checks of the running app; `smoke --lanes` checks the five lanes too (use plain `smoke` on a held app) |
| `run` | like `start`, in this window, until Ctrl+C |

Start the app with the desktop shortcut or `resumebot start`, not with `stack.py start` from inside a Claude session: an
app started there stops when Claude updates or closes.

The programs' logs are in `logs\` in the data folder: `page.log`, `engine.log`, `supervisor.log`, `lanes.log` and
`open.log`. A log over 10 MB is set aside, and three are kept.

The page is at `http://localhost:8430`. A start refuses and says so if something already answers at the page's or the
engine's address (8430 and 8530 unless you changed the port).

## Updating, finishing, installing: one flow

There is one flow for a fresh install, for an install that stopped half way and for an update to a newer release. Say
`set up resumebot` (or `update resumebot`) to Claude; it runs the install script, which never stops at an existing
install. The code folder holds nothing of yours, so it is replaced, never repaired, and only when it is not already the
newest release; the data folder holds your work, so it is never deleted, overwritten or renamed. The steps are the table
above. You are asked only for what is missing: your name on a fresh install, the sign-in approval when the agents are
not signed in. If the script is interrupted, run it again: it picks up where it stopped. The backups in `backups\` are
yours to delete when you are happy with the update.

To go back after an update, stop the app (`stack.py --data "<data folder>" stop`) and copy the newest backup's contents
over the data folder. Between updates, `scriptsackup_local.py --data-dir "<data folder>"` (run from the code folder with
the app's own Python) makes a database backup of its own in `backups\` in the data folder.

## Removing it

1. Stop the app: `stack.py --data "<data folder>" stop`.
2. Delete **resumebot** from your desktop.
3. Remove the two doors from Claude Code:

   ```
   claude mcp remove resumebot-owner --scope user
   claude mcp remove resumebot-browser --scope user
   ```

4. Delete the code folder, the data folder and `Documents\resumebot`. The data folder holds your work: your master
   resume, every role and your settings. Keep a backup if you may come back.
5. If you or the install set `RESUMEBOT_DATA_DIR` or added the app's folder to your user PATH, remove those in Windows'
   environment variables settings.
6. If you added the plugin, remove it: `/plugin uninstall resumebot-setup@resumebot`.

`stack.py` is `%LOCALAPPDATA%\Programs\resumebot\app\scripts\local\stack.py`, run with the app's own Python,
`%LOCALAPPDATA%\Programs\resumebot\app\backend\.venv\Scripts\python.exe`.
