import { randomUUID } from "node:crypto";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { performance } from "node:perf_hooks";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const GRACE_MS = 10_000;
const ACTIVITY_TYPES = ["assistant.intent", "tool.execution_progress", "tool.execution_start"];
const SUMMARY_HISTORY_LIMIT = 32;
const SUMMARY_CACHE_LIMIT = 256;

function activityText(value) {
    return value.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, " ")
        .replace(/\s+/g, " ").trim().slice(0, 240).replace(/[\ud800-\udbff]$/, "");
}

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
    let latestActivityTime = -Infinity;
    let latestToolCall = null;
    let lastSummaryLookup = null;
    const toolSummaries = new Map();

    function suppliedSummary(toolCallId, toolName) {
        const cached = toolSummaries.get(toolCallId);
        return cached && cached.name === toolName ? cached.summary : undefined;
    }

    function updateActivity(event, history = false) {
        if (event.agentId && event.agentId !== sessionId) return;
        if (!ACTIVITY_TYPES.includes(event.type) && event.type !== "assistant.message") return;
        const timestamp = typeof event.timestamp === "string" ? Date.parse(event.timestamp) : NaN;
        if (history && !Number.isFinite(timestamp)) throw new Error("Invalid activity timestamp.");
        const time = Number.isFinite(timestamp) ? timestamp : now();
        if (event.type === "assistant.message") {
            const requests = event.data?.toolRequests;
            if (requests === undefined) return;
            if (!Array.isArray(requests) || requests.some(request =>
                !request || typeof request.toolCallId !== "string" || !request.toolCallId
                || typeof request.name !== "string" || !request.name
                || (request.intentionSummary != null && typeof request.intentionSummary !== "string"))) {
                throw new Error("Invalid tool summaries.");
            }
            for (const request of requests) {
                const summary = activityText(request.intentionSummary ?? "");
                if (!summary) continue;
                toolSummaries.delete(request.toolCallId);
                toolSummaries.set(request.toolCallId, { name: request.name, summary });
                if (toolSummaries.size > SUMMARY_CACHE_LIMIT)
                    toolSummaries.delete(toolSummaries.keys().next().value);
            }
            // Metadata can arrive after execution starts, but must not replace later progress/intent.
            if (latestToolCall) {
                const summary = suppliedSummary(latestToolCall.id, latestToolCall.name);
                if (summary) latestActivity = summary;
            }
            return;
        }
        let activity;
        if (event.type === "assistant.intent") {
            if (typeof event.data?.intent !== "string") throw new Error("Invalid assistant intent.");
            activity = event.data.intent;
        } else if (event.type === "tool.execution_progress") {
            if (typeof event.data?.progressMessage !== "string") throw new Error("Invalid tool progress.");
            activity = event.data.progressMessage;
        } else if (event.type === "tool.execution_start") {
            const summary = suppliedSummary(event.data?.toolCallId, event.data?.toolName);
            const description = event.data?.arguments?.description;
            if (summary) activity = summary;
            else if (typeof description === "string" && description.trim()) activity = description;
            else if (typeof event.data?.toolName === "string") activity = `Using ${event.data.toolName}`;
            else throw new Error("Invalid tool start.");
        }
        if (activity === undefined) return;
        // A persisted tool start must not overwrite a newer live intent/progress event.
        if (time < latestActivityTime) return;
        latestActivityTime = time;
        latestActivity = activityText(activity);
        latestToolCall = event.type === "tool.execution_start"
            ? { id: event.data.toolCallId, name: event.data.toolName } : null;
    }

    async function readHistory(types, max) {
        const history = await session.rpc.eventLog.read({
            direction: "backward", max, types,
            agentScope: "primary", includeEphemeral: false,
        });
        if (!history || !Array.isArray(history.events) || history.events.length > max
                || history.cursorStatus !== "ok" || typeof history.cursor !== "string"
                || typeof history.hasMore !== "boolean"
                || history.events.some(event => !event || !types.includes(event.type))) {
            throw new Error("Invalid SDK activity history.");
        }
        return history.events;
    }

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
            let failureState = "task_query_failed";
            try {
                let observed;
                do {
                    observed = revision;
                    failureState = "task_query_failed";
                    count = pendingShells(await session.rpc.tasks.list());
                    if (closed) return;
                    failureState = "activity_query_failed";
                    const [activity] = await readHistory(ACTIVITY_TYPES, 1);
                    let messages;
                    const callId = activity?.data?.toolCallId;
                    if (!closed && observed === revision && activity?.type === "tool.execution_start"
                            && (!activity.agentId || activity.agentId === sessionId)
                            && Date.parse(activity.timestamp) >= latestActivityTime
                            && typeof callId === "string" && callId && callId !== lastSummaryLookup
                            && !suppliedSummary(callId, activity.data.toolName)) {
                        messages = await readHistory(["assistant.message"], SUMMARY_HISTORY_LIMIT);
                    }
                    if (!closed && observed === revision) {
                        if (messages) {
                            for (const message of messages) updateActivity(message, true);
                            lastSummaryLookup = callId;
                        }
                        if (activity) updateActivity(activity, true);
                    }
                } while (!closed && observed !== revision);
            } catch {
                if (!closed) {
                    if (lastError !== failureState) {
                        report(failureState === "activity_query_failed"
                            ? "Gadget SDK bridge: activity query failed or returned an unsupported schema."
                            : "Gadget SDK bridge: task query failed or returned an unsupported schema.");
                    }
                    lastError = failureState;
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
        try { updateActivity(event); }
        catch {
            report("Gadget SDK bridge: invalid activity event.");
            return;
        }
        if (event.type === "assistant.turn_start" && (!event.agentId || event.agentId === sessionId)) {
            rootGeneration++;
            rootActive = true;
            settleUntil = 0;
        }
        if (event.type === "session.idle" && (!event.agentId || event.agentId === sessionId)) {
            rootActive = false;
        }
        if (["session.background_tasks_changed", "system.notification",
             "tool.execution_start", "tool.execution_progress", "tool.execution_complete",
             "assistant.turn_start", "session.idle", "assistant.intent", "assistant.message"].includes(event.type)) {
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
