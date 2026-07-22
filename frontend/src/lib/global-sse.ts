/**
 * 全局 SSE 连接管理器 — 连接生命周期独立于组件挂载/卸载。
 *
 * 解决的问题:
 *   - 切换会话时 ChatPanel unmount → 旧的 SSEClient.stop() 导致 agent 停止
 *   - 新方案: 连接在全局 Map 中管理, 组件只做订阅/取消订阅
 *
 * 使用方式:
 *   globalSSEManager.connect(threadId, url, body)
 *   const unsub = globalSSEManager.subscribe(threadId, callback)
 *   // 组件卸载时: unsub()
 */

import { SSEClient } from "./sse-client";
import type { SSEEvent } from "./types";

type EventCallback = (event: SSEEvent) => void;

class GlobalSSEManager {
  private connections: Map<string, SSEClient> = new Map();
  private listeners: Map<string, Set<EventCallback>> = new Map();

  /** 启动新 SSE 连接 (如果该 thread 已有连接则跳过) */
  connect(threadId: string, url: string, body: object): void {
    if (this.connections.has(threadId)) return;

    const sse = new SSEClient({
      onEvent: (event: SSEEvent) => {
        // 分发给该 thread 的所有订阅者
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
        // 收到终态事件后自动清理连接
        if (event.type === "finished" || event.type === "error") {
          this.connections.delete(threadId);
        }
      },
      maxReconnectAttempts: 0, // 全局管理器不自动重连 (由上层决定)
    });

    this.connections.set(threadId, sse);
    sse.connect(url, body);
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
      sse.stop();
      this.connections.delete(threadId);
    }
  }
}

export const globalSSEManager = new GlobalSSEManager();
