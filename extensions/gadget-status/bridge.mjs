import { randomUUID } from "node:crypto";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { performance } from "node:perf_hooks";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const GRACE_MS = 10_000;

export function snapshotPath(sessionId, home = process.env.COPILOT_HOME || join(homedir(), ".copilot")) {
    if (typeof sessionId !== "string" || !/^[a-zA-Z0-9-]+$/.test(sessionId)) {
        throw new Error("Invalid SDK bridge session identity.");
    }
    return join(home, "session-state", sessionId, "gadget-sdk.json");
}

export async function writeSnapshot(path, snapshot) {
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    const temporary = `${path}.${randomUUID()}.tmp`;
    try {
        await writeFile(temporary, JSON.stringify(snapshot) + "\n", { flag: "wx", mode: 0o600 });
        await rename(temporary, path);
    } finally {
        await rm(temporary, { force: true });
    }
}

export function pendingShells(result) {
    if (!result || !Array.isArray(result.tasks)) {
        throw new Error("Invalid SDK task list.");
    }
    let count = 0;
    const ids = new Set();
    for (const task of result.tasks) {
        if (!task || typeof task.type !== "string") {
            throw new Error("Invalid SDK task entry.");
        }
        // Other task kinds remain the transcript reducer's responsibility.
        if (task.type !== "shell") continue;
        if (typeof task.id !== "string" || !task.id || ids.has(task.id)) {
            throw new Error("Invalid SDK shell identity.");
        }
        ids.add(task.id);
        if (task.status === "running" || task.status === "idle") count++;
        else if (!TERMINAL.has(task.status)) throw new Error("Unknown SDK shell status.");
    }
    return count;
}

export function createBridge(session, {
    sessionId,
    ownerPid,
    publish,
    now = Date.now,
    monotonic = () => performance.now(),
    report = message => console.error(message),
}) {
    let pending = null;
    let settleUntil = 0;
    let rootGeneration = 0;
    let rootActive = false;
    let inFlight = null;
    let writing = null;
    let refreshTimer = null;
    let revision = 0;
    let closed = false;
    let lastError = null;
    let latestActivity = "";

    async function emit(state, count = 0, settling = false) {
        writing = publish({
            protocol_version: 1,
            session_id: sessionId,
            owner_pid: ownerPid,
            updated_at: now() / 1000,
            state,
            pending_shells: count,
            settling,
            latest_activity: state === "ready" ? latestActivity : "",
        });
        await writing;
    }

    function refresh() {
        clearTimeout(refreshTimer);
        refreshTimer = null;
        if (closed) return Promise.resolve();
        if (inFlight) return inFlight;
        inFlight = (async () => {
            const generation = rootGeneration;
            let count;
            try {
                let observed;
                do {
                    observed = revision;
                    count = pendingShells(await session.rpc.tasks.list());
                } while (!closed && observed !== revision);
            } catch {
                if (!closed) {
                    if (lastError !== "task_query_failed") {
                        report("Gadget SDK bridge: task query failed or returned an unsupported schema.");
                    }
                    lastError = "task_query_failed";
                    await emit(lastError);
                }
                return;
            }
            if (closed) return;
            if (count > 0) settleUntil = 0;
            else if (pending > 0 && !rootActive && generation === rootGeneration) {
                settleUntil = monotonic() + GRACE_MS;
            }
            pending = count;
            lastError = null;
            await emit("ready", count, monotonic() < settleUntil);
        })().finally(() => { inFlight = null; });
        return inFlight;
    }

    const unsubscribe = session.on(event => {
        const root = !event.agentId || event.agentId === sessionId;
        let activity;
        if (root && event.type === "assistant.intent") {
            if (typeof event.data?.intent !== "string") {
                report("Gadget SDK bridge: invalid assistant intent.");
                return;
            }
            activity = event.data.intent;
        } else if (root && event.type === "tool.execution_progress") {
            if (typeof event.data?.progressMessage !== "string") {
                report("Gadget SDK bridge: invalid tool progress.");
                return;
            }
            activity = event.data.progressMessage;
        } else if (root && event.type === "tool.execution_start") {
            const description = event.data?.arguments?.description;
            if (typeof description === "string" && description.trim()) {
                activity = description;
            } else if (typeof event.data?.toolName === "string") {
                activity = `Using ${event.data.toolName}`;
            }
        }
        if (activity !== undefined) {
            latestActivity = activity.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, " ")
                .replace(/\s+/g, " ").trim().slice(0, 240).replace(/[\ud800-\udbff]$/, "");
        }
        if (event.type === "assistant.turn_start" && (!event.agentId || event.agentId === sessionId)) {
            latestActivity = "";
            rootGeneration++;
            rootActive = true;
            settleUntil = 0;
        }
        if (event.type === "session.idle" && (!event.agentId || event.agentId === sessionId)) {
            rootActive = false;
        }
        if (["session.background_tasks_changed", "system.notification",
             "tool.execution_start", "tool.execution_progress", "tool.execution_complete",
             "assistant.turn_start", "session.idle", "assistant.intent"].includes(event.type)) {
            revision++;
            if (!refreshTimer) {
                refreshTimer = setTimeout(() => {
                    refresh().catch(() => report("Gadget SDK bridge: could not publish local status."));
                }, 50);
            }
        }
    });

    return {
        refresh,
        async stop() {
            closed = true;
            clearTimeout(refreshTimer);
            unsubscribe();
            // A hung RPC must not block shutdown; its eventual result is ignored.
            if (writing) {
                try { await writing; }
                catch { report("Gadget SDK bridge: could not publish local status."); }
            }
            await emit("stopped");
        },
    };
}
