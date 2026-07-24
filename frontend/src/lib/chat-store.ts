/**
 * 聊天状态管理 — 消息、SSE 流、TODO、Token
 *
 * v2: 使用 streaming ID 追踪替代按 msgType 匹配。
 * 当 thinking / message 交替到达时，各自追加到同一个卡片，不再碎片化。
 */
import { create } from "zustand";
import { ChatMessage, SSEEvent, TodoItem, TokenUsage, ClarificationRequest, TeamMemberRuntimeStatus } from "./types";
import { generateId } from "./utils";
import { useTeamStore } from "./team-store";

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

  // ── Streaming buffer IDs (v2) ──
  /** 当前流式 AI 回复的消息 ID（所有 message chunk 都追加到这里） */
  _streamingMessageId: string | null;
  /** 当前流式 thinking 卡片的消息 ID（所有 thinking chunk 都追加到这里） */
  _streamingThinkingId: string | null;

  // ── SubAgent 详情面板 ──
  /** 当前选中的 SubAgent 消息 ID（右侧详情面板会展示其完整会话）。 */
  selectedSubagentId: string | null;
  selectSubagent: (id: string | null) => void;

  // ── SubAgent 子会话存储 (v3) ──
  /** 每个 SubAgent 的独立会话: subagent_name → 消息列表 */
  subConversations: Record<string, ChatMessage[]>;
  /** 追加消息到指定 SubAgent 的子会话 */
  appendToSubConversation: (name: string, msg: Omit<ChatMessage, "id" | "createdAt">) => void;

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

// ── helpers ──────────────────────────────────────────────────────────────

/** Append content to a message by ID in the messages array. */
function _appendToMessage(
  messages: ChatMessage[],
  targetId: string,
  content: string,
): ChatMessage[] {
  return messages.map((m) =>
    m.id === targetId ? { ...m, content: m.content + content } : m,
  );
}

/** Update metadata on a message by ID. */
function _updateMessageMeta(
  messages: ChatMessage[],
  targetId: string,
  meta: Record<string, unknown>,
): ChatMessage[] {
  return messages.map((m) =>
    m.id === targetId ? { ...m, metadata: { ...m.metadata, ...meta } } : m,
  );
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
  _streamingMessageId: null,
  _streamingThinkingId: null,
  selectedSubagentId: null,
  subConversations: {},

  selectSubagent: (id) => set({ selectedSubagentId: id }),

  appendToSubConversation: (name, msg) => {
    const message: ChatMessage = {
      ...msg,
      id: generateId(),
      createdAt: new Date().toISOString(),
    };
    set((s) => ({
      subConversations: {
        ...s.subConversations,
        [name]: [...(s.subConversations[name] || []), message],
      },
    }));
  },

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
      // ── AI 文本消息 ───────────────────────────────────────────────
      case "message":
        if (event.content) {
          const msgId = s._streamingMessageId;
          if (msgId && s.isStreaming) {
            // 追加到已有的流式消息
            const newMessages = _appendToMessage(s.messages, msgId, event.content);
            const tid = s.activeThreadId;
            set({
              messages: newMessages,
              ...(tid ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } } : {}),
            });
          } else {
            // 第一条 message chunk — 创建新消息并记住 ID
            const message: ChatMessage = {
              id: generateId(),
              role: "ai",
              content: event.content,
              msgType: "message",
              metadata: {},
              tokenCount: 0,
              createdAt: new Date().toISOString(),
            };
            const newMessages = [...s.messages, message];
            const tid = s.activeThreadId;
            set({
              messages: newMessages,
              _streamingMessageId: message.id,
              ...(tid ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } } : {}),
            });
          }
        }
        break;

      // ── 思考过程 ──────────────────────────────────────────────────
      case "thinking":
        if (event.content) {
          const thinkingId = s._streamingThinkingId;
          if (thinkingId && s.isStreaming) {
            // 追加到已有的 thinking 卡片
            const newMessages = _appendToMessage(s.messages, thinkingId, event.content);
            const tid = s.activeThreadId;
            set({
              messages: newMessages,
              ...(tid ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } } : {}),
            });
          } else {
            // 第一条 thinking chunk — 创建新的 thinking 卡片
            const message: ChatMessage = {
              id: generateId(),
              role: "ai",
              content: event.content,
              msgType: "thinking",
              metadata: {},
              tokenCount: 0,
              createdAt: new Date().toISOString(),
            };
            const newMessages = [...s.messages, message];
            const tid = s.activeThreadId;
            set({
              messages: newMessages,
              _streamingThinkingId: message.id,
              ...(tid ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } } : {}),
            });
          }
        }
        break;

      // ── 工具调用 ──────────────────────────────────────────────────
      case "tool_call":
        // 当前 AI 流式消息已完成 (触发工具调用的是完整的 AI 回复), 清除 streaming ID
        // 以便工具返回后的新 AI 回复创建新消息而不是追加到旧消息
        set({ _streamingMessageId: null, _streamingThinkingId: null });
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

      // ── SubAgent 事件 ─────────────────────────────────────────────
      case "subagent_start": {
        // task 工具调用触发了 subagent — 清除 streaming ID
        set({ _streamingMessageId: null, _streamingThinkingId: null });
        const sName = event.subagent_name || "unknown";
        // 初始化子会话存储
        if (!s.subConversations[sName]) {
          set((s2) => ({
            subConversations: { ...s2.subConversations, [sName]: [] },
          }));
        }
        get().addMessage({
          role: "subagent",
          content: `SubAgent "${sName}" 开始执行`,
          msgType: "subagent_start",
          metadata: {
            subagent_name: sName,
            instruction: event.instruction,
            max_turns: event.max_turns || 50,
          },
          tokenCount: 0,
        });
        break;
      }

      case "subagent_progress": {
        const latestStart = [...s.messages].reverse().find(
          (m) =>
            (m.msgType === "subagent_start" || m.msgType === "subagent_progress") &&
            m.metadata?.subagent_name === event.subagent_name,
        );
        if (latestStart) {
          const newMessages = s.messages.map((m) =>
            m.id === latestStart.id
              ? {
                  ...m,
                  msgType: "subagent_progress" as ChatMessage["msgType"],
                  metadata: {
                    ...m.metadata,
                    iterations: event.iterations,
                    max_turns: event.max_turns ?? m.metadata.max_turns,
                    current_step: event.current_step,
                  },
                }
              : m,
          );
          const tid = s.activeThreadId;
          set({
            messages: newMessages,
            ...(tid ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } } : {}),
          });
        }
        break;
      }

      case "subagent_end": {
        // ── 原地更新 start/progress 卡片为最终状态 ──
        // 不再 addMessage 创建第二张卡片
        const sName: string = event.subagent_name || "unknown";
        const finalContent: string =
          event.content ||
          event.subagent_result?.output ||
          `SubAgent "${sName}" 执行完成`;
        const finalTokens: number =
          event.subagent_result?.token_usage_records?.reduce(
            (sum: number, r: { total_tokens?: number }) =>
              sum + (r.total_tokens || 0),
            0,
          ) || 0;

        // 找到匹配的运行中卡片
        const card = [...s.messages].reverse().find(
          (m) =>
            (m.msgType === "subagent_start" ||
              m.msgType === "subagent_progress") &&
            m.metadata?.subagent_name === sName,
        );

        if (card) {
          const newMessages = s.messages.map((m) =>
            m.id === card.id
              ? {
                  ...m,
                  content: finalContent,
                  msgType: "subagent_end" as ChatMessage["msgType"],
                  tokenCount: finalTokens,
                  metadata: {
                    ...m.metadata,
                    subagent_name: sName,
                    status: event.status || event.subagent_result?.status,
                    duration_ms: event.duration_ms,
                    subagent_result: event.subagent_result,
                  },
                }
              : m,
          );
          const tid = s.activeThreadId;
          set({
            messages: newMessages,
            ...(tid
              ? { threadMessages: { ...s.threadMessages, [tid]: newMessages } }
              : {}),
          });
        } else {
          // 兜底: 没有找到运行中卡片时新建
          get().addMessage({
            role: "subagent",
            content: finalContent,
            msgType: "subagent_end",
            metadata: {
              subagent_name: sName,
              status: event.status || event.subagent_result?.status,
              duration_ms: event.duration_ms,
              subagent_result: event.subagent_result,
            },
            tokenCount: finalTokens,
          });
        }
        break;
      }

      // ── SubAgent 内部事件 (v3) — 只进子会话, 不进主聊天 ────────
      case "subagent_thinking": {
        const name = event.subagent_name;
        if (name && event.content) {
          get().appendToSubConversation(name, {
            role: "ai",
            content: event.content,
            msgType: "thinking",
            metadata: {},
            tokenCount: 0,
          });
        }
        break;
      }

      case "subagent_tool_call": {
        const name = event.subagent_name;
        if (name) {
          get().appendToSubConversation(name, {
            role: "tool",
            content: event.tool_name || "unknown",
            msgType: "tool_call",
            metadata: {
              tool_name: event.tool_name,
              tool_args: event.tool_args,
            },
            tokenCount: 0,
          });
        }
        break;
      }

      case "subagent_tool_result": {
        const name = event.subagent_name;
        if (name && event.tool_result) {
          get().appendToSubConversation(name, {
            role: "tool",
            content: event.tool_result,
            msgType: "tool_result",
            metadata: {
              tool_name: event.tool_name,
              tool_result: event.tool_result,
            },
            tokenCount: 0,
          });
        }
        break;
      }

      // ── Agent Team 事件 ──────────────────────────────────────────
      case "message_injected": {
        get().addMessage({
          role: "system",
          content: `📨 ${event.content || "消息已注入给 Lead"}`,
          msgType: "text",
          metadata: { event_type: "message_injected", thread_id: event.thread_id },
          tokenCount: 0,
        });
        break;
      }

      case "team_start": {
        useTeamStore.getState().setRunning(true);
        if (event.members && event.members.length > 0) {
          useTeamStore.getState().initMembers(event.members);
        }
        // 在主聊天中插入一条系统消息
        get().addMessage({
          role: "system",
          content: `🚀 Team 模式已启动 (${event.members?.length || 0} 个成员)`,
          msgType: "text",
          metadata: { event_type: "team_start", members: event.members, project_id: event.project_id },
          tokenCount: 0,
        });
        break;
      }

      case "team_status": {
        get().addMessage({
          role: "system",
          content: `📋 ${event.content || event.phase || "Team 状态更新"}`,
          msgType: "text",
          metadata: { event_type: "team_status", phase: event.phase },
          tokenCount: 0,
        });
        break;
      }

      case "team_task_update": {
        if (event.task) {
          useTeamStore.getState().addTask(event.task);
        }
        break;
      }

      case "member_status": {
        useTeamStore.getState().updateMemberStatus(
          event.agent_name || event.subagent_name || "unknown",
          (event.status as TeamMemberRuntimeStatus) || "idle",
          event.task_id || event.current_task_id,
          event.task_title,
          event.started_at,
        );
        break;
      }

      case "team_message": {
        if (event.message) {
          useTeamStore.getState().addMessage(event.message);
        }
        break;
      }

      case "team_end": {
        useTeamStore.getState().setRunning(false);
        get().addMessage({
          role: "system",
          content: `✅ Team 执行结束 (状态: ${event.status}, 轮次: ${event.total_rounds || 0})`,
          msgType: "text",
          metadata: { event_type: "team_end", status: event.status, total_rounds: event.total_rounds },
          tokenCount: 0,
        });
        break;
      }

      case "team_error": {
        useTeamStore.getState().setRunning(false);
        get().addMessage({
          role: "system",
          content: `❌ Team 错误: ${event.content || "未知错误"}`,
          msgType: "error",
          metadata: { event_type: "team_error" },
          tokenCount: 0,
        });
        break;
      }

      case "team_degrade": {
        get().addMessage({
          role: "system",
          content: `⚠️ Team 模式降级为单 Agent: ${event.reason || ""}`,
          msgType: "text",
          metadata: { event_type: "team_degrade", reason: event.reason },
          tokenCount: 0,
        });
        break;
      }

      // ── TODO / Title / Token ─────────────────────────────────────
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

      // ── 错误 / 澄清 / 结束 ───────────────────────────────────────
      case "error":
        set({
          error: event.content || "执行出错",
          isStreaming: false,
          pendingClarification: null,
          _stopClarificationFn: null,
          _streamingMessageId: null,
          _streamingThinkingId: null,
        });
        get().addMessage({
          role: "system",
          content: `❌ ${event.content || "未知错误"}`,
          msgType: "error",
          metadata: { status: event.status },
          tokenCount: 0,
        });
        break;

      case "clarification":
        set({ isStreaming: false });
        if (event.request) {
          set({ pendingClarification: event.request });
        }
        break;

      // ── /clear 指令: 上下文已清空, 同步清空本地历史显示 ──
      case "context_cleared": {
        const clearedTid = s.activeThreadId;
        set({
          messages: [],
          todos: [],
          tokenUsage: null,
          pendingClarification: null,
          _streamingMessageId: null,
          _streamingThinkingId: null,
          ...(clearedTid
            ? { threadMessages: { ...s.threadMessages, [clearedTid]: [] } }
            : {}),
        });
        break;
      }

      case "finished":
        set({
          isStreaming: false,
          _stopClarificationFn: null,
          // ⚠️ 不在这里清除 streaming ID — 历史消息加载时需要保留
          // 下一次用户发送消息时会通过 addMessage 自然创建新 ID
        });
        break;
    }
  },

  setStreaming: (v) =>
    set({
      isStreaming: v,
      // 开始新的一轮流式时清除旧 ID
      ...(v ? {} : { _streamingMessageId: null, _streamingThinkingId: null }),
    }),

  setTitle: (t) => set({ title: t }),
  clearMessages: () =>
    set({
      messages: [],
      todos: [],
      error: null,
      tokenUsage: null,
      pendingClarification: null,
      _streamingMessageId: null,
      _streamingThinkingId: null,
      selectedSubagentId: null,
      subConversations: {},
    }),
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
      isStreaming: false,
      todos: [],
      tokenUsage: null,
      cumulativeTokens: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 },
      error: null,
      pendingClarification: null,
      _streamingMessageId: null,
      _streamingThinkingId: null,
      selectedSubagentId: null,
      subConversations: {},
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
