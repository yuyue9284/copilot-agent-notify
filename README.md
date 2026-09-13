# Copilot Agent Notify

A GitHub Copilot CLI plugin that displays a desktop notification when the main
agent finishes a turn, or when Copilot needs permission or additional user
input. Subagent completion alerts are disabled by default and can be enabled.
Inside Zellij, it also decorates the originating pane and its tab with working
and attention icons. Windows Terminal also gets a session-wide progress
indicator, independent of the focused pane. The plugin does not set its tab title.

Notifications identify the Copilot instance and session:

```text
[WSL:Ubuntu] My session title [#5ab108a5]
Input needed.
Go to: /home/user/project
```

When enabled, subagent notifications retain the parent session title and
identify the child:

```text
[WSL:Ubuntu] My session title [#5ab108a5]
Subagent complete: Explore [#8c21f47a].
Go to: /home/user/project
```

Each subagent uses its own notification tag, so it does not replace the parent
session's completion notification.

Native Windows notifications use `[Windows]`. Linux, macOS, and other Unix
systems use their operating-system name. The short session ID disambiguates
sessions with the same title and working directory.

## Install

Install directly from GitHub:

```bash
copilot plugin install yuyue9284/copilot-agent-notify
```

Restart Copilot CLI after installation. Use `copilot plugin list` or
`/plugin list` to confirm that `personal-agent-notify` is enabled.

## Desktop session gadget (Windows + WSL)

Build the ready-to-run application explicitly from Windows PowerShell:

```powershell
.\windows-helper\build-gadget.ps1
```

This uses the installed .NET Framework MSBuild/C# toolchain, not a modern .NET
SDK. Output is `windows-helper\gadget\bin\Release\CopilotSessions.exe`, its
`.exe.config`, and the three shared Python collector files beside it. Rebuild
after changing native or collector sources. Normal launch never compiles code.

Run `scripts\gadget.cmd` from Windows Explorer, or launch the executable directly:

```powershell
.\windows-helper\gadget\bin\Release\CopilotSessions.exe --python C:\path\to\python.exe
```

`--python` selects an absolute native Windows Python 3.9+ executable. Without it,
the native host attempts interpreter discovery and reports missing dependencies.
`python scripts/session_gadget.py` remains a thin compatibility launcher that
passes its interpreter to the prebuilt app; it does not monitor sessions or build
the window.

To install the prebuilt application and a **Copilot Sessions** desktop shortcut
under `%LOCALAPPDATA%\CopilotAgentNotify\gadget`, run:

```powershell
.\windows-helper\install-gadget.ps1
# Optional explicit interpreter:
.\windows-helper\install-gadget.ps1 -Python C:\path\to\python.exe
```

The installer validates native Python 3.9+ and targets `CopilotSessions.exe`
directly with `--python` in the shortcut. It preserves existing `gadget.json`,
`gadget-ui.json`, `gadget-forgotten.json`, and owner caches.
This does not enable automatic startup. Rebuild and reinstall after updating the
repository. Close an open gadget before updating it. To
uninstall, remove that gadget directory and the desktop shortcut. If your
PowerShell authorization blocks scripts on a WSL UNC path, use an approved local
Windows checkout/build location or request administrator guidance; do not bypass
or change execution policy. `gadget.cmd` can launch an already-built app but
cannot replace the build step. The executable opens without a console window.

The native C#/WPF window uses a consistent dark theme, status badges, summary
cards, and directory subtitles. It has an **Always on top** toggle and combines native
Windows sessions with sessions in running WSL distributions. Each row shows its
environment, title, short session ID, and status; selecting it shows the full ID,
owner PID, and working directory.

In local sessions on Windows 11 build 22621 or later, the window prefers native
Desktop Acrylic: dark navy surfaces over a system-blurred backdrop, with opaque
text and controls. Remote sessions deliberately use a predictable **plain
translucency** fallback, also used when Acrylic is unsupported. This fallback is
about 90% opaque (230/255), has **no blur**, and uniformly blends the entire window,
including text and the standard title bar. It does not make the window click-through.
Standard resizing, title-bar actions, and both layouts are preserved.

High contrast or disabled Windows **Transparency effects** keeps the window fully
opaque; unexpected native failures also restore the readable opaque appearance.
The gadget observes appearance/session changes without changing system settings.
Windows can suppress Acrylic according to activation, power, and hardware
conditions: API support alone does not guarantee visible blur, and the remote
fallback policy is not a claim that Acrylic never works over Remote Desktop.

The custom terminal/checkmark icon is used for the app, taskbar, and installed
desktop shortcut. Its multi-resolution assets are generated locally by
`windows-helper/gadget/generate_icon.py`, without downloading third-party artwork.

Click **Session**, **Environment**, **Status**, or **ID** to sort; click the same
header again to reverse direction. The arrow shows the current direction, and
both the chosen sort and selection survive live updates. Status sorts by attention,
working, loading, unknown, then done, rather than alphabetically.
Both layouts use slim, rounded dark scrollbars with hover and drag feedback;
mouse-wheel scrolling, track paging, dragging, and keyboard navigation remain available.

### Layouts and unread completions

Use **Compact** in the header to switch layouts:

- **Comfortable** (default): summary cards, directory subtitles, and all four
  columns, initially 900 x 530.
- **Compact**: initially 480 x 300, resizable down to 420 x 230, shorter rows,
  no summary cards or directory subtitles, and no ID column. Session, Environment,
  and Status remain sortable; select a row to see its directory and full ID in
  the footer tooltip. **Pin** is the same always-on-top toggle.

Dimensions are WPF logical pixels and scale with Windows display settings.
The layout choice is saved in `%LOCALAPPDATA%\CopilotAgentNotify\gadget-ui.json`
and restored at the next launch. Switching layouts does not reset sorting or
unread state; custom window size and sort order are not persisted across launches.

When an observed working/waiting session becomes **Done**, it gets a **Done · New**
label and adds one to the taskbar unread-completion overlay. Multiple completions
are counted by session, not by message. The overlay shows up to `99+`; the
**Mark all read** button and taskbar description show the full count.

Select a session to mark it read, click an already-selected row, or press Enter
or Space on it. **Mark all read** clears all counts. Merely bringing the window
to the foreground or refreshing/sorting does not mark completions read.
If a session resumes work or closes, its old completion badge is removed.
Transient `Loading`/`Unknown` states do not create completion alerts.

Unread state is tracked only while this gadget instance is open, not persisted:
sessions already done when it opens are not retroactively counted. A turn that
starts and finishes entirely between polls may not be observed. Windows controls
taskbar overlay visibility (including small-icon and taskbar grouping settings);
the unread indicator and count remain available inside the gadget.

### Forget retained sessions

Select a row and click **Forget** (available in both layouts) to dismiss it locally
and clear its unread badge. This does **not** stop Copilot, delete its history,
or change its actual activity status.

The session stays hidden through polls, gadget restarts, and reconnects until it
records a new user message, assistant turn start, tool execution start, or
permission/input request. Model changes, session resume bookkeeping, and completion
events alone do not undo the dismissal. A resurfacing session shows its real
status; if new activity has already completed, it receives an unread completion
badge. Other sessions in the same CLI process are unaffected.

Dismissals are saved in `%LOCALAPPDATA%\CopilotAgentNotify\gadget-forgotten.json`,
using environment/session identities and fingerprints of activity records, never
prompt contents. **Forget** is disabled while history is still loading or its
status is unknown, so an incomplete replay cannot establish an incorrect baseline.
To restore all dismissed rows manually, close the gadget, remove only this
dismissal file, and reopen it. Configuration and layout files are separate.

### Choose WSL distributions

Create or edit `%LOCALAPPDATA%\CopilotAgentNotify\gadget.json` on Windows:

```json
{
  "wsl_distros": ["Ubuntu"]
}
```

This monitors only the running `Ubuntu` distribution, while native Windows
monitoring stays enabled. Names are case-insensitive; stopped distributions are
not started. Use `[]` to disable WSL monitoring, or `null` to monitor all running
distributions (also the default when the file or setting is absent).
Invalid configuration is reported rather than silently monitoring everything.
Restart the gadget after editing this file. The installer preserves this config.

| Status | Meaning |
| --- | --- |
| In progress | The root or one of its background children is working |
| Needs input | A permission or input request is waiting |
| Done | No tracked work remains; the Copilot process is still open |
| Loading | The initial transcript replay has not caught up yet |
| Unknown | A transcript or collector could not be read reliably |

Sessions disappear after their process closes. `Done` means the turn is idle,
not that its code or commands succeeded. Long-running work is never marked done
because of a timeout. Unresponsive collectors show `Unknown` and an error instead.
Normal status refresh is about two seconds; WSL distribution discovery is every
ten seconds. Large transcripts can take several refreshes to load initially.

Requirements and scope:

- Windows Python 3.9+, .NET Framework 4.8 with WPF assemblies,
  and `python3` 3.9+ in each running WSL distro. Tkinter is no longer required.
  No third-party Python packages, .NET SDK download, or browser runtime is needed.
  The explicit build additionally requires the installed Framework MSBuild/C#
  tools. Windows Python remains required in this migration phase: only host
  orchestration has moved to C#, not the shared discovery/reducer code.
  Previous `gadget-build` runtime-compiler caches are no longer used; existing
  preferences and owner caches retain their paths and semantics.
- Discovery covers the signed-in Windows user's `~/.copilot` and each running
  distro's **default user's** `~/.copilot`. `COPILOT_HOME`, when set in the
  collector's environment, overrides the home for that OS. Shell-profile-only
  variables are not loaded by the WSL collector. Other OS users and custom homes
  not configured in that environment are outside the discovery scope.
- Zellij and the notification hooks are not required. Live `inuse.PID.lock`
  files identify owners. Once verified, the scanner caches the process start
  token and lock-file identity; Linux entries also include the boot ID.
  This keeps live sessions visible across WSL clock shifts without trusting a
  reused PID or a lock from an earlier boot. Existing exact-match activity-hook
  registrations can recover sessions first discovered after a clock shift.
  Previously unseen locks without that evidence still require the initial
  process-start timestamp check; the scanner does not blindly adopt old locks.
  The metadata-only cache lives under `gadget-owners` in the OS-local notification
  state directory (`~/.cache/copilot-agent-notify` by default on WSL,
  `%LOCALAPPDATA%\CopilotAgentNotify` on Windows). Cache errors are reported.
  Each live-owned root transcript has its own row, even when several sessions
  share one CLI process. Multiple owners of the same session are deduplicated
  by session identity, not PID. Closing one session does not remove its siblings;
  stale locks, missing transcripts, and explicitly shut-down sessions are excluded.
  Child work within a root transcript is included in that root's status.
- The native application owns typed snapshot validation, source aggregation,
  monitoring, and collector process cancellation. It launches a Windows Python
  collector and sends the same read-only Python collector code over local WSL pipes.
  Each collector emits versioned NDJSON (`protocol_version: 1`, `sessions`,
  `errors`). The shared Python scanner, activity reducer, and hooks are retained;
  there is no Python parent feeding the WPF window during normal operation.
  Nothing needs installing into the distro, and stopped distros are not
  intentionally started. Closing the window stops its collectors.
- The gadget does not modify transcripts, terminal titles, or notification
  settings. No HTTP server, network service, telemetry, prompt content, or tool
  output is exposed. Session titles and directory paths are visible on screen.

For native-only JSON diagnostics, run `python scripts/session_gadget.py --snapshot`.
This reads one batch, so large transcripts may report `Loading`.
`python scripts/session_probe.py --watch` streams collector snapshots; closing
its stdin requests shutdown on Windows and Linux. The executable's
`--collector-stdin` mode accepts the older `{rows, errors, discovering}` UI input
only for compatibility and tests, not normal monitoring.

### Gadget regression tests

On Linux/WSL:

```bash
PYTHONPATH=tests python3 -m unittest test_session_gadget test_activity test_outer_progress -q
```

On native Windows, from the repository root:

```powershell
$env:PYTHONPATH = "tests"
python -m unittest test_session_gadget -q
```

The Windows suite explicitly compiles all production C# sources into the
`GadgetHostTests` and `GadgetUiTests` runners and exercises header clicks,
live sorting, selection, compact layout persistence, unread badge transitions,
and repeated normal window closure with an open collector pipe. The native host
runner replaces the former Python source/configuration, worker lifecycle, and
window-pipe tests: allowlists and discovery, stale/failed source recovery,
cancellation before/during creation, blocked bootstrap, graceful EOF, and genuine
I/O/process errors. Both platforms test the shared scanner/reducer, versioned
watch protocol and EOF, injected WSL bootstrap, and prebuilt compatibility launch.
Fixtures use isolated test homes/caches and local processes, not real user
transcripts. Native UI tests briefly open test windows.
The optional `COPILOT_GADGET_LIVE_TEST=1` check requires open Windows and WSL Copilot
sessions and starts real read-only collectors; it is disabled by default.

## Secret-scanning Git hooks

This repository includes Gitleaks hooks in `.githooks/`:

- `pre-commit` scans the staged changes, not unstaged files.
- `pre-push` scans all local Git history, including all refs and merge diffs.
  This intentionally also blocks secrets on other local branches or in earlier
  commits, even if the secret was deleted from the current files.

Install Gitleaks 8.30.1 or later natively for each environment where you use Git.
The hooks look for `gitleaks` on `PATH`, then `~/.local/bin/gitleaks` on Unix or
`~/.local/bin/gitleaks.exe` under Git for Windows. `GITLEAKS_BIN` can specify a
different executable. Use a Linux binary in WSL, not a Windows executable.

Enable the hooks **once per clone**:

```bash
git config --local core.hooksPath .githooks
```

Check for existing hooks or a configured `core.hooksPath` before changing it;
Git uses only one hooks directory. Hooks are not automatically enabled by cloning.
The scanner must be installed separately; no binaries or credentials are stored
in this repository. Missing scanners, scan errors, and findings block the operation.
Findings redact secret values, and inline `gitleaks:allow` comments are not honored.

These are local safeguards, not a guarantee: Git hooks can be bypassed and scanners
cannot identify every secret. GitHub push protection is a complementary server-side
control. A secret that has already been exposed should be revoked or rotated,
not merely deleted from the latest revision.

## Configure subagent alerts

`COPILOT_NOTIFY_SUBAGENTS` controls subagent completion alerts:

| Value | Behavior |
| --- | --- |
| Unset, empty, or `0` (default) | Only main-agent completion alerts |
| `1` | Main-agent and subagent completion alerts |

Disabled subagent completion alerts produce neither a desktop notification nor
a terminal bell. Permission and input-needed alerts remain enabled regardless
of this setting, including those associated with subagents.

Child sessions can also emit `agentStop` before the parent's `subagentStop`.
The scripts compare `agentStop`'s session ID with the root session ID in the
transcript's initial `session.start` record and suppress child events. Only
`subagentStop` sends the opt-in child completion alert, avoiding duplicates.
If a supplied transcript cannot be read or its session header is invalid, the
hook reports an error rather than sending a potentially misidentified alert.

To enable subagent completion alerts, set the variable in the shell that
launches Copilot:

```bash
export COPILOT_NOTIFY_SUBAGENTS=1
```

```powershell
$env:COPILOT_NOTIFY_SUBAGENTS = "1"
```

Set it to `0` to disable them again. Restart Copilot after changing the variable
so its hooks inherit the new value. Other nonempty values cause subagent
completion hooks to report a configuration error.

## Zellij activity indicators

Enabled by default only when both `ZELLIJ_SESSION_NAME` and `ZELLIJ_PANE_ID`
are present. `COPILOT_NOTIFY_ICONS=0` disables this feature without disabling
desktop notifications or bells. Unset/empty/`1` enables it; other values report
a configuration error. Restart Copilot after changing the setting.

Requirements:

- Python 3.9+ (standard library only): `python3` on Unix; `py -3`, `python3`,
  or `python` on native Windows, in that order. Install Python separately if
  missing; the hooks never download dependencies.
- Zellij with JSON `list-panes` and `list-tabs`, `rename-pane --pane-id`, and
  `rename-tab-by-id` support (the target versions are 0.44.0/0.44.1).
- A native interpreter and native Zellij for the same OS. Do not run Windows
  Python against WSL Zellij or vice versa.

The prefix uses the hourglass U+23F3 for working and warning U+26A0 for attention,
without numeric counts. For example, `[\u23f3 | \u26a0] work` renders with the
actual Unicode icons. Each icon means at least one Copilot root session in that
pane or tab has the corresponding state; a root with a working actor and a
different waiting child can show both icons. If A and B share a tab, finishing A
leaves that tab working until B finishes. Separate tabs are aggregated
independently, including inactive tabs. Idle names have no checkmark or other
permanent decoration.

### Outer Windows Terminal indicator

When running under Windows Terminal (`WT_SESSION` is present), the coordinator
also aggregates all registered Copilot sessions across the Zellij session:

- Working: an indeterminate progress ring replaces the outer tab's normal icon.
- Input or permission needed: a paused, full progress ring takes precedence.
- Last active session finishes: the progress ring clears and an outer terminal
  bell requests attention so you can check the results. Other still-working or
  waiting sessions prevent this all-finished alert.

The completion bell is sent once per observed active-to-idle transition, not
repeated on idle polls, startup, detach cleanup, or coordinator shutdown.
Windows Terminal controls the bell badge's appearance and clears it on focus
(an already focused tab may show it only briefly). It is not a persistent unread
counter. Existing per-session desktop notifications and bells remain enabled.

This is Windows Terminal's progress UI, not an hourglass in its title. Zellij
can still change the outer title when focus moves, but that does not clear the
progress indicator. Inner pane/tab icons keep their per-pane/tab meaning.
Progress can also appear on Windows Terminal's taskbar button.

Supported on WSL/Linux and native Windows, for explicitly named interactive
clients such as `zellij attach term` or `zellij --session term`. Unnamed attach,
SSH clients, and other terminals are not targeted. New attached clients are
discovered about every five seconds. Native Windows requires Windows PowerShell
for client discovery. Each matching Windows Terminal client gets the indicator.
It is delivered directly to the client's outer terminal, not through a Zellij pane.
Native console attachment runs in a short-lived helper so it cannot invalidate
the coordinator's standard handles or prevent later idle-state updates.
Progress transitions are logged as `outer progress client=PID state=N`:
`0` clears the indicator, `3` means working, and `4` means attention.

`COPILOT_NOTIFY_OUTER_PROGRESS=0` opts out without disabling inner icons or
desktop notifications; unset/empty/`1` enables it. Restart the coordinator after
changing this setting. The feature shares Windows Terminal's progress channel:
other programs writing progress to the same outer terminal can replace it.
The coordinator tracks the client terminals it has written to, but cannot detect
another program taking over that progress channel. After detach it attempts to
clear through the original parent terminal; if both processes exited, restoration
is unavailable. A crashed coordinator recovers its progress journal on restart.

### State and lifecycle

`sessionStart`, `userPromptSubmitted`, and `agentStop` register the session.
Initial hooks resolve `COPILOT_HOME/session-state/<sessionId>/events.jsonl`
(default `~/.copilot`); a supplied `transcriptPath` takes precedence.
The initial `session.start` must match the registration's session ID. Child
`agentStop` cannot register against its parent's transcript or clear root work.
`sessionEnd` unregisters only its matching process identity.

One detached Python coordinator per OS-local Zellij session owns all title
writes. Unix uses `flock`; Windows uses a named OS mutex. Registrations are
atomic and serialized separately from the writer lock. Each record tracks
the caller's pane ID, not focus, and the CLI PID plus process start identity.
The CLI owner is found among hook ancestors using `inuse.<pid>.lock`; before
that file exists, the nearest `copilot`/`node` ancestor is used.

The coordinator polls about once per second (plus Zellij RPC time), tails
transcripts incrementally, and re-queries pane-to-tab mappings each cycle.
Large initial backlogs are processed in bounded batches without publishing
historical working states. Partial JSONL lines wait for completion; truncation,
replacement, and resume reset state. Pane moves update both old and new tabs.
No prompt, reasoning, tool result, or assistant text is copied into state files.

There is **no working-session inactivity timeout**: a long tool invocation
stays working while its owner is alive. Dead/reused owner PIDs, removed panes,
session shutdown and sessionEnd remove registrations. A missing startup
transcript gets a 60-second grace period; a later prompt/stop retries registration.
Detaching does not clear work while the CLI and Zellij session remain alive.
When the last registration disappears, the coordinator restores owned names
and exits. If Zellij has already disappeared, it logs that restoration was
unavailable and exits rather than retrying forever.

### Exact event semantics and limitations

- Root `assistant.turn_start` and an idle-delivered root user message start
  working. Queued/system/child messages do not independently start a root turn.
  Turn numbers are not assumed monotonic.
- `assistant.turn_end` alone does **not** mean the request finished. Idle normally requires
  both a root assistant message without tool requests and a completed matching
  root `agentStop` hook, in either order. Hook failure is logged, but does not
  keep a finished response working merely because notification delivery failed.
  A later actual turn starts work
  again, including stop-hook continuations. This is transcript-observed state,
  not a contractual CLI "request complete" API: a stop hook that subsequently
  blocks can cause a brief idle interval before continuation appears.
- When stop hooks are absent, an explicit `phase: final_answer` message without
  tool requests followed by the root's matching `assistant.turn_end` also ends
  root work. This fallback requires a turn ID, no running root tools, and no
  pending stop hook. Background children still keep the session working.
  Unmarked messages, commentary, and ordinary tool-turn endings do not qualify.
- Root background children keep the session working until their
  `subagent.completed`/`subagent.failed` event. Their completion cannot clear an
  active parent. Mixed child metadata is correlated by the spawning tool ID.
  A child's new `assistant.turn_start` also restores its working state after
  completion, since resumed agents may not emit another `subagent.started`.
  Its next final response plus stop-hook completion, or explicit final-answer
  phase plus matching turn end, clears only that child.
- `ask_user` waits clear on the matching tool completion. Permission/input
  notification hooks also show attention for children, independently of
  `COPILOT_NOTIFY_SUBAGENTS`. Without an explicit tool correlation ID, attention
  clears on that actor's next assistant turn (or root user response/final stop,
  child completion, abort/shutdown). An unrelated background tool completion
  never clears it. Consequently, a permission indicator can remain until the
  next model turn after approval; schemas without actor/correlation metadata
  cannot provide perfectly precise per-child working counts.
- Explicit transcript `abort` clears current state; errors/retries without an
  abort, stop, or shutdown are not guessed to be idle. Transcript event schemas
  are implementation details and may need updates with future CLI releases.
- An explicit pane rename freezes its terminal-driven dynamic title. Zellij's
  listing does not distinguish manual names from dynamic ones, so idle restores
  the captured **text**, not dynamic-title mode. After ending the session, use
  Zellij's `undo-rename-pane --pane-id terminal_ID` yourself if dynamic titles
  are desired; the coordinator deliberately does not guess and erase manual names.
- A user rename detected while working wins: decoration for that pane/tab is
  suppressed until it becomes idle, and the next work interval uses its new
  base name. Idle registered sessions never repeatedly rename titles.
  Zellij offers no atomic compare-and-rename, so a rename in the tiny interval
  between a listing and an action can still race.
- A durable ownership journal restores only exact names this coordinator wrote.
  A killed coordinator is restarted by the next registration hook and recovers
  the journal; there is no separate watchdog. Do not delete its journal while
  decorated names remain. Renaming a Zellij session, moving a Copilot process
  into a different Zellij session, or manually sharing caches across machines
  is not supported.

### Diagnostics and recovery

State is under `$XDG_CACHE_HOME/copilot-agent-notify/zellij/<key>` (default
`~/.cache/...`) on Unix, or `%LOCALAPPDATA%\CopilotAgentNotify\zellij\<key>` on
Windows. `COPILOT_NOTIFY_STATE_DIR` overrides the base directory for isolated
tests; OS and Zellij session name still form the key. Native Windows and WSL
sessions named `term` therefore never share state. Keep the cache OS-local.
`COPILOT_NOTIFY_ZELLIJ` can point to an explicit Zellij executable.

`status.json` reports the writer PID, start identity, heartbeat, registration
count, last error, and a separate `outer_error` for progress delivery.
`coordinator.log` records startup, shutdown, and changed
errors, rotating at 256 KiB with one backup. Startup/dependency failures report
to hook stderr; title failures do not gate the separate notification hooks.
Notification stdout/schema is unchanged.
The `status` command additionally checks the PID's start identity and reports
`alive`, so a stale heartbeat left by a killed writer is distinguishable.

Inside the relevant session, with the same state-directory setting:

```bash
python3 scripts/activity.py status
```

```powershell
py -3 .\scripts\activity.py status
```

After a coordinator crash, the next prompt restarts it. For explicit recovery,
run the foreground worker with the **exact** journal directory and session:

```text
python3 scripts/activity.py worker --directory CACHE_KEY_DIR --session SESSION_NAME --zellij ZELLIJ_EXECUTABLE
```

It exits immediately if a healthy writer owns the lock; otherwise it recovers
registrations and restores names when their owners have exited.

### Regression tests and safe live validation

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

```powershell
py -3 -m unittest discover -s .\tests -v
```

Tests create and remove uniquely named `.runtime-*` directories **inside**
`tests/`. Fake Zellij and desktop providers prevent changes
to real sessions or real toasts. Both actual shell wrappers are exercised when
their runtimes are available; the Windows notification test substitutes only
the helper executable path with a capture script. These tests do not establish
that a particular real Zellij build renders icons correctly.
Native PowerShell tests explicitly skip if the filesystem's execution policy
rejects scripts (for example, this checkout accessed through a WSL UNC path);
no policy is bypassed or changed. Run from an approved native checkout to
validate those wrappers on Windows.

An opt-in automated live test creates a uniquely named disposable Zellij
session, runs the real hook wrappers against replayed lifecycle records, checks
actual pane/tab names, and removes its session and fixtures:

```bash
COPILOT_NOTIFY_LIVE_TEST=1 python3 -m unittest discover -s tests -p test_activity_live.py -v
```

This passed on WSL Zellij 0.44.1. On the tested native Windows Zellij 0.44.0,
`attach --create-background` returned success but did not expose the disposable
session to subsequent actions, including when invoked outside the test harness.
The native Windows wrapper/coordinator regression suite passed from a local
path; native Windows live rendering still requires the interactive procedure
below. The automated live test reports this startup failure rather than skipping
it or claiming that rendering passed.

For live verification without installing or touching existing panes:

1. Open a fresh terminal, outside any current Zellij session. Choose a unique
   name such as `copilot-notify-check-<random-suffix>`; never reuse `term`.
   Start it with `zellij --session NEW_UNIQUE_NAME`.
2. Inside that disposable session, use the repository as the working directory
   and set `COPILOT_NOTIFY_STATE_DIR` to a new directory under the repository.
   Use the same setting for every test pane. Do not point it at normal caches.
3. Create two disposable panes in one tab (`zellij action new-pane`) and a
   separate disposable tab via the Zellij UI. Run
   `copilot --plugin-dir .` in each pane, loading this local
   plugin rather than reinstalling it. Avoid loading a second enabled copy of
   the notification plugin in the test CLI.
4. Submit overlapping work. Verify A finishing leaves B's tab working, both
   finishing restore the original tab name, a different tab stays independent,
   permission/input attention clears after resume, and child completion does
   not clear root work. Move a busy pane between the disposable tabs; rename a
   decorated pane/tab manually and verify that name is respected.
5. Exit the test Copilot processes, check `status` becomes `running: false`,
   and verify original/updated base names are restored. Exit the disposable
   Zellij session. Delete **only** that exact session with
   `zellij delete-session NEW_UNIQUE_NAME` and remove only the test state
   directory you created. Never use `kill-all-sessions`/`delete-all-sessions`.

Review/live validation should precede reinstalling. After approval, run
`copilot plugin install .` separately in WSL and native
Windows (using each environment's local repository path), then restart their
Copilot processes. Each OS needs its own Python and Zellij. Outer Windows
Terminal progress is aggregated separately for each OS-local Zellij session;
the plugin does not override the outer tab title.

## Notification delivery

The plugin uses `notify-send` on Linux, Notification Center on macOS, and the
compiled `windows-helper/bin/copilot-notify.exe` WinRT client on Windows and
WSL. Windows toasts are attributed to Windows Terminal and remain in
Notification Center. If no desktop notification provider is available on
Linux, the plugin emits a terminal bell instead.

WSL uses its `/init` interop launcher when available, so a missing automatic
Windows-executable handler does not prevent notification delivery. PowerShell
is not required for this WSL delivery path. Launcher/notifier errors are
reported rather than silently treated as successful notifications.

On Windows and WSL, each successfully submitted notification also sends a BEL
to the originating terminal, allowing Windows Terminal to show its tab bell
indicator. Hook stdout stays empty. WSL finds the nearest terminal in the
hook's process ancestry, so the bell travels through Zellij rather than through
captured hook output. On native Windows, the helper first looks for a Windows
Terminal ancestor and attaches to its direct child's console to ring the outer
tab. When native Windows Zellij is also in the ancestry, it first rings the
console of Zellij's nearest child (the originating pane's shell). This produces
both inner Zellij and outer Windows Terminal tab indicators without relying on
native Zellij to forward the bell. Plain PowerShell sessions receive only the
outer bell. If no Windows Terminal ancestor exists, it uses the attached console
or nearest available ancestor console.

No terminal settings are changed. Sound/visual behavior depends on Windows
Terminal's bell settings and the multiplexer. Inactive Zellij tabs may behave
differently by version. Headless IDE sessions without an ancestor terminal
still receive a toast and log that the bell was skipped.
Native Windows routing follows process ancestry, not Zellij's client registry;
reattached or multi-client Zellij sessions are not guaranteed to target the
currently viewing tab. Nested multiplexers beyond this two-level arrangement
are not explicitly handled. No focus changes are made. If one target console
is unavailable, the helper logs the skipped bell and still attempts the other.

Rebuild the Windows helper after changing `windows-helper/CopilotNotify.cs`:

The current build script requires .NET Framework 4.x's C# compiler, the .NET
Framework 4.8 reference assemblies, and Windows SDK 10.0.26100.0 metadata at
their standard installation paths. The prebuilt helper is included for users
who do not need to rebuild it.

```powershell
.\windows-helper\build.ps1
```

After changing the plugin, reinstall it to refresh Copilot CLI's cached copy:

```bash
copilot plugin install .
```

Remove it with:

```bash
copilot plugin uninstall personal-agent-notify
```

## Repository layout

- `plugin.json` and `hooks.json`: plugin identity and lifecycle hooks.
- `scripts/`: notification delivery and Zellij activity coordination.
- `windows-helper/`: native notification helper source, build script, and binary.
- `tests/`: standard-library regression tests and opt-in live Zellij tests.

The repository is named `copilot-agent-notify`; the installed plugin identifier
remains `personal-agent-notify` for compatibility with existing installations.
This is an independent project, not an official GitHub or Microsoft product.

## License

Licensed under the Apache License, Version 2.0. See `LICENSE` for the full text.
