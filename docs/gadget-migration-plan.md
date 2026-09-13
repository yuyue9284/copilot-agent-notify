# Gadget host migration

## Scope

Move Windows gadget orchestration into the existing C#/WPF application.
Keep the Python session discovery, transcript reader, activity reducer, and
notification hooks shared between Windows and WSL. Windows Python remains a
dependency in this phase.

Retain the current .NET Framework/WPF stack. A modern .NET runtime upgrade and
a compiled cross-platform collector are separate decisions, not prerequisites
for this migration.

## Target architecture

```text
CopilotSessions.exe
  WPF presentation and existing local preferences
  Typed monitor, snapshot validation, source aggregation, cancellation
    Windows Python collector
    WSL Python collectors (only configured, already-running distributions)
```

The application owns its collector processes. There is no Python parent process
feeding the WPF application in normal operation. Collector pipes remain local;
no service, network listener, transcript upload, or telemetry is introduced.

## Implementation stages

1. **Typed host and contracts**
   - Introduce validated snapshot/configuration models and a session-status enum.
   - Move discovery, worker management, freshness, error aggregation, and shutdown
     from `session_gadget.py` into C# services outside the presentation class.
   - Launch a native Windows Python collector and the existing WSL bootstrap.
   - Wire WPF startup/closure to monitor startup/cancellation.
   - Retain an explicit stdin-protocol mode solely for compatibility/testing.
2. **Collector and packaging integration**
   - Add an explicit, cross-platform `session_probe.py --watch` mode.
   - Version collector snapshots without changing `Scanner.snapshot()` semantics.
   - Add a normal C# project and an explicit build command producing a ready-to-run
     application plus its Python collector files.
   - Remove runtime compilation and Python orchestration from normal launch.
   - Update the installer, desktop shortcut, compatibility launcher, and docs.
3. **Regression and integration gates**
   - Preserve all scanner/reducer behavior with the existing Python regressions.
   - Transfer host lifecycle/configuration/aggregation coverage to C# tests.
   - Keep real WPF interaction coverage for layouts, sorting, taskbar badges,
     Forget, and shared-PID sessions.
   - Exercise native and WSL collection, malformed protocol handling, partial
     source failures, cancellation, and repeated normal window closure.
4. **Local deployment**
   - Replace the installed gadget only after integration checks pass.
   - Preserve `gadget.json`, `gadget-ui.json`, `gadget-forgotten.json`, and owner
     caches.
   - Verify the desktop shortcut targets the executable and that startup and
     shutdown work through that launch path.

## Contracts

- Collector output: newline-delimited JSON objects containing
  `protocol_version: 1`, `sessions`, and `errors`.
- Session fields remain `id`, `pid`, `title`, `cwd`, `status`, and
  `activity_revision`; the host supplies the source identity.
- Preserve `Loading`, `Unknown`, `Needs input`, `In progress`, and `Done` wire
  values. Parse them into a validated enum internally.
- `activity_revision` remains nullable. Preserve exact strings: changing their
  canonical form would alter persisted Forget behavior.
- Closing a collector's stdin requests termination. Shutdown must be bounded,
  including a child that never reads its bootstrap.
- `--python <path>` selects the Windows interpreter when installed through the
  shortcut. Launches without it must either discover a supported interpreter or
  report an actionable error.
- `--collector-stdin` selects the existing UI-only snapshot protocol for tests;
  the default executable mode runs the C# monitor.

## Parallel ownership

- **Host agent:** C# contracts, monitoring services, WPF integration, and C# host
  regression tests.
- **Integration agent:** Python watch/compatibility entry points, project/build
  files, launcher/installer, Python integration tests, and user documentation.
- Agents work in separate worktrees. Neither changes the other's file domain.
  Integration occurs in the main working tree after both report their changes.

## Acceptance and non-goals

- No lost same-process sessions or regressions in clock-safe ownership.
- No change to completion, unread, or Forget/resurfacing semantics.
- No collector launch after cancellation; no orphan process after normal close.
- A failed source does not hide healthy sources or leave stale success states.
- Genuine configuration, protocol, process, and I/O failures remain visible.
- Normal window closure does not show the previous invalid-argument dialog.
- Build artifacts and real session data are not committed.
- No full Python rewrite, UI redesign, automatic startup, framework upgrade, or
  commit/push is included in this request.

## Phase-one outcome

Implemented and installed locally:

- `SessionContracts.cs` defines validated collector snapshots, configuration,
  session statuses, and source aggregation.
- `GadgetHost.cs` owns native/WSL collectors and application shutdown; the WPF
  window no longer requires a Python monitoring parent.
- `session_probe.py --watch` provides versioned snapshots and stdin-EOF shutdown
  on Windows and Linux. The shared discovery and reducer logic remains Python.
- `CopilotSessions.csproj` and `build-gadget.ps1` produce the prebuilt application.
  The installer and desktop shortcut launch that executable directly.

The Python regression suites, native C# host/WPF suite, actual Windows/Ubuntu
collection, local build/installer, desktop shortcut, and repeated normal window
closure were exercised. Live discovery retained the current session without
duplication, and normal closure left no collector children behind. Existing
configuration, layout, and Forget preference files were preserved.

Integration corrections include UTF-8 BOM tolerance at the WSL bootstrap
boundary, preservation of actionable collector errors after exit, compatible
WSL argument quoting, and PowerShell-to-Python installer quoting. Native tests
use Windows-local temporary storage for persistence operations rather than WSL
UNC storage.

Removing Windows Python or migrating the shared collector core remains outside
this phase.
