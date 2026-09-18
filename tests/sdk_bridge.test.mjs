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

function fixture() {
    const output = [];
    const errors = [];
    let tasks = [];
    let listener;
    let clock = 1000;
    let list = async () => ({ tasks });
    let unsubscribed = false;
    const bridge = createBridge({
        rpc: { tasks: { list: () => list() } },
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
        bridge, output, errors,
        set tasks(value) { tasks = value; },
        set clock(value) { clock = value; },
        set list(value) { list = value; },
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
        updated_at: 101, state: "ready", pending_shells: 1, settling: false,
    });
    assert.ok(!JSON.stringify(f.output).includes("PRIVATE"));
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
