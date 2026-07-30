"use client";

import { useRef, useEffect, useCallback, useState, useMemo } from "react";
import { Send, Square, AlertTriangle, Bot } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { globalSSEManager } from "@/lib/global-sse";
import { threadsAPI, filesAPI, executeAPI, getCurrentUserId } from "@/lib/api-client";
import { ChatMessage, AttachedFile, AgentLogEntry, ClarificationRequest } from "@/lib/types";
import { generateId } from "@/lib/utils";
import { useProjectStore } from "@/lib/project-store";
import { useTeamStore } from "@/lib/team-store";
import { TeamMemberList } from "@/components/team/TeamMemberList";
import { TeamMessageFeed } from "@/components/team/TeamMessageFeed";
import MessageList from "./MessageList";
import ClarificationDialog from "./ClarificationDialog";
import InputBar from "./InputBar";
import SubagentDetailPanel from "./SubagentDetailPanel";

interface ChatPanelProps {
  threadId?: string;
  threadTitle?: string;
  // ── Agent Team 扩展 ──
  projectId?: string;
  agentName?: string;
  mode?: "single" | "team";
  onThreadCreated?: (threadId: string) => void;
  // ── Agent 隔离视图 ──
  viewMode?: "team" | "agent";
  viewAgentName?: string;
  agentLogEntries?: AgentLogEntry[];
  agentLogsLoading?: boolean;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ChatPanel({
  threadId,
  threadTitle,
  projectId,
  agentName,
  mode: propMode,
  onThreadCreated,
  viewMode,
  viewAgentName,
  agentLogEntries,
  agentLogsLoading,
}: ChatPanelProps) {
  const projectAgents = useProjectStore((state) =>
    projectId ? state.projectAgents : [],
  );

  const {
    messages, isStreaming, error, title, handleSSEEvent,
    setStreaming, addMessage, setError, _stopClarificationFn,
    setActiveThread, setThreadMessages,
  } = useChatStore();
  const prevThreadIdRef = useRef<string>("");
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);

  // ── Agent 选择状态 ──
  const [selectedAgent, setSelectedAgent] = useState<string>(agentName || "default");
  // 外部 prop 变化时同步
  useEffect(() => {
    if (agentName) setSelectedAgent(agentName);
  }, [agentName]);

  const handleAttachFiles = useCallback((fileList: FileList | null) => {
    if (!fileList) return;
    const newFiles: AttachedFile[] = Array.from(fileList).map((file) => ({
      id: generateId(), // 本地唯一 id — 同名文件按 id 区分, 避免互相覆盖状态
      filename: file.name,
      original_name: file.name,
      mime_type: file.type,
      size_bytes: file.size,
      virtual_path: `/mnt/user-data/uploads/${file.name}`,
      status: "pending",
    }));
    setAttachedFiles((prev) => [...prev, ...newFiles]);

    // Upload each file immediately
    newFiles.forEach((attached, index) => {
      const file = fileList[index];
      if (!file) return;

      setAttachedFiles((prev) =>
        prev.map((f) => (f.id === attached.id ? { ...f, status: "uploading" } : f))
      );

      filesAPI
        .upload(threadId || "", file)
        .then((res) => {
          const data = res.data || {};
          setAttachedFiles((prev) =>
            prev.map((f) =>
              f.id === attached.id
                ? {
                    ...f,
                    filename: data.filename || f.filename,
                    original_name: data.original_name || f.original_name,
                    mime_type: data.mime_type || f.mime_type,
                    size_bytes: data.size_bytes ?? f.size_bytes,
                    virtual_path: data.virtual_path || f.virtual_path,
                    status: "done",
                  }
                : f
            )
          );
        })
        .catch((err) => {
          const msg = err?.response?.data?.detail || err.message || "Upload failed";
          setAttachedFiles((prev) =>
            prev.map((f) =>
              f.id === attached.id ? { ...f, status: "error", error: msg } : f
            )
          );
        });
    });
  }, [threadId]);

  const handleRemoveFile = useCallback((fileId: string) => {
    setAttachedFiles((prev) => prev.filter((f) => f.id !== fileId));
  }, []);

  async function sendMessage(text: string, files: AttachedFile[]) {
    if ((!text.trim() && files.length === 0) || isStreaming) return;

    const resolvedMode = propMode || (projectId ? "team" : "single");

    // 无 threadId (项目页快速单聊): 先创建线程再发送
    let currentThreadId: string | undefined = threadId;
    if (!currentThreadId) {
      if (onThreadCreated && projectId) {
        try {
          const { data } = await threadsAPI.create({
            title: resolvedMode === "single" ? `与 ${selectedAgent} 的对话` : "团队对话",
            project_id: projectId,
            agent_name: resolvedMode === "single" ? selectedAgent : undefined,
            mode: resolvedMode,
          });
          currentThreadId = data.id;
          setActiveThread(data.id); // 立即切换, 保住下面的乐观消息气泡
          onThreadCreated(data.id);
        } catch (err) {
          console.error("创建会话线程失败", err);
          setError("创建会话线程失败，请重试");
          return;
        }
      } else {
        setError("尚未创建会话线程");
        return;
      }
    }
    if (!currentThreadId) return; // 类型收窄: 上面所有分支均已返回或赋值

    // 该线程已有活跃 SSE 连接 → connect() 会静默跳过, 提前拦截避免消息被吞.
    // 自愈合: 本地记录运行中但后端可能早已结束 (僵尸连接, 如旧 bundle 残留 /
    // 流异常结束未清理) → 以后端状态为准, 后端说没在跑就清掉本地僵尸连接放行.
    if (globalSSEManager.isRunning(currentThreadId)) {
      let backendBusy = true;
      try {
        const { data } = await executeAPI.getStatus(currentThreadId);
        backendBusy = data?.status === "running" || data?.status === "cancelling";
      } catch {
        backendBusy = true; // 状态查询失败时保持保守拦截
      }
      if (backendBusy) {
        setError("该会话正在运行中，请等待完成或先停止");
        return;
      }
      globalSSEManager.stop(currentThreadId); // 清理僵尸连接
    }

    // Build file metadata payload for Harness UploadsMiddleware
    const readyFiles = files.filter((f) => f.status === "done");
    const filesPayload = readyFiles.map((f) => ({
      filename: f.filename,
      size: f.size_bytes,
      path: f.virtual_path,
      mime_type: f.mime_type,
    }));

    const displayContent = [
      text,
      readyFiles.length > 0
        ? `\n\n[Attached ${readyFiles.length} file(s): ${readyFiles
            .map((f) => `${f.original_name || f.filename} (${formatFileSize(f.size_bytes)})`)
            .join(", ")}]`
        : "",
    ].join("");

    addMessage({
      role: "human",
      content: displayContent.trim(),
      msgType: "text",
      metadata: { files: filesPayload },
      tokenCount: 0,
    });
    setStreaming(true);
    setError(null);
    setAttachedFiles([]);

    // 通过全局 SSE 管理器启动连接 (连接生命周期独立于组件)
    globalSSEManager.connect(currentThreadId, "/api/execute", {
      thread_id: currentThreadId,
      user_id: getCurrentUserId(),
      message: text,
      files: filesPayload.length > 0 ? filesPayload : undefined,
      project_id: projectId || undefined,
      agent_name: selectedAgent,
      mode: resolvedMode,
    });
  }

  function stopExecution() {
    if (threadId) {
      // Notify backend to stop the run first; failure must not block local cleanup
      executeAPI.stop(threadId).catch(() => {});
      globalSSEManager.stop(threadId);
    }
    _stopClarificationFn?.();
    setStreaming(false);
  }

  // ── 线程切换：订阅全局 SSE 事件 + 加载历史消息 ──
  useEffect(() => {
    if (!threadId) return;

    // 订阅全局 SSE 管理器 (不关闭连接 — 切走时 agent 继续后台执行)
    const { unsubscribe } = globalSSEManager.subscribe(threadId, (event) => {
      handleSSEEvent(event);
      if (event.type === "finished" || event.type === "error") {
        setStreaming(false);
      }
    });

    // 检测是否切回了一个可能后台运行过的 thread
    const isSwitchingThread = prevThreadIdRef.current !== threadId;
    prevThreadIdRef.current = threadId;
    setActiveThread(threadId);  // 先切换活跃线程 (会重置 isStreaming=false)
    setAttachedFiles([]);

    // 如果该 thread 有活跃连接 → 跳过 DB 加载, 直接用 SSE 流式输出
    // ⚠️ 必须在 setActiveThread 之后调用 — setActiveThread 会重置 isStreaming=false,
    //    必须在此之后覆盖为 true, 否则后续 message token 会碎片化
    const isReconnecting = globalSSEManager.isRunning(threadId);
    if (isReconnecting) {
      useChatStore.setState({ _streamingMessageId: null, _streamingThinkingId: null });
      setStreaming(true);
    }

    const loadHistory = async () => {
      if (!threadId) return;
      // 活跃连接 → DB 是旧快照, SSE 才是实时流, 跳过 DB 加载
      if (isReconnecting) return;
      // 同一 thread 正在前台流式 → DB 是旧快照, 跳过
      if (!isSwitchingThread) {
        const current = useChatStore.getState().threadMessages[threadId];
        if (current && current.length > 0) return;
      }
      try {
        const { data } = await threadsAPI.getMessages(threadId);
        if (data?.messages && data.messages.length > 0) {
          const dbMsgs: ChatMessage[] = data.messages.map((m: any) => ({
            id: m.id,
            role: m.role,
            content: m.content,
            msgType: m.msg_type,
            metadata: m.extra_metadata || {},
            createdAt: m.created_at,
            tokenCount: m.token_count || 0,
          }));
          // 切回时 DB 有 agent 后台执行产生的完整记录, 替换内存残留
          setThreadMessages(threadId, dbMsgs);
          // 最后一条是未回答的澄清请求 (刷新前 agent 暂停) → 恢复弹出确认框.
          // 已回答时其后会有人类消息, 不会误恢复
          const last = dbMsgs[dbMsgs.length - 1];
          const req = last?.metadata?.request as ClarificationRequest | undefined;
          if (last?.msgType === "clarification" && req) {
            useChatStore.getState().setPendingClarification(req);
          }
        }
      } catch (err) {
        console.error("加载历史消息失败", err);
      }
    };
    loadHistory();

    return () => {
      // 组件卸载时只取消订阅，不关闭连接 (agent 继续后台执行)
      unsubscribe();
    };
  }, [threadId]);

  // ── Agent 日志 → ChatMessage 转换 (静态日志, 一次性渲染) ──
  const agentMessages: ChatMessage[] = useMemo(() => {
    if (!agentLogEntries) return [];
    return agentLogEntries.map((entry, index) => {
      if (entry.type === "task_boundary") {
        return {
          id: `boundary-${entry.task_id}-${index}`,
          role: "system" as const,
          content: entry.summary || "",
          msgType: "task_boundary",
          metadata: {
            task_id: entry.task_id,
            title: entry.title,
            status: entry.status,
          },
          createdAt: entry.timestamp,
          tokenCount: 0,
        };
      }
      // message entry — 区分 tool_call (工具调用) 和 tool_result (工具结果)
      if (entry.role === "tool_call") {
        return {
          id: `log-${entry.task_id}-${index}`,
          role: "tool" as const,
          content: entry.content || "",
          msgType: "tool_call",
          metadata: {
            task_id: entry.task_id,
            tool_name: entry.tool_name || "unknown",
            tool_args: entry.content || "",
          },
          createdAt: entry.timestamp,
          tokenCount: 0,
        };
      }
      if (entry.role === "tool_result") {
        return {
          id: `log-${entry.task_id}-${index}`,
          role: "tool" as const,
          content: entry.content || "",
          msgType: "tool_result",
          metadata: {
            task_id: entry.task_id,
            tool_name: entry.tool_name || "unknown",
          },
          createdAt: entry.timestamp,
          tokenCount: 0,
        };
      }
      const roleMap: Record<string, ChatMessage["role"]> = {
        human: "human",
        ai: "ai",
      };
      return {
        id: `log-${entry.task_id}-${index}`,
        role: roleMap[entry.role || "ai"] || "ai",
        content: entry.content || "",
        msgType: "text",
        metadata: { task_id: entry.task_id },
        createdAt: entry.timestamp,
        tokenCount: 0,
      };
    });
  }, [agentLogEntries]);

  // ── 标题持久化：SSE 更新 title 后同步到后端 ──
  const prevTitleRef = useRef(title);
  useEffect(() => {
    const currentThreadId = threadId;
    if (title && title !== prevTitleRef.current && title !== "新会话" && currentThreadId) {
      threadsAPI.updateTitle(currentThreadId, title).catch(console.error);
    }
    prevTitleRef.current = title;
  }, [title, threadId]);

  // ── Agent 隔离视图 (只读, 静态日志) ──
  const isAgentView = viewMode === "agent";

  return (
    <div className="flex h-full">
      {/* ── 左侧: 主聊天区 ── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* ── Agent 视图标题栏 ── */}
        {isAgentView && (
          <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-200 bg-blue-50 flex-shrink-0">
            <Bot className="w-3.5 h-3.5 text-blue-500" />
            <span className="text-xs font-medium text-blue-700">
              {viewAgentName} 的工作内容
            </span>
            <span className="text-xs text-blue-400">
              (只读 — Agent 执行的对话记录)
            </span>
            {agentLogsLoading && (
              <div className="w-3 h-3 border border-blue-300 border-t-blue-600 rounded-full animate-spin ml-auto" />
            )}
          </div>
        )}

        {/* ── Team 状态栏 (仅团队模式全视图) ── */}
        {!isAgentView && propMode === "team" && projectId && (
          <div className="border-b border-slate-200 bg-slate-50 p-3">
            <div className="flex gap-3">
              <div className="w-1/3">
                <TeamMemberList agents={projectAgents} />
              </div>
              <div className="w-2/3">
                <TeamMessageFeed agents={projectAgents} />
              </div>
            </div>
          </div>
        )}

        {/* ── 消息列表 ── */}
        {isAgentView && agentLogsLoading ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="w-5 h-5 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin mx-auto mb-2" />
              <p className="text-xs text-slate-400">加载 agent 工作记录...</p>
            </div>
          </div>
        ) : isAgentView && agentMessages.length === 0 ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <Bot className="w-8 h-8 mx-auto mb-2 text-slate-300" />
              <p className="text-sm text-slate-400">暂无工作内容</p>
              <p className="text-xs text-slate-400 mt-1">
                {viewAgentName} 尚未执行任何任务
              </p>
            </div>
          </div>
        ) : (
          <MessageList
            messages={isAgentView ? agentMessages : messages}
            isStreaming={isAgentView ? false : isStreaming}
          />
        )}

        {/* ── 错误提示 (仅团队全视图) ── */}
        {!isAgentView && error && (
          <div className="mx-4 mb-2 p-2 bg-destructive/10 border border-destructive/20 rounded-lg flex items-center gap-2 text-xs text-destructive">
            <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
            <span className="flex-1 truncate">{error}</span>
            <button onClick={() => setError(null)} className="text-destructive/70">&times;</button>
          </div>
        )}

        {/* ── 输入栏 (仅团队全视图) ── */}
        {!isAgentView && (
          <InputBar
            onSend={sendMessage}
            onStop={stopExecution}
            isStreaming={isStreaming}
            attachedFiles={attachedFiles}
            onAttachFiles={handleAttachFiles}
            onRemoveFile={handleRemoveFile}
            members={projectAgents}
            mode={propMode}
            selectedAgent={selectedAgent}
            onAgentChange={setSelectedAgent}
          />
        )}

        <ClarificationDialog threadId={threadId || ""} />
      </div>

      {/* ── 右侧: SubAgent 详情面板 ── */}
      <SubagentDetailPanel />
    </div>
  );
}
