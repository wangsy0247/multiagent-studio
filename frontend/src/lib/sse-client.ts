/**
 * SSE 客户端 — 连接管理、自动重连、事件解析
 */

import { SSEEvent } from "./types";

type EventCallback = (event: SSEEvent) => void;
type StatusCallback = (status: "connecting" | "connected" | "disconnected" | "error") => void;

interface SSEClientOptions {
  onEvent: EventCallback;
  onStatus?: StatusCallback;
  maxReconnectAttempts?: number;
  baseDelay?: number;
}

// App 服务直连 URL（SSE 流式响应必须直连，Next.js rewrites 代理会缓冲响应）
const APP_API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";

export class SSEClient {
  private abortController: AbortController | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts: number;
  private baseDelay: number;
  private onEvent: EventCallback;
  private onStatus?: StatusCallback;
  private isStopped = false;
  private reader: ReadableStreamDefaultReader<Uint8Array> | null = null;

  constructor(options: SSEClientOptions) {
    this.onEvent = options.onEvent;
    this.onStatus = options.onStatus;
    this.maxReconnectAttempts = options.maxReconnectAttempts ?? 5;
    this.baseDelay = options.baseDelay ?? 1000;
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
      this.reconnectAttempts = 0;
      this.reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (!this.isStopped) {
        const { done, value } = await this.reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const event: SSEEvent = JSON.parse(line.slice(6));
              this.onEvent(event);

              // 收到 event 后停止
              if (event.type === "finished" || event.type === "error") {
                this.onStatus?.("disconnected");
                return;
              }
            } catch {
              // 忽略 JSON 解析错误
            }
          }
        }
      }
    } catch (err: any) {
      if (err.name === "AbortError") return;
      this.onStatus?.("error");
      this.tryReconnect(url, body);
    }
  }

  private async tryReconnect(url: string, body: object) {
    if (this.isStopped || this.reconnectAttempts >= this.maxReconnectAttempts) {
      this.onStatus?.("disconnected");
      return;
    }

    const delay = this.baseDelay * Math.pow(2, this.reconnectAttempts);
    this.reconnectAttempts++;

    await new Promise((r) => setTimeout(r, delay));
    if (!this.isStopped) {
      await this.connect(url, body);
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
