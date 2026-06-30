/**
 * 聊天状态管理 — 消息、SSE 流、TODO、Token
 */

import { create } from "zustand";
import { ChatMessage, SSEEvent, TodoItem, TokenUsage, ClarificationRequest } from "./types";
import { generateId } from "./utils";

interface ChatStore {
  messages: ChatMessage[];
  isStreaming: boolean;
  todos: TodoItem[];
  tokenUsage: TokenUsage | null;
  cumulativeTokens: TokenUsage;
  title: string;
  error: string | null;
  pendingClarification: ClarificationRequest | null;
  _stopClarificationFn: (() => void) | null;

  // Per-thread isolation
  threadMessages: Record<string, ChatMessage[]>;
  threadTitles: Record<string, string>;
  activeThreadId: string | null;

  addMessage: (msg: Omit<ChatMessage, "id" | "createdAt">) => void;
  handleSSEEvent: (event: SSEEvent) => void;
  setStreaming: (v: boolean) => void;
  setTitle: (t: string) => void;
  clearMessages: () => void;
  setError: (e: string | null) => void;
  addTokenUsage: (usage: TokenUsage) => void;
  setPendingClarification: (req: ClarificationRequest | null) => void;
  setStopClarificationFn: (fn: (() => void) | null) => void;
  setActiveThread: (threadId: string) => void;
  setThreadMessages: (threadId: string, msgs: ChatMessage[]) => void;
}

export const useChatStore = create<ChatStore>((set, get) => ({
  messages: [],
  isStreaming: false,
  todos: [],
  tokenUsage: null,
  cumulativeTokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 },
  title: "新会话",
  error: null,
  pendingClarification: null,
  _stopClarificationFn: null,
  threadMessages: {},
  threadTitles: {},
  activeThreadId: null,

  addMessage: (msg) => {
    const message: ChatMessage = {
      ...msg,
      id: generateId(),
      createdAt: new Date().toISOString(),
    };
    set((s) => {
      const newMessages = [...s.messages, message];
      const tid = s.activeThreadId;
      return {
        messages: newMessages,
        ...(tid ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } } : {}),
      };
    });
  },

  handleSSEEvent: (event) => {
    const { type } = event;
    const s = get();

    // 校验事件归属 — 防止旧线程的 SSE 污染当前活跃线程
    if (event.thread_id && s.activeThreadId && event.thread_id !== s.activeThreadId) {
      return;
    }

    switch (type) {
      case "message":
        if (event.content) {
          // 检查是否已有正在流式输出的 AI 消息
          const lastMsg = s.messages[s.messages.length - 1];
          if (lastMsg && lastMsg.role === "ai" && lastMsg.msgType === "message" && s.isStreaming) {
            // 追加内容到最后一个 AI 消息 → 同步到 threadMessages
            const newMessages = s.messages.map((m, i) =>
              i === s.messages.length - 1 ? { ...m, content: m.content + event.content! } : m
            );
            const tid = s.activeThreadId;
            set({
              messages: newMessages,
              ...(tid ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } } : {}),
            });
          } else {
            get().addMessage({
              role: "ai",
              content: event.content,
              msgType: "message",
              metadata: {},
              tokenCount: 0,
            });
          }
        }
        break;

      case "tool_call":
        get().addMessage({
          role: "tool",
          content: event.tool_name || "unknown",
          msgType: "tool_call",
          metadata: { tool_name: event.tool_name, tool_args: event.tool_args },
          tokenCount: 0,
        });
        break;

      case "tool_result":
        get().addMessage({
          role: "tool",
          content: event.tool_result || "",
          msgType: "tool_result",
          metadata: { tool_name: event.tool_name, result: event.tool_result },
          tokenCount: 0,
        });
        break;

      case "subagent_start":
        get().addMessage({
          role: "subagent",
          content: `SubAgent "${event.subagent_name}" 开始执行`,
          msgType: "subagent_start",
          metadata: { subagent_name: event.subagent_name, instruction: event.instruction },
          tokenCount: 0,
        });
        break;

      case "subagent_end":
        get().addMessage({
          role: "subagent",
          content: event.content || `SubAgent "${event.subagent_name}" 执行完成`,
          msgType: "subagent_end",
          metadata: {
            subagent_name: event.subagent_name,
            status: event.status,
            duration_ms: event.duration_ms,
          },
          tokenCount: 0,
        });
        break;

      case "todo_update":
        if (event.todo) {
          set((s) => ({
            todos: s.todos.some((t) => t.id === event.todo!.id)
              ? s.todos.map((t) => (t.id === event.todo!.id ? event.todo! : t))
              : [...s.todos, event.todo!],
          }));
        }
        break;

      case "title_update":
        if (event.title && event.thread_id) {
          set((s) => ({
            title: event.title,
            threadTitles: { ...s.threadTitles, [event.thread_id]: event.title! },
          }));
        }
        break;

      case "token_usage":
        if (event.tokens) {
          get().addTokenUsage(event.tokens);
        }
        break;

      case "error":
        set({ error: event.content || "执行出错", isStreaming: false, pendingClarification: null, _stopClarificationFn: null });
        get().addMessage({
          role: "system",
          content: `❌ ${event.content || "未知错误"}`,
          msgType: "error",
          metadata: { status: event.status },
          tokenCount: 0,
        });
        break;

      case "clarification":
        // HITL: Agent 暂停等待用户输入
        set({ isStreaming: false });
        if (event.request) {
          set({ pendingClarification: event.request });
        }
        break;

      case "finished":
        set({ isStreaming: false, _stopClarificationFn: null });
        break;
    }
  },

  setStreaming: (v) => set({ isStreaming: v }),
  setTitle: (t) => set({ title: t }),
  clearMessages: () => set({ messages: [], todos: [], error: null, tokenUsage: null, pendingClarification: null }),
  setError: (e) => set({ error: e }),
  setPendingClarification: (req) => set({ pendingClarification: req }),
  setStopClarificationFn: (fn) => set({ _stopClarificationFn: fn }),

  addTokenUsage: (usage) =>
    set((s) => ({
      tokenUsage: usage,
      cumulativeTokens: {
        prompt_tokens: s.cumulativeTokens.prompt_tokens + usage.prompt_tokens,
        completion_tokens: s.cumulativeTokens.completion_tokens + usage.completion_tokens,
        total_tokens: s.cumulativeTokens.total_tokens + usage.total_tokens,
        cost_usd: s.cumulativeTokens.cost_usd + usage.cost_usd,
      },
    })),

  setActiveThread: (threadId) => {
    const s = get();
    const updated: Record<string, ChatMessage[]> = { ...s.threadMessages };
    if (s.activeThreadId) {
      updated[s.activeThreadId] = s.messages;
    }
    // 保存当前线程的 title 再切换
    const updatedTitles: Record<string, string> = { ...s.threadTitles };
    if (s.activeThreadId && s.title && s.title !== "新会话") {
      updatedTitles[s.activeThreadId] = s.title;
    }
    set({
      activeThreadId: threadId,
      messages: updated[threadId] || [],
      threadMessages: updated,
      threadTitles: updatedTitles,
      title: updatedTitles[threadId] || "新会话",
      isStreaming: false,  // 切换线程时停止流式状态
      todos: [],
      tokenUsage: null,
      cumulativeTokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 },
      error: null,
      pendingClarification: null,
    });
  },

  setThreadMessages: (threadId, msgs) => {
    set((s) => {
      const updated = { ...s.threadMessages, [threadId]: msgs };
      return {
        threadMessages: updated,
        messages: threadId === s.activeThreadId ? msgs : s.messages,
      };
    });
  },
}));
