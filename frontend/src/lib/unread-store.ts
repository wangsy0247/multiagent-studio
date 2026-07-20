/**
 * 定时任务未读状态 — zustand store
 * 侧边栏红点轮询 + 任务卡片"N 条新执行"，展开历史标记已读后刷新
 */

import { create } from "zustand";
import { scheduledTasksAPI } from "./api-client";

interface UnreadState {
  total: number;
  byTask: Record<string, number>;
  refresh: () => Promise<void>;
}

export const useUnreadStore = create<UnreadState>((set) => ({
  total: 0,
  byTask: {},
  refresh: async () => {
    try {
      const { data } = await scheduledTasksAPI.unreadCount();
      set({ total: data.total || 0, byTask: data.by_task || {} });
    } catch {
      // 静默失败：未读数只是提示，不影响主流程
    }
  },
}));
