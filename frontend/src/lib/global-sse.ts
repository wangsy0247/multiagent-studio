/**
 * 全局 SSE 连接管理器 — 连接生命周期独立于组件挂载/卸载。
 *
 * 解决的问题:
 *   - 切换会话时 ChatPanel unmount → 旧的 SSEClient.stop() 导致 agent 停止
 *   - 新方案: 连接在全局 Map 中管理, 组件只做订阅/取消订阅
 *
 * 断线续传 (Phase 3, 对齐 DeerFlow Last-Event-ID 思路):
 *   - 每条事件携带后端分配的序号 (SSE `id:` 行), 收到后记录到
 *     sessionStorage (`sse:lastEvent:{threadId}`), 终态时清除;
 *   - 页面刷新后若 thread 仍在运行, 用 resume() 挂接
 *     `POST /api/execute/{threadId}/resume` 补发缺失事件后续流;
 *     不可续传 (resync 事件 / 网络错误 / 流中断) 时回调 onFallback,
 *     由调用方回退到状态轮询。
 *
 * 使用方式:
 *   globalSSEManager.connect(threadId, url, body)
 *   const unsub = globalSSEManager.subscribe(threadId, callback)
 *   // 组件卸载时: unsub()
 */

import { SSEClient } from "./sse-client";
import type { SSEEvent } from "./types";

type EventCallback = (event: SSEEvent) => void;

// 终态事件 — 收到后清除 lastEventId 并释放连接
const TERMINAL_TYPES = new Set(["finished", "error", "team_error", "team_end"]);

const lastEventKey = (threadId: string) => `sse:lastEvent:${threadId}`;

class GlobalSSEManager {
  private connections: Map<string, SSEClient> = new Map();
  private listeners: Map<string, Set<EventCallback>> = new Map();
  /** 用户主动停止的连接 — 其 disconnected 状态不触发 resume 回退 */
  private manualStops: Set<string> = new Set();

  /** 启动新 SSE 连接 (如果该 thread 已有连接则跳过) */
  connect(threadId: string, url: string, body: object): void {
    if (this.connections.has(threadId)) return;
    // 新运行序号从 1 重置, 旧 lastEventId 已失效
    this.clearLastEventId(threadId);

    const sse = new SSEClient({
      onEvent: (event: SSEEvent, eventId?: number) => {
        if (eventId !== undefined) this.saveLastEventId(threadId, eventId);
        this.dispatch(threadId, event);
        // 收到终态事件后自动清理连接
        if (TERMINAL_TYPES.has(event.type)) {
          this.clearLastEventId(threadId);
          this.connections.delete(threadId);
        }
      },
      onStatus: (status) => {
        // 读循环异常结束 (网络断开/服务重启, 无终态事件) 时也要清理连接,
        // 否则残留连接会让后续 connect() 静默跳过, 消息被吞掉
        if (status === "disconnected" || status === "error") {
          this.connections.delete(threadId);
        }
      },
    });

    this.track(threadId, sse);
    sse.connect(url, body).finally(() => this.untrack(threadId, sse));
  }

  /**
   * 断线续传 — 挂接 resume 端点, 从 lastEventId 之后补发并续流。
   * 不可续传 (resync) 或连接中断且未见终态时回调 onFallback (回退轮询)。
   */
  resume(threadId: string, lastEventId: number, onFallback: () => void): void {
    if (this.connections.has(threadId)) return;

    let terminalSeen = false;
    let fallbackFired = false;
    const fireFallback = () => {
      if (fallbackFired) return;
      fallbackFired = true;
      onFallback();
    };

    const sse = new SSEClient({
      onEvent: (event: SSEEvent, eventId?: number) => {
        if (event.type === "resync") {
          // 后端明确告知不可续传 (not_running / gap) → 回退轮询
          this.clearLastEventId(threadId);
          this.connections.delete(threadId);
          fireFallback();
          return;
        }
        if (eventId !== undefined) this.saveLastEventId(threadId, eventId);
        this.dispatch(threadId, event);
        if (TERMINAL_TYPES.has(event.type)) {
          terminalSeen = true;
          this.clearLastEventId(threadId);
          this.connections.delete(threadId);
        }
      },
      onStatus: (status) => {
        if (status !== "disconnected" && status !== "error") return;
        this.connections.delete(threadId);
        if (this.manualStops.delete(threadId)) return; // 用户主动停止, 不回退
        // 未见终态流就断了 (网络错误/服务重启/被背压丢弃) → 回退轮询
        if (!terminalSeen) fireFallback();
      },
    });

    this.track(threadId, sse);
    sse
      .connect(`/api/execute/${threadId}/resume?last_event_id=${lastEventId}`, {})
      .finally(() => this.untrack(threadId, sse));
  }

  /** 订阅 thread 的 SSE 事件。返回取消订阅函数 + 连接状态 */
  subscribe(
    threadId: string,
    callback: EventCallback,
  ): { unsubscribe: () => void; isRunning: boolean } {
    if (!this.listeners.has(threadId)) {
      this.listeners.set(threadId, new Set());
    }
    this.listeners.get(threadId)!.add(callback);

    return {
      unsubscribe: () => {
        this.listeners.get(threadId)?.delete(callback);
      },
      isRunning: this.connections.has(threadId),
    };
  }

  /** 检查 thread 是否有活跃连接 */
  isRunning(threadId: string): boolean {
    return this.connections.has(threadId);
  }

  /** 手动停止连接 (用户点击停止按钮) */
  stop(threadId: string): void {
    const sse = this.connections.get(threadId);
    if (sse) {
      this.manualStops.add(threadId);
      sse.stop();
      this.connections.delete(threadId);
    }
    this.clearLastEventId(threadId);
  }

  /** 该 thread 最后收到的事件序号 (sessionStorage), 无则 null */
  getLastEventId(threadId: string): number | null {
    if (typeof window === "undefined") return null;
    const raw = sessionStorage.getItem(lastEventKey(threadId));
    if (raw === null) return null;
    const n = parseInt(raw, 10);
    return Number.isNaN(n) ? null : n;
  }

  // ── 内部 ──────────────────────────────────────────────────────

  private dispatch(threadId: string, event: SSEEvent): void {
    const subs = this.listeners.get(threadId);
    if (subs) {
      subs.forEach((fn) => {
        try {
          fn(event);
        } catch {
          // 单个订阅者报错不影响其他订阅者
        }
      });
    }
  }

  private track(threadId: string, sse: SSEClient): void {
    this.connections.set(threadId, sse);
  }

  /** 兜底清理: connect() 在流结束/出错/异常时都会 settle,
   *  保证任何路径下连接都不会残留为 "僵尸连接" (否则重进会话时
   *  isRunning 恒 true, UI 卡在 "AI 正在思考..." 且无法发送消息) */
  private untrack(threadId: string, sse: SSEClient): void {
    if (this.connections.get(threadId) === sse) {
      this.connections.delete(threadId);
    }
  }

  private saveLastEventId(threadId: string, eventId: number): void {
    if (typeof window === "undefined") return;
    try {
      sessionStorage.setItem(lastEventKey(threadId), String(eventId));
    } catch {
      // sessionStorage 不可用 (隐私模式等) — 静默降级为无续传
    }
  }

  private clearLastEventId(threadId: string): void {
    if (typeof window === "undefined") return;
    try {
      sessionStorage.removeItem(lastEventKey(threadId));
    } catch {}
  }
}

export const globalSSEManager = new GlobalSSEManager();
