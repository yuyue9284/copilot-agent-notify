#!/usr/bin/env node
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { createBridge } from "../extensions/gadget-status/bridge.mjs";

const { values } = parseArgs({
    options: { sdk: { type: "string" }, cli: { type: "string" } },
});
if (!values.sdk || !values.cli || !isAbsolute(values.sdk) || !isAbsolute(values.cli)) {
    throw new Error("Usage: node scripts/sdk_status_poc.mjs --sdk ABSOLUTE_SDK_INDEX_JS --cli ABSOLUTE_CLI_PATH");
}

async function bounded(promise, description, milliseconds = 30000) {
    let timer;
    try {
        return await Promise.race([
            promise,
            new Promise((_, reject) => {
                timer = setTimeout(() => reject(new Error(`Timed out: ${description}`)), milliseconds);
            }),
        ]);
    } finally {
        clearTimeout(timer);
    }
}

async function until(predicate, description, milliseconds = 15000) {
    const end = Date.now() + milliseconds;
    do {
        if (await bounded(predicate(), description, Math.max(1, end - Date.now()))) return;
        await delay(100);
    } while (Date.now() < end);
    throw new Error(`Not observed: ${description}`);
}

const { CopilotClient, RuntimeConnection } = await import(pathToFileURL(values.sdk).href);
const directory = await mkdtemp(join(tmpdir(), "copilot-status-poc-"));
const home = join(directory, "home");
const workspace = join(directory, "workspace");
await mkdir(home);
await mkdir(workspace);
const env = {};
for (const key of ["PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"]) {
    if (process.env[key]) env[key] = process.env[key];
}
Object.assign(env, {
    HOME: home, USERPROFILE: home, COPILOT_HOME: join(home, ".copilot"),
    XDG_CONFIG_HOME: join(home, ".config"), XDG_CACHE_HOME: join(home, ".cache"),
    LOCALAPPDATA: join(home, "AppData", "Local"), APPDATA: join(home, "AppData", "Roaming"),
    TMPDIR: directory, TMP: directory, TEMP: directory,
    COPILOT_OFFLINE: "true", COPILOT_PROVIDER_BASE_URL: "http://127.0.0.1:9",
    COPILOT_MODEL: "synthetic-offline-model",
    COPILOT_AUTO_UPDATE: "false",
});

const isScript = values.cli.endsWith(".js");
const client = new CopilotClient({
    connection: RuntimeConnection.forStdio({
        path: isScript ? process.execPath : values.cli,
        args: [...(isScript ? [values.cli] : []), "--no-auto-update", "--no-remote-export",
               "--disable-builtin-mcps"],
    }),
    mode: "empty",
    baseDirectory: env.COPILOT_HOME,
    workingDirectory: workspace,
    env,
});
let session;
let bridge;
let snapshot;
let bridgeError;
let phase = "connect";
const events = new Map();
let rootTurns = 0;

try {
    console.log("Connecting to an isolated offline runtime (no prompts or credentials).");
    await bounded(client.start(), phase);
    const version = await client.getStatus();
    console.log(`Runtime: ${version.version}`);
    phase = "create disposable session";
    session = await bounded(client.createSession({
        workingDirectory: workspace,
        model: "synthetic-offline-model",
        provider: { type: "openai", baseUrl: "http://127.0.0.1:9" },
        availableTools: ["bash", "powershell", "read_bash", "stop_bash"],
        onPermissionRequest: request => request.kind === "shell"
            ? { kind: "approve-once" } : { kind: "reject" },
    }), phase);
    session.on(event => {
        if (["session.background_tasks_changed", "tool.execution_start",
             "tool.execution_complete", "assistant.turn_start", "session.idle",
             "session.error"].includes(event.type)) {
            events.set(event.type, (events.get(event.type) || 0) + 1);
        }
        if (event.type === "assistant.turn_start" && !event.agentId) rootTurns++;
    });
    bridge = createBridge(session, {
        sessionId: session.sessionId, ownerPid: process.pid,
        publish: async value => { snapshot = value; },
        report: () => { bridgeError = new Error("SDK bridge reported a query/publication error."); },
    });
    const sample = async () => {
        await bridge.refresh();
        if (bridgeError) throw bridgeError;
        assert.equal(snapshot.state, "ready");
    };
    phase = "initial task snapshot";
    await bounded(sample(), phase);
    assert.equal(snapshot.pending_shells, 0);
    console.log("PASS: initial snapshot has no pending shells.");

    phase = "start background shell";
    const toolName = process.platform === "win32" ? "powershell" : "bash";
    const command = process.platform === "win32" ? "Start-Sleep -Seconds 4" : "sleep 4";
    const toolResult = await bounded(session.rpc.tools.execute({
        name: toolName,
        arguments: { command, description: "Synthetic SDK status check", mode: "async" },
    }), phase);
    assert.notEqual(toolResult.resultType, "failure", toolResult.error || "Synthetic tool failed.");
    await until(async () => {
        await sample();
        return snapshot.pending_shells > 0;
    }, "running shell");
    const shells = (await session.rpc.tasks.list()).tasks.filter(task => task.type === "shell");
    assert.ok(shells.some(task => task.status === "running"));
    console.log("PASS: structured task list and bridge report a running shell.");

    phase = "observe completion";
    await until(async () => {
        await sample();
        return snapshot.pending_shells === 0;
    }, "shell completion");
    assert.ok((await session.rpc.tasks.list()).tasks.some(
        task => task.type === "shell" && task.status === "completed"));
    assert.ok(snapshot.settling || rootTurns > 0,
        "Completion must settle or hand off to a resumed root.");
    console.log(snapshot.settling
        ? "PASS: completed task enters the bridge's handoff grace period."
        : "PASS: runtime resumed the root; bridge does not apply a redundant grace period.");
    await until(async () => {
        await sample();
        return !snapshot.settling;
    }, "settled completion");
    assert.ok((events.get("session.background_tasks_changed") || 0) > 0);
    console.log("PASS: completion handoff checked; task-change events were received.");

    const startShell = async command => {
        const before = new Set((await session.rpc.tasks.list()).tasks.map(task => task.id));
        const result = await session.rpc.tools.execute({
            name: toolName,
            arguments: { command, description: "Synthetic SDK terminal status check", mode: "async" },
        });
        assert.notEqual(result.resultType, "failure", result.error || "Synthetic tool failed.");
        let task;
        await until(async () => {
            task = (await session.rpc.tasks.list()).tasks.find(
                value => value.type === "shell" && !before.has(value.id));
            return task;
        }, "new structured shell task");
        return task;
    };
    phase = "nonzero exit";
    const failing = await startShell(process.platform === "win32"
        ? "Start-Sleep -Seconds 1; exit 7" : "sleep 1; exit 7");
    let nonzeroStatus;
    await until(async () => {
        const task = (await session.rpc.tasks.list()).tasks.find(task => task.id === failing.id);
        nonzeroStatus = task?.status;
        return ["completed", "failed", "cancelled"].includes(nonzeroStatus);
    }, "nonzero shell terminal status");
    assert.notEqual(nonzeroStatus, "cancelled", "Nonzero command was unexpectedly cancelled.");
    await sample();
    assert.equal(snapshot.pending_shells, 0);
    console.log(`OBSERVED: exit-7 shell has task status '${nonzeroStatus}' and is no longer pending.`);

    phase = "cancellation";
    const cancellable = await startShell(process.platform === "win32"
        ? "Start-Sleep -Seconds 20" : "sleep 20");
    assert.equal(cancellable.status, "running");
    await sample();
    assert.equal(snapshot.pending_shells, 1);
    assert.equal((await session.rpc.tasks.cancel({ id: cancellable.id })).cancelled, true);
    await until(async () => {
        const task = (await session.rpc.tasks.list()).tasks.find(task => task.id === cancellable.id);
        return task?.status === "cancelled";
    }, "cancelled shell status");
    await sample();
    assert.equal(snapshot.pending_shells, 0);
    console.log("PASS: cancellation is reported as cancelled and stops counting as pending.");
    console.log(JSON.stringify({ result: "pass", events: Object.fromEntries(events) }));
} catch (error) {
    console.error(`POC failed during ${phase}: ${error.message}`);
    process.exitCode = 1;
} finally {
    if (bridge) {
        try { await bounded(bridge.stop(), "bridge shutdown", 5000); }
        catch (error) {
            console.error(`POC cleanup: ${error.message}`);
            process.exitCode = 1;
        }
    }
    try {
        const errors = await bounded(client.stop(), "runtime shutdown", 10000);
        if (errors.length) throw new Error("Runtime reported cleanup errors.");
    } catch (error) {
        console.error(`POC cleanup: ${error.message}`);
        await client.forceStop();
        process.exitCode = 1;
    }
    // Only this invocation's uniquely created fixture directory is removed.
    assert.equal(resolve(directory), directory);
    await rm(directory, { recursive: true });
}
