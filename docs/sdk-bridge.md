# Optional SDK status bridge (prototype)

This prototype addresses an idle main agent waiting for a background shell:
the shell tool invocation has returned, but the command is still running.
It queries Copilot's own task registry instead of parsing tool-result text or
inspecting shell processes.

**Not installed automatically.** Task queries and the bridge have been exercised
against real, isolated CLI 1.0.85 runtimes. User-level extension discovery,
hosted shell tracking, and session-owner matching have also been exercised on
WSL/Linux and native Windows. Existing interactive session switching remains
unverified.
The default gadget still works without it. This is not a replacement for all
transcript-based status tracking.

## API basis and compatibility

The implementation follows the public `github/copilot-sdk` source at commit
`0dd9d4324339b84835c915b0700fdc5c25cec5af`:

- `nodejs/docs/extensions.md` and `nodejs/src/extension.ts`: a CLI-loaded
  `extension.mjs` joins the foreground session via
  `@github/copilot-sdk/extension` and `joinSession()`.
- `nodejs/src/generated/rpc.ts`: `session.rpc.tasks.list()` returns structured
  tasks, including shell IDs, status, attachment mode, and execution mode.
- `nodejs/src/generated/session-events.ts`:
  `session.background_tasks_changed` signals a task-registry change.

The task RPC surface is **experimental**. Source availability is not evidence
that every installed CLI version implements it. The CLI must supply the SDK
module resolver, extension lifecycle, and task-list method. An unavailable
method or unsupported shell schema is reported as an error, not an empty list.

## Design

`extensions/gadget-status/` deliberately sits **outside** `.github/extensions/`
so cloning this repository does not auto-load it.

The extension registers no tools or permission hooks, sends no prompts, and
requests no sensitive environment variables. It joins its host's foreground
session and makes task-list and bounded event-history queries. It does not start, cancel, promote, or
remove tasks. This is an observation-only implementation, not a claim that the
SDK connection is restricted to read-only operations by a permission boundary.

Task-change, tool-completion, notification, and root-activity events request a
refresh. Events within 50 milliseconds are coalesced; a five-second timer provides
reconciliation and freshness heartbeats. Each refresh also reads the newest
persisted main-agent activity through SDK `session.rpc.eventLog.read`
(`direction: backward`, `max: 1`, activity-type filter, `agentScope: primary`).
This restores activity after reload and updates it even when a host does not
deliver live event callbacks. Windows and WSL use the same path, including CLI
sessions launched from VS Code terminals. Newer live intent/progress events take
precedence over older persisted events by event timestamp.
Only one refresh is in flight at a time, with sequential SDK queries.
If events arrive during a query, its result is discarded and queried again before
publication. Hung queries stop publishing fresh status rather than accumulating
requests. There is no direct shell process inspection.

Shell tasks in `running` or `idle` state keep the gadget working. Terminal
statuses are `completed`, `failed`, and `cancelled`. Attached and detached shells
are both included: a deliberately long-lived detached server can therefore
keep the session working. Other task kinds remain the existing transcript
reducer's responsibility.

When observed pending shells finish while the root is inactive, a ten-second
handoff grace period holds the working state. Root resumption clears that grace.
Already-completed tasks in the initial snapshot do not start a grace period.

## Local data contract

The extension atomically writes `gadget-sdk.json` inside its session directory:

```text
COPILOT_HOME/session-state/SESSION_ID/gadget-sdk.json
```

`COPILOT_HOME` defaults to `~/.copilot`. The file contains only:

```json
{
  "protocol_version": 1,
  "session_id": "synthetic-session",
  "owner_pid": 42,
  "updated_at": 1700000000,
  "state": "ready",
  "pending_shells": 1,
  "settling": false,
  "latest_activity": "Checking synthetic tests"
}
```

The optional `latest_activity` field carries the most recently received
main-agent activity: `assistant.intent`, `tool.execution_progress` messages, or
the short `arguments.description` from a tool-start event. Tool starts without
a description show `Using <tool name>`, never raw commands or file arguments.
The latest of these events wins. Text is normalized to one line and limited to
240 UTF-16 code units by the bridge. Child activity is ignored. Text is retained
across assistant turns (including thinking between tool calls) and after completion
as the **latest** activity, not a claim of ongoing work. A new activity event replaces
it; it is omitted from display while bridge health is unknown.
On startup the bridge restores the latest matching persisted activity, if any.
It remains blank when the session has no matching activity. Ephemeral descriptions
are not guaranteed to survive a reload. Older bridge snapshots without this field
remain supported. An unsupported or failed event-history query surfaces
`activity_query_failed`, not a healthy blank value.

The gadget's **Latest activity** column is visible by default and can be hidden
with **Appearance > Show latest activity**. This preference persists in
`gadget-ui.json`; it controls display only, not local collection. Legacy sessions
remain blank. Activity text is not included in desktop notification bodies.

Commands, raw tool output, prompts, and shell IDs are not copied
directly into this file, but activity descriptions may themselves contain private task details
or paths. Review it before sharing snapshots or screenshots.
Error states use fixed labels rather than raw RPC errors.
The file remains local and must not be committed or included in public reports.

The shared `StatusProvider` in `scripts/activity.py` supplies both the gadget
collector and Zellij/Windows Terminal coordinator. It uses the snapshot only
for the matching session and owner PID.
A validated `starting` snapshot gets up to 60 seconds for initialization during
extension startup or a session switch. The gadget shows **Loading** without a
warning (transcript input requests still show **Needs input**). Terminal indicators
remain working, without treating successful initialization as a completion.
The startup window is bounded by both snapshot age and a collector-local monotonic
timer, so repeatedly rewriting `starting` cannot indefinitely hide a stuck bridge.
If initialization exceeds this window, the gadget shows **Unknown** with an
initialization-timeout warning. Invalid snapshots and explicit failures are not
given this grace.

A present but invalid, stopped, failed, or more-than-15-seconds-old ready snapshot
produces gadget **Unknown** and an error, never a false **Done**. Terminal
indicators show the existing attention marker, and the coordinator's local
`status.json` and log report the bridge error. Recovery to idle does not ring a
terminal completion bell. Bridge errors preserve the incremental transcript
reader and registration rather than causing full replays.

A future timestamp beyond five seconds also fails validation. A missing file
retains transcript-only behavior only if this collector has not seen a bridge
file for that session owner. Once seen, disappearance is an error until recovery,
an explicit legacy override, or collector restart. Healthy shell activity holds
**In progress**, while transcript permission/input requests still take priority.

Grace periods use a monotonic clock. Snapshot freshness uses wall-clock time;
a clock adjustment can temporarily cause Unknown. The existing Copilot owner
liveness checks remain in place.

## Backend selection

There is **no normal UI backend selector**. The default `auto` policy uses the
transcript baseline and supplements it with healthy SDK shell status when the
optional extension is available. It does not load the extension or connect to
another CLI process. Sessions without a bridge keep their existing behavior.
An unhealthy bridge is not silently treated as unavailable/idle.

For troubleshooting or removing a trial, merge this setting into
`COPILOT_HOME/copilot-agent-notify.json` for each monitored OS user:

```json
{
  "status_backend": "legacy"
}
```

Keep other existing settings, such as `hook_alerts`. Remove `status_backend`
or set it to `"auto"` to restore automatic selection. Configuration is reread
each collection cycle. `COPILOT_NOTIFY_STATUS_BACKEND=auto|legacy` overrides
the file when set in the collector/coordinator environment; WSL collectors do
not load shell profiles. Invalid backend values or unreadable configuration
are reported as errors rather than silently selecting a backend.

There is deliberately no forced `"sdk"` mode: the current SDK integration covers
background shells, not the full root/child/input lifecycle. Transcript parsing
is still required. Event coalescing reduces redundant bridge queries, but this
hybrid is **not a demonstrated overall performance improvement** over legacy.
It does not yet eliminate duplicate readers between gadget and terminal.

Standalone hook desktop notifications still follow hook events, not this
provider. If the gadget supplies your notifications, disable duplicate hook
alerts with `"hook_alerts": false`. Migrating hook completion decisions requires
separate lifecycle validation; this bridge does not change permission handling.

## Isolated validation

From the repository root:

```bash
node --test tests/sdk_bridge.test.mjs
PYTHONPATH=tests python3 -m unittest test_activity test_session_gadget test_outer_progress -q
```

The Node tests use an injected SDK-shaped session, not real Copilot sessions.
They cover task-schema validation, event refreshes, metadata minimization,
completion grace, root resumption, query failure, hung-query shutdown, and
atomic file replacement. Python tests cover consumption, freshness, ownership,
attention precedence, automatic/legacy selection, bridge disappearance/recovery,
incremental reader retention, terminal aggregation, and failure reporting.

## Live, model-free task-status POC

`scripts/sdk_status_poc.mjs` uses the actual SDK and a disposable CLI runtime.
It does not install the extension, attach to working sessions, or submit an
LLM prompt. It invokes the shell tool directly through the SDK to create
harmless timed commands, observes the task registry, and feeds the real session
events and queries into the prototype bridge.

Provide matching SDK and CLI files from an installed CLI distribution:

```bash
node scripts/sdk_status_poc.mjs \
  --sdk /path/to/copilot/copilot-sdk/index.js \
  --cli /path/to/copilot/index.js
```

Both paths must be absolute. The CLI argument can also be a native executable.
Use the SDK shipped with that CLI version rather than assuming the latest
public SDK matches an older runtime. No package installation is performed.

The POC creates a unique temporary home and working directory, passes only
essential OS environment variables, enables offline mode, and uses a synthetic
model with an unreachable loopback provider. No existing credentials or
configuration are copied. Completion can automatically resume the runtime's
root agent; the offline provider prevents a real model request from succeeding.
The test shuts down only its own runtime and removes its temporary directory.

Observed on September 18, 2026, using CLI 1.0.85 on WSL/Linux:

| Check | Result |
| --- | --- |
| Fresh task registry | Zero pending shells |
| Asynchronous timed shell | Structured `shell` task with status `running`; bridge reports pending work |
| Natural completion | Task becomes `completed`; bridge clears the pending count |
| Task-change subscription | `session.background_tasks_changed` events received |
| Root handoff | Root turn-start event observed; bridge cancels unnecessary settling |
| Cancellation through task RPC | Task becomes `cancelled`; bridge clears the pending count |
| Command deliberately exiting with code 7 | Task status is still `completed`, not `failed` |

**Task completion is not proof of command success.** The POC preserves that
distinction rather than labeling a terminal task as a successful command.

The POC prints a pass/fail result, event counts, and the observed nonzero-exit
classification, not transcripts, task IDs, commands from user sessions, or
credentials. A missing method, unsupported schema, absent running/terminal
transition, or missing task-change event fails the check.

This establishes live shell-task observability for the tested version. It
does **not** establish permission/input tracking, real model turn completion,
background-agent coverage, passive attachment to arbitrary running sessions,
or interactive extension/session-switching compatibility.

An additional isolated host check on September 18, 2026 used SDK sessions with
`requestExtensions: true` on WSL/Linux and native Windows, both CLI 1.0.85.
The runtime automatically discovered the bridge under
`COPILOT_HOME/extensions/gadget-status`, launched it, and published ready,
running-shell, and completed-shell snapshots without a manual extension reload.
The snapshot's owner PID matched the session's `inuse` lock.
This verifies the real extension host, not the interactive UI's session-switch
behavior. A Windows runtime shutdown can leave the last ready snapshot rather
than a stopped marker; owner liveness and freshness checks still apply.

## Before installing

For a user-scoped trial, copy `extension.mjs` and `bridge.mjs` together into
`COPILOT_HOME/extensions/gadget-status/` on each intended OS. The default is
`~/.copilot/extensions/gadget-status/`; Windows and WSL have separate homes.
Do not overwrite an existing extension with the same name without reviewing it.

In the tested interactive CLI setup, extension loading required experimental
mode. Installing files and restarting alone did not activate the bridge.
Use `/experimental on`, then `/extensions mode` and select **Load Only** if
you want installed extensions to run without agent-managed extension changes.
Experimental mode enables other experimental features too, not just this bridge.
Check `/extensions` or the Extensions section of `/env` for `gadget-status`.
These controls and requirements may change with CLI versions.

A running extension is not by itself proof that the gadget is consuming it.
With the `auto` policy, its session must have a fresh, ready `gadget-sdk.json`
matching the session and owner. No snapshot means legacy tracking for a newly
observed owner. The gadget currently has no per-session SDK/legacy badge.
Fresh, valid bridge snapshots were observed in interactive sessions on both
Windows and WSL after activation; switching between retained sessions remains
unverified.

Before enabling this prototype, validate it in a disposable CLI session on
each intended OS: load both `.mjs` files as one extension, start a background
shell, confirm the task-list response and owner PID, observe its completion,
then exercise session switching and extension shutdown. A fresh snapshot must
remain available for the foreground session without affecting its permission
handling or conversation.

Do not use `resumeSession()` in a separate CLI process to attempt to observe
an active user's session. This prototype uses only the CLI-owned extension
connection. Extension coverage of retained, non-foreground sessions has not
been established; stale data is intentionally surfaced.

To remove an installed trial later, select `legacy`, unload the extension, and
remove only its `gadget-sdk.json` files for the trial sessions. Restart the
collectors/coordinator before returning to `auto`, so remembered bridge presence
does not flag intentional removal as a failure. The current prototype does not
include an installer.
