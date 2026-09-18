import { createBridge, snapshotPath, writeSnapshot } from "./bridge.mjs";

// Only load this entry point through Copilot's extension host.
const sessionId = process.env.SESSION_ID;
const path = snapshotPath(sessionId);
const publish = snapshot => writeSnapshot(path, snapshot);
const unavailable = state => publish({
    protocol_version: 1, session_id: sessionId, owner_pid: process.ppid,
    updated_at: Date.now() / 1000, state,
    pending_shells: 0, settling: false,
});

try {
    await unavailable("starting");
    const { joinSession } = await import("@github/copilot-sdk/extension");
    const session = await joinSession();
    const bridge = createBridge(session, {
        sessionId,
        ownerPid: process.ppid,
        publish,
    });
    await bridge.refresh();
    const timer = setInterval(() => {
        bridge.refresh().catch(() => console.error("Gadget SDK bridge: could not publish local status."));
    }, 5000);
    process.once("SIGTERM", () => {
        clearInterval(timer);
        bridge.stop().catch(() => console.error("Gadget SDK bridge: could not publish shutdown status."));
    });
} catch {
    console.error("Gadget SDK bridge: SDK unavailable or extension initialization failed.");
    await unavailable("initialization_failed");
    process.exitCode = 1;
}
