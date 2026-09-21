import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { createBridge, pendingShells, snapshotPath, writeSnapshot } from "../extensions/gadget-status/bridge.mjs";

const shell = (status = "running", id = "synthetic-shell") => ({
    type: "shell", id, status, command: "PRIVATE COMMAND", description: "PRIVATE DESCRIPTION",
});

const toolMessage = (requests, timestamp = "1970-01-01T00:01:39Z") => ({
    type: "assistant.message", timestamp, data: { content: "PRIVATE ASSISTANT TEXT", toolRequests: requests },
});
const toolStart = (toolCallId, toolName = "view", timestamp = "1970-01-01T00:01:40Z") => ({
    type: "tool.execution_start", timestamp,
    data: { toolCallId, toolName, arguments: { path: "/synthetic/private/path", content: "PRIVATE FILE CONTENT" } },
});

function fixture() {
    const output = [];
    const errors = [];
    let tasks = [];
    let listener;
    let clock = 1000;
    let list = async () => ({ tasks });
    let history = [];
    let read = async params => {
        const events = history.filter(event => params.types.includes(event.type));
        return { events: events.slice(-params.max), cursor: "synthetic-cursor",
            cursorStatus: "ok", hasMore: events.length > params.max };
    };
    const reads = [];
    let unsubscribed = false;
    const bridge = createBridge({
        rpc: { tasks: { list: () => list() }, eventLog: { read: params => {
            reads.push(params);
            return read(params);
        } } },
        on: callback => {
            listener = callback;
            return () => { unsubscribed = true; };
        },
    }, {
        sessionId: "synthetic-session", ownerPid: 42,
        publish: async value => { output.push(value); },
        now: () => 100000 + clock, monotonic: () => clock,
        report: value => errors.push(value),
    });
    return {
        bridge, output, errors, reads,
        set tasks(value) { tasks = value; },
        set clock(value) { clock = value; },
        set list(value) { list = value; },
        set history(value) { history = value; },
        set read(value) { read = value; },
        event: value => listener(value),
        get last() { return output.at(-1); },
        get unsubscribed() { return unsubscribed; },
    };
}

test("only nonterminal shells count; unknown shell schemas fail explicitly", () => {
    assert.equal(pendingShells({ tasks: [
        shell(), shell("idle", "second"), shell("completed", "third"),
        shell("failed", "fourth"), shell("cancelled", "fifth"), { type: "agent" },
    ] }), 2);
    for (const result of [null, {}, { tasks: [null] }, { tasks: [shell("new-status")] },
                          { tasks: [shell(), shell()] }]) {
        assert.throws(() => pendingShells(result));
    }
});

test("query snapshots contain only allowlisted metadata and no command text", async () => {
    const f = fixture();
    f.tasks = [shell()];
    await f.bridge.refresh();
    assert.deepEqual(f.last, {
        protocol_version: 1, session_id: "synthetic-session", owner_pid: 42,
        updated_at: 101, state: "ready", pending_shells: 1, settling: false, latest_activity: "",
    });
    assert.ok(!JSON.stringify(f.output).includes("PRIVATE"));
});

test("SDK history restores and reconciles activity without any push notifications", async () => {
    const f = fixture();
    try {
        f.history = [{ type: "tool.execution_start", timestamp: "1970-01-01T00:01:40Z",
            data: { toolName: "bash", arguments: {
                description: "Checking synthetic fixtures", command: "PRIVATE COMMAND",
            } } }];
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Checking synthetic fixtures");
        assert.deepEqual(f.reads[0], {
            direction: "backward", max: 1,
            types: ["assistant.intent", "tool.execution_progress", "tool.execution_start"],
            agentScope: "primary", includeEphemeral: false,
        });
        f.history = [{ type: "tool.execution_start", timestamp: "1970-01-01T00:01:41Z",
            data: { toolName: "view", arguments: { path: "/synthetic/private/path" } } }];
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        f.event({ type: "assistant.turn_start" });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        assert.ok(!JSON.stringify(f.output).includes("PRIVATE COMMAND"));
        assert.ok(!JSON.stringify(f.output).includes("/synthetic/private/path"));
    } finally { await f.bridge.stop(); }
});

test("older history and child events cannot replace newer live activity", async () => {
    const f = fixture();
    try {
        f.event({ type: "tool.execution_progress", timestamp: "1970-01-01T00:01:42Z",
            data: { progressMessage: "Completed synthetic checks" } });
        f.history = [{ type: "tool.execution_start", timestamp: "1970-01-01T00:01:40Z",
            data: { toolName: "view" } }];
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Completed synthetic checks");
        f.history = [{ type: "assistant.intent", agentId: "synthetic-child",
            timestamp: "1970-01-01T00:01:43Z", data: { intent: "Child work" } }];
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Completed synthetic checks");
    } finally { await f.bridge.stop(); }
});

test("supplied summaries are matched to actual tool starts, not pending requests or tool titles", async () => {
    const f = fixture();
    try {
        f.event(toolMessage([
            { toolCallId: "synthetic-view", name: "view", intentionSummary: "Inspecting synthetic fixtures" },
            { toolCallId: "synthetic-create", name: "create", intentionSummary: "Writing a synthetic fixture" },
        ]));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "");
        const start = toolStart("synthetic-view");
        start.data.arguments.description = "Existing description";
        f.event(start);
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Inspecting synthetic fixtures");
        f.event(toolStart("synthetic-create", "create"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Writing a synthetic fixture");
        f.event(toolMessage([{ toolCallId: "synthetic-plain", name: "view", toolTitle: "PRIVATE TITLE" }]));
        f.event(toolStart("synthetic-plain"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        assert.ok(!JSON.stringify(f.output).includes("PRIVATE"));
        assert.ok(!JSON.stringify(f.output).includes("/synthetic/private/path"));
    } finally { await f.bridge.stop(); }
});

test("missing, null, blank and control-only summaries keep both existing fallbacks", async () => {
    const f = fixture();
    try {
        for (const [index, summary] of [undefined, null, "", " \n ", "\u202e\u0000"].entries()) {
            const id = `synthetic-${index}`;
            f.event(toolMessage([{ toolCallId: id, name: "view", intentionSummary: summary }]));
            const start = toolStart(id);
            start.data.arguments.description = "Existing tool description";
            f.event(start);
            await f.bridge.refresh();
            assert.equal(f.last.latest_activity, "Existing tool description");
            delete start.data.arguments.description;
            f.event(start);
            await f.bridge.refresh();
            assert.equal(f.last.latest_activity, "Using view");
        }
    } finally { await f.bridge.stop(); }
});

test("late summaries enrich their own latest tool start without replacing later activity", async () => {
    const f = fixture();
    try {
        f.event(toolStart("synthetic-view"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        f.event(toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Supplied summary" }]));
        await delay(100);
        assert.equal(f.last.latest_activity, "Supplied summary");
        for (const event of [
            { type: "tool.execution_progress", data: { progressMessage: "Later progress" } },
            { type: "assistant.intent", data: { intent: "Later intent" } },
            toolStart("synthetic-other", "create", "1970-01-01T00:01:42Z"),
        ]) {
            f.event(event);
            await f.bridge.refresh();
            const expected = f.last.latest_activity;
            f.event(toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Old summary" }]));
            await f.bridge.refresh();
            assert.equal(f.last.latest_activity, expected);
        }
    } finally { await f.bridge.stop(); }
});

test("child summaries and mismatched tool names cannot label a root call", async () => {
    const f = fixture();
    try {
        f.event({ ...toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Child summary" }]),
            agentId: "synthetic-child" });
        f.event(toolStart("synthetic-view"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        f.event(toolMessage([{ toolCallId: "synthetic-view", name: "create", intentionSummary: "Wrong tool" }]));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        f.event(toolStart("synthetic-view"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
    } finally { await f.bridge.stop(); }
});

test("summary text and its cache are bounded without exposing other message fields", async () => {
    const f = fixture();
    try {
        f.event(toolMessage(Array.from({ length: 300 }, (_, index) => ({
            toolCallId: `synthetic-${index}`, name: "view", intentionSummary: `Supplied ${index}`,
        }))));
        f.event(toolStart("synthetic-0"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        f.event(toolStart("synthetic-299"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Supplied 299");
        f.event(toolMessage([{ toolCallId: "synthetic-long", name: "view",
            intentionSummary: "  " + "x".repeat(239) + "\u{1f680}\u202e\n" }]));
        f.event(toolStart("synthetic-long"));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "x".repeat(239));
        assert.ok(!JSON.stringify(f.output).includes("PRIVATE"));
    } finally { await f.bridge.stop(); }
});

test("history restores supplied summaries after a final message and caches the lookup", async () => {
    const f = fixture();
    try {
        f.history = [
            toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Persisted summary" }]),
            toolStart("synthetic-view"),
            { type: "assistant.message", timestamp: "1970-01-01T00:01:41Z", data: { content: "PRIVATE FINAL TEXT" } },
        ];
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Persisted summary");
        assert.deepEqual(f.reads[1], {
            direction: "backward", max: 32, types: ["assistant.message"],
            agentScope: "primary", includeEphemeral: false,
        });
        await f.bridge.refresh();
        assert.equal(f.reads.length, 3);
        assert.equal(f.last.latest_activity, "Persisted summary");
        assert.ok(!JSON.stringify(f.output).includes("PRIVATE"));
    } finally { await f.bridge.stop(); }
});

test("history lookup is bounded, preserves the fallback on absence and accepts a later live summary", async () => {
    const f = fixture();
    try {
        f.history = [
            toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Outside bounded history" }]),
            toolStart("synthetic-view"),
            ...Array.from({ length: 32 }, () => ({
                type: "assistant.message", timestamp: "1970-01-01T00:01:41Z", data: { content: "PRIVATE FINAL TEXT" },
            })),
        ];
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        await f.bridge.refresh();
        assert.equal(f.reads.length, 3);
        f.event(toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Late summary" }]));
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Late summary");
    } finally { await f.bridge.stop(); }
});

test("summary history failures are explicit and can recover without losing the lookup", async () => {
    const f = fixture();
    const reply = events => ({ events, cursor: "", cursorStatus: "ok", hasMore: false });
    try {
        f.read = async params => {
            if (params.types.includes("assistant.message")) throw new Error("PRIVATE QUERY ERROR");
            return reply([toolStart("synthetic-view")]);
        };
        await f.bridge.refresh();
        assert.equal(f.last.state, "activity_query_failed");
        assert.ok(!JSON.stringify([f.output, f.errors]).includes("PRIVATE"));
        f.read = async params => reply(params.types.includes("assistant.message")
            ? [toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Recovered summary" }])]
            : [toolStart("synthetic-view")]);
        await f.bridge.refresh();
        assert.equal(f.last.state, "ready");
        assert.equal(f.last.latest_activity, "Recovered summary");
    } finally { await f.bridge.stop(); }
});

test("invalid summary metadata is reported rather than copied or silently accepted", async () => {
    const f = fixture();
    try {
        for (const requests of [null, {}, [null], [{ toolCallId: "synthetic-view", name: "view", intentionSummary: 42 }]]) {
            f.event(toolMessage(requests));
            assert.equal(f.errors.at(-1), "Gadget SDK bridge: invalid activity event.");
        }
        assert.equal(f.errors.length, 4);
        f.history = [toolMessage(null), toolStart("synthetic-view")];
        await f.bridge.refresh();
        assert.equal(f.last.state, "activity_query_failed");
    } finally { await f.bridge.stop(); }
});

test("live events during a summary lookup discard obsolete history", async () => {
    const f = fixture();
    const reply = events => ({ events, cursor: "", cursorStatus: "ok", hasMore: false });
    let finish;
    f.read = params => params.types.includes("assistant.message")
        ? new Promise(resolve => { finish = resolve; }) : reply([toolStart("synthetic-view")]);
    try {
        const running = f.bridge.refresh();
        await delay(0);
        assert.equal(typeof finish, "function");
        f.event({ type: "assistant.intent", timestamp: "1970-01-01T00:01:43Z", data: { intent: "Newer live work" } });
        finish(reply([toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Obsolete work" }])]));
        await running;
        assert.equal(f.last.latest_activity, "Newer live work");
        assert.equal(f.output.length, 1);
        assert.equal(f.reads.filter(params => params.types.includes("assistant.message")).length, 1);
    } finally { await f.bridge.stop(); }
});

test("shutdown ignores a hung summary lookup and its late result", async () => {
    const f = fixture();
    const reply = events => ({ events, cursor: "", cursorStatus: "ok", hasMore: false });
    let finish;
    f.read = params => params.types.includes("assistant.message")
        ? new Promise(resolve => { finish = resolve; }) : reply([toolStart("synthetic-view")]);
    const running = f.bridge.refresh();
    await delay(0);
    assert.equal(typeof finish, "function");
    await f.bridge.stop();
    finish(reply([toolMessage([{ toolCallId: "synthetic-view", name: "view", intentionSummary: "Late work" }])]));
    await running;
    assert.equal(f.last.state, "stopped");
    assert.equal(f.last.latest_activity, "");
});

test("activity history failures are redacted, hide text, and recover", async () => {
    const f = fixture();
    try {
        f.event({ type: "assistant.intent", data: { intent: "Checking fixtures" } });
        f.read = async () => { throw new Error("PRIVATE SERVER ERROR"); };
        await f.bridge.refresh();
        await f.bridge.refresh();
        assert.equal(f.last.state, "activity_query_failed");
        assert.equal(f.last.latest_activity, "");
        assert.equal(f.errors.length, 1);
        assert.ok(!JSON.stringify([f.output, f.errors]).includes("PRIVATE"));
        f.read = async () => ({ events: [], cursor: "", cursorStatus: "ok", hasMore: false });
        await f.bridge.refresh();
        assert.equal(f.last.state, "ready");
        assert.equal(f.last.latest_activity, "Checking fixtures");
        for (const result of [null, {}, { events: [], cursor: "", cursorStatus: "expired", hasMore: false },
            { events: [{ type: "assistant.intent", data: { intent: "Untimed" } }],
                cursor: "", cursorStatus: "ok", hasMore: false }]) {
            f.read = async () => result;
            await f.bridge.refresh();
            assert.equal(f.last.state, "activity_query_failed");
        }
    } finally { await f.bridge.stop(); }
});

test("shutdown ignores a late activity read without waiting for it", async () => {
    const f = fixture();
    let finish;
    f.read = () => new Promise(resolve => { finish = resolve; });
    const running = f.bridge.refresh();
    await Promise.resolve();
    assert.equal(typeof finish, "function");
    await f.bridge.stop();
    finish({ events: [], cursor: "", cursorStatus: "ok", hasMore: false });
    await running;
    assert.equal(f.last.state, "stopped");
});

test("latest activity follows root intent, is bounded, and survives assistant turns", async () => {
    const f = fixture();
    try {
        f.event({ type: "assistant.intent", data: { intent: "  Checking\nsynthetic tests\u202e  " } });
        await delay(100);
        assert.equal(f.last.latest_activity, "Checking synthetic tests");
        f.event({ type: "assistant.intent", agentId: "child", data: { intent: "Child work" } });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Checking synthetic tests");
        f.event({ type: "session.idle" });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Checking synthetic tests");
        f.event({ type: "assistant.intent", data: { intent: "x".repeat(300) } });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity.length, 240);
        f.event({ type: "assistant.intent", data: { intent: "x".repeat(239) + "\u{1f680}" } });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "x".repeat(239));
        f.list = async () => { throw new Error("synthetic"); };
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "");
        f.list = async () => ({ tasks: [] });
        f.event({ type: "assistant.turn_start" });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "x".repeat(239));
    } finally {
        await f.bridge.stop();
    }
});

test("shell completion holds status for exactly ten seconds", async () => {
    const f = fixture();
    f.tasks = [shell()];
    await f.bridge.refresh();
    f.tasks = [shell("completed")];
    await f.bridge.refresh();
    assert.equal(f.last.settling, true);
    f.clock = 10999;
    await f.bridge.refresh();
    assert.equal(f.last.settling, true);
    f.clock = 11000;
    await f.bridge.refresh();
    assert.equal(f.last.settling, false);
});

test("sessions without intents publish tool descriptions and progress, never raw commands", async () => {
    const f = fixture();
    try {
        f.event({ type: "tool.execution_start", data: {
            toolName: "bash", arguments: { description: "Checking synthetic fixtures", command: "PRIVATE COMMAND" },
        } });
        await delay(100);
        assert.equal(f.last.latest_activity, "Checking synthetic fixtures");
        f.event({ type: "tool.execution_complete" });
        f.event({ type: "assistant.turn_end" });
        f.event({ type: "assistant.turn_start" });
        await delay(100);
        assert.equal(f.last.latest_activity, "Checking synthetic fixtures");
        f.event({ type: "tool.execution_progress", data: { progressMessage: "Completed 2 of 3 checks" } });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Completed 2 of 3 checks");
        f.event({ type: "tool.execution_progress", agentId: "child",
            data: { progressMessage: "Child status" } });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Completed 2 of 3 checks");
        f.event({ type: "tool.execution_start", data: {
            toolName: "view", arguments: { path: "/synthetic/private/path" },
        } });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        f.event({ type: "assistant.turn_start" });
        f.event({ type: "session.idle" });
        await f.bridge.refresh();
        assert.equal(f.last.latest_activity, "Using view");
        assert.ok(!JSON.stringify(f.output).includes("PRIVATE COMMAND"));
        assert.ok(!JSON.stringify(f.output).includes("/synthetic/private/path"));
    } finally {
        await f.bridge.stop();
    }
});

test("historical completed tasks do not start a grace period", async () => {
    const f = fixture();
    f.tasks = [shell("completed")];
    await f.bridge.refresh();
    assert.equal(f.last.settling, false);
});

test("parent resume clears settling; child activity does not clear it", async () => {
    const f = fixture();
    f.tasks = [shell()];
    await f.bridge.refresh();
    f.tasks = [];
    await f.bridge.refresh();
    f.event({ type: "assistant.turn_start", agentId: "synthetic-child" });
    await f.bridge.refresh();
    assert.equal(f.last.settling, true);
    f.event({ type: "assistant.turn_start" });
    await f.bridge.refresh();
    assert.equal(f.last.settling, false);
});

test("active root completion is not delayed by a finishing shell", async () => {
    const f = fixture();
    f.tasks = [shell()];
    f.event({ type: "assistant.turn_start" });
    await f.bridge.refresh();
    f.tasks = [];
    await f.bridge.refresh();
    assert.equal(f.last.settling, false);
    f.event({ type: "session.idle" });
    await f.bridge.refresh();
    assert.equal(f.last.settling, false);
});

test("task-change events refresh snapshots without overlapping queries", async () => {
    const f = fixture();
    let finish;
    let calls = 0;
    f.list = () => {
        calls++;
        return new Promise(resolve => { finish = resolve; });
    };
    f.event({ type: "session.background_tasks_changed" });
    const running = f.bridge.refresh();
    f.event({ type: "tool.execution_complete" });
    assert.equal(calls, 1);
    f.list = async () => {
        calls++;
        return { tasks: [shell()] };
    };
    finish({ tasks: [shell()] });
    await running;
    assert.equal(calls, 2);
    assert.equal(f.last.pending_shells, 1);
    await f.bridge.stop();
});

test("events during a query discard obsolete idle snapshots", async () => {
    const f = fixture();
    let finish;
    f.list = () => new Promise(resolve => { finish = resolve; });
    const running = f.bridge.refresh();
    f.event({ type: "session.background_tasks_changed" });
    f.list = async () => ({ tasks: [shell()] });
    finish({ tasks: [] });
    await running;
    assert.equal(f.output.length, 1);
    assert.ok(f.output.every(value => value.pending_shells === 1));
    await f.bridge.stop();
});

test("an event burst triggers one scheduled refresh without manual polling", async () => {
    const f = fixture();
    let calls = 0;
    f.list = async () => {
        calls++;
        return { tasks: [shell()] };
    };
    try {
        for (let i = 0; i < 20; i++) f.event({ type: "session.background_tasks_changed" });
        assert.equal(calls, 0);
        await delay(100);
        assert.equal(calls, 1);
        assert.equal(f.last.pending_shells, 1);
    } finally {
        await f.bridge.stop();
    }
});

test("unsupported RPC produces explicit redacted error and can recover", async () => {
    const f = fixture();
    f.list = async () => { throw new Error("PRIVATE SERVER ERROR"); };
    await f.bridge.refresh();
    await f.bridge.refresh();
    assert.equal(f.last.state, "task_query_failed");
    assert.equal(f.errors.length, 1);
    assert.ok(!JSON.stringify([f.output, f.errors]).includes("PRIVATE"));
    f.list = async () => ({ tasks: [] });
    await f.bridge.refresh();
    assert.equal(f.last.state, "ready");
});

test("shutdown does not wait for a hung query or publish its late result", async () => {
    const f = fixture();
    let finish;
    f.list = () => new Promise(resolve => { finish = resolve; });
    const running = f.bridge.refresh();
    await f.bridge.stop();
    assert.equal(f.last.state, "stopped");
    assert.equal(f.unsubscribed, true);
    finish({ tasks: [shell()] });
    await running;
    assert.equal(f.last.state, "stopped");
});

test("atomic publisher writes the local protocol and leaves no temporary files", async () => {
    const home = await mkdtemp(join(tmpdir(), "gadget-sdk-test-"));
    try {
        const path = snapshotPath("synthetic-session", home);
        await writeSnapshot(path, { state: "ready" });
        await writeSnapshot(path, { state: "stopped" });
        assert.deepEqual(JSON.parse(await readFile(path, "utf8")), { state: "stopped" });
        assert.deepEqual(await readdir(join(home, "session-state", "synthetic-session")), ["gadget-sdk.json"]);
        assert.throws(() => snapshotPath("../outside", home));
        assert.throws(() => snapshotPath(undefined, home));
    } finally {
        await rm(home, { recursive: true });
    }
});
