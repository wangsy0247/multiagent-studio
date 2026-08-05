/**
 * SSE 客户端 — 连接管理、事件解析 (含标准 `id:` 行 → 事件序号)
 *
 * 注意: 不再有自动重连。旧的重连会原样重发 POST body, 对 execute/respond
 * 意味着重复执行 agent — 断线续传必须走独立的 resume 端点 (见 global-sse.ts)。
 */

import { SSEEvent } from "./types";

type EventCallback = (event: SSEEvent, eventId?: number) => void;
type StatusCallback = (status: "connecting" | "connected" | "disconnected" | "error") => void;

interface SSEClientOptions {
  onEvent: EventCallback;
  onStatus?: StatusCallback;
}

// App 服务直连 URL（SSE 流式响应必须直连，Next.js rewrites 代理会缓冲响应）
const APP_API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export class SSEClient {
  private abortController: AbortController | null = null;
  private onEvent: EventCallback;
  private onStatus?: StatusCallback;
  private isStopped = false;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;

  constructor(options: SSEClientOptions) {
    this.onEvent = options.onEvent;
    this.onStatus = options.onStatus;
  }

  async connect(url: string, body: object) {
    this.isStopped = false;
    this.abortController = new AbortController();

    // SSE 流式请求直连 App 服务 (绕过 Next.js proxy，避免缓冲)
    const fullUrl = url.startsWith("/api/") ? `${APP_API_BASE}${url}` : url;

    try {
      this.onStatus?.("connecting");

      const response = await fetch(fullUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${this.getToken()}`,
        },
        body: JSON.stringify(body),
        signal: this.abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      this.onStatus?.("connected");
      this.reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      // 当前事件块的序号 (SSE 帧格式: "id: N\ndata: {...}\n\n")
      let pendingId: number | undefined;

      while (!this.isStopped) {
        const { done, value } = await this.reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("id: ")) {
            const n = parseInt(line.slice(4), 10);
            pendingId = Number.isNaN(n) ? undefined : n;
            continue;
          }
          if (line === "" || line === "\r") {
            // 事件块边界 — 未消费的 id 不跨块保留
            pendingId = undefined;
            continue;
          }
          if (line.startsWith("data: ")) {
            try {
              const event: SSEEvent = JSON.parse(line.slice(6));
              const id = pendingId;
              pendingId = undefined;
              this.onEvent(event, id);

              // 收到终态/resync 事件后停止
              if (
                event.type === "finished" ||
                event.type === "error" ||
                event.type === "resync"
              ) {
                this.onStatus?.("disconnected");
                return;
              }
            } catch {
              // 忽略 JSON 解析错误
            }
          }
        }
      }

      // Stream ended without a terminal finished/error event (e.g. team
      // clarification pause, team_error) — notify so the caller can clean up
      // the connection, otherwise a stale connection blocks the next send.
      // Skipped when stop() was called (it already notified).
      if (!this.isStopped) {
        this.onStatus?.("disconnected");
      }
    } catch (err: any) {
      if (err.name === "AbortError") return;
      this.onStatus?.("error");
      // 不自动重连: 重发 POST body 会重复执行 agent。
      // 断线恢复由 global-sse 的 resume 端点路径负责 (带上层决策)。
      this.onStatus?.("disconnected");
    }
  }

  stop() {
    this.isStopped = true;
    this.reader?.cancel();
    this.abortController?.abort();
    this.onStatus?.("disconnected");
  }

  private getToken(): string {
    if (typeof window !== "undefined") {
      const stored = localStorage.getItem("auth-storage");
      if (stored) {
        try {
          const { state } = JSON.parse(stored);
          return state?.accessToken || "";
        } catch {}
      }
    }
    return "";
  }
}
