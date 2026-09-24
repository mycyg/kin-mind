/** MemoryPalace 1.0 host transport. Recall and lifecycle policy live in Python. */
import { createHash, randomUUID } from "node:crypto";
import {
  mkdirSync,
  openSync,
  closeSync,
  fsyncSync,
  readFileSync,
  renameSync,
  writeFileSync,
  unlinkSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Config } from "./config.js";
import type { FeedTodo } from "./narrow.js";

/** Put a text into the model context as a `form: 'recall'` user message. */
export type InjectFn = (text: string) => void;

/** A host that can run work from a true idle phase; dsh's `Agent` satisfies it. */
export interface MaintenanceHost {
  runMaintenance: <T>(task: (signal: AbortSignal) => Promise<T>) => Promise<T>;
}

/** One tool result as the service receives it. */
export interface ToolObservation {
  sessionId: string;
  cwd: string;
  toolName: string;
  callId: string;
  args: unknown;
  isError: boolean;
  value: unknown;
  errorMessage: string | undefined;
  contentText: string;
}

export class ServiceRuntime {
  private pending = new Map<string, Promise<void>>();
  private sessions = new Map<string, string>();
  private closed = new Set<string>();
  constructor(readonly config: Config) {}
  private root() {
    return process.env.EVENTMEM_HOME ?? join(homedir(), ".memorypalace");
  }
  private send(
    sessionId: string,
    cwd: string,
    event: string,
    payload: Record<string, unknown>,
    inject?: InjectFn,
  ): void {
    this.sessions.set(sessionId, cwd);
    const normalized = {
      event,
      payload: {
        ...payload,
        session_id: sessionId,
        cwd,
        host: "deepseek-harness",
      },
    };
    const raw = JSON.stringify(normalized);
    const directory = join(this.root(), "host-spool");
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const path = join(
      directory,
      createHash("sha256").update(raw).digest("hex") + ".json",
    );
    const temporary = path + "." + randomUUID() + ".tmp";
    const descriptor = openSync(temporary, "wx", 0o600);
    try {
      writeFileSync(descriptor, raw);
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
    renameSync(temporary, path);
    const operation = (this.pending.get(sessionId) ?? Promise.resolve()).then(
      async () => {
        try {
          const token = readFileSync(
            join(this.root(), "local-token"),
            "utf8",
          ).trim();
          const response = await fetch(
            (process.env.EVENTMEM_URL ?? "http://127.0.0.1:8319") +
              "/v1/host/events",
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Authorization: `Bearer ${token}`,
              },
              body: raw,
              signal: AbortSignal.timeout(7000),
            },
          );
          if (!response.ok)
            throw new Error(`MemoryPalace HTTP ${response.status}`);
          const result = (await response.json()) as { text?: string };
          try {
            unlinkSync(path);
          } catch {
            /* worker may already have replayed receipt */
          }
          if (result.text && inject && !this.closed.has(sessionId))
            inject(result.text);
        } catch {
          // The private spool remains for service replay. A failed call never
          // acknowledges receipt or computes an alternate local recall ranking.
        }
      },
    );
    this.pending.set(sessionId, operation);
  }
  sessionStart(
    sessionId: string,
    cwd: string,
    inject: InjectFn,
    source?: string,
  ): void {
    this.closed.delete(sessionId);
    this.send(
      sessionId,
      cwd,
      source === "compact" ? "compact" : "start",
      {},
      this.config.injectWorkingSet ? inject : undefined,
    );
  }
  async preAction(
    sessionId: string,
    cwd: string,
    name: string,
    args: unknown,
    inject: InjectFn,
  ): Promise<void> {
    this.send(
      sessionId,
      cwd,
      "pre_action",
      { tool_name: name, tool_input: args },
      inject,
    );
    await this.flush(sessionId);
  }
  message(
    sessionId: string,
    cwd: string,
    role: string,
    text: string,
    seq: number,
  ): void {
    if (this.config.writeFeed && text)
      this.send(sessionId, cwd, "message", {
        role,
        text,
        command_id: `message:${seq}`,
        extract: role === "user",
      });
  }
  toolResult(observation: ToolObservation, inject: InjectFn): void {
    if (!this.config.writeFeed) return;
    this.send(
      observation.sessionId,
      observation.cwd,
      "tool",
      { ...observation },
      inject,
    );
  }
  todoWrite(
    sessionId: string,
    cwd: string,
    todos: readonly FeedTodo[],
    inject: InjectFn,
  ): void {
    if (!this.config.writeFeed) return;
    this.send(
      sessionId,
      cwd,
      "tool",
      {
        tool_name: "TodoWrite",
        tool_input: { todos },
        command_id: createHash("sha256")
          .update(JSON.stringify(todos))
          .digest("hex"),
      },
      inject,
    );
  }
  boundary(
    sessionId: string,
    cwd: string,
    kind: string,
    data: Record<string, unknown>,
  ): void {
    if (this.config.writeFeed)
      this.send(sessionId, cwd, "boundary", {
        data: { kind, ...data },
        command_id: `${kind}:${String(data.seq ?? randomUUID())}`,
      });
  }
  onIdle(_sessionId: string, _cwd: string, _host: MaintenanceHost): void {
    /* durable server worker owns maintenance */
  }
  onBusy(_sessionId: string): void {
    /* interactions automatically throttle server work */
  }
  async flush(sessionId: string): Promise<void> {
    await this.pending.get(sessionId);
  }
  async flushAll(): Promise<void> {
    for (const sessionId of [...this.sessions.keys()]) this.drop(sessionId);
    await Promise.all(this.pending.values());
  }
  drop(sessionId: string): void {
    const cwd = this.sessions.get(sessionId);
    if (cwd)
      this.send(sessionId, cwd, "end", { command_id: `end:${sessionId}` });
    this.closed.add(sessionId);
    this.sessions.delete(sessionId);
  }
}
