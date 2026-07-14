"use client";

import { useRef, useEffect, useCallback, useState } from "react";
import { Send, Square, AlertTriangle } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { SSEClient } from "@/lib/sse-client";
import { threadsAPI, filesAPI } from "@/lib/api-client";
import { ChatMessage, AttachedFile } from "@/lib/types";
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
}: ChatPanelProps) {
  const projectAgents = useProjectStore((state) =>
    projectId ? state.projectAgents : [],
  );

  const {
    messages, isStreaming, error, title, handleSSEEvent,
    setStreaming, addMessage, setError, _stopClarificationFn,
    setActiveThread, setThreadMessages,
  } = useChatStore();
  const sseRef = useRef<SSEClient | null>(null);
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
      filename: file.name,
      original_name: file.name,
      mime_type: file.type,
      size_bytes: file.size,
      virtual_path: `/mnt/user-data/uploads/${file.name}`,
      status: "pending",
    }));
    setAttachedFiles((prev) => [...prev, ...newFiles]);

    // Upload each file immediately
    newFiles.forEach((attached) => {
      const file = Array.from(fileList).find((f) => f.name === attached.filename);
      if (!file) return;

      setAttachedFiles((prev) =>
        prev.map((f) => (f.filename === attached.filename ? { ...f, status: "uploading" } : f))
      );

      filesAPI
        .upload(threadId || "", file)
        .then((res) => {
          const data = res.data || {};
          setAttachedFiles((prev) =>
            prev.map((f) =>
              f.filename === attached.filename
                ? {
                    ...f,
                    id: data.id,
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
              f.filename === attached.filename ? { ...f, status: "error", error: msg } : f
            )
          );
        });
    });
  }, [threadId]);

  const handleRemoveFile = useCallback((filename: string) => {
    setAttachedFiles((prev) => prev.filter((f) => f.filename !== filename));
  }, []);

  async function sendMessage(text: string, files: AttachedFile[]) {
    if ((!text.trim() && files.length === 0) || isStreaming) return;

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

    let connected = false;
    const sse = new SSEClient({
      onEvent: (event) => {
        connected = true;
        handleSSEEvent(event);
        if (event.type === "finished" || event.type === "error") {
          setStreaming(false);
        }
      },
      onStatus: (status) => {
        if (status === "connected") connected = true;
        if (status === "error" && !connected) {
          // 连接直接失败（后端不可达） → 提示用户
          setStreaming(false);
          setError("无法连接后端服务 (localhost:8000)，请确认已启动 Harness + App 服务");
        }
      },
      maxReconnectAttempts: 1, // 提问场景不需要重连
    });
    sseRef.current = sse;

    // 如果没有 threadId 但有 projectId，等待由外部传入（ChatTab 中通过 onThreadCreated 回调设置）
    const currentThreadId = threadId;
    if (!currentThreadId) {
      setError("尚未创建会话线程");
      return;
    }

    try {
      const resolvedMode = propMode || (projectId ? "team" : "single");
      await sse.connect("/api/execute", {
        thread_id: currentThreadId,
        user_id: (() => {
          try {
            const stored = localStorage.getItem("auth-storage");
            if (stored) {
              const { state } = JSON.parse(stored);
              return state?.user?.id || "default";
            }
          } catch {}
          return "default";
        })(),
        message: text,
        files: filesPayload.length > 0 ? filesPayload : undefined,
        project_id: projectId || undefined,
        agent_name: selectedAgent,
        mode: resolvedMode,
      });
    } catch (err: any) {
      console.error("SSE 连接异常:", err);
      if (!connected) {
        setError("无法连接后端服务，请确认端口 8000 已启动");
      }
    } finally {
      setStreaming(false);
      sseRef.current = null;
    }
  }

  function stopExecution() {
    sseRef.current?.stop();
    _stopClarificationFn?.();
    setStreaming(false);
  }

  // ── 线程切换：设置活跃线程 + 加载历史消息 ──
  useEffect(() => {
    if (!threadId) return;  // threadId 可选：仅在已有线程时设置

    // 停止上一次的 SSE 连接，防止旧线程事件污染新线程
    if (sseRef.current) {
      sseRef.current.stop();
      sseRef.current = null;
    }
    setStreaming(false);
    setActiveThread(threadId);
    setAttachedFiles([]);

    const loadHistory = async () => {
      if (!threadId) return;
      // 已有内存消息时跳过（避免覆盖流式数据）
      const current = useChatStore.getState().threadMessages[threadId];
      if (current && current.length > 0) return;

      try {
        const { data } = await threadsAPI.getMessages(threadId);
        if (data?.messages && data.messages.length > 0) {
          const msgs: ChatMessage[] = data.messages.map((m: any) => ({
            id: m.id,
            role: m.role,
            content: m.content,
            msgType: m.msg_type,
            metadata: m.extra_metadata || {},
            createdAt: m.created_at,
            tokenCount: m.token_count || 0,
          }));
          setThreadMessages(threadId, msgs);
        }
      } catch (err) {
        console.error("加载历史消息失败", err);
      }
    };
    loadHistory();

    return () => {
      // 组件卸载时清理 SSE
      if (sseRef.current) {
        sseRef.current.stop();
        sseRef.current = null;
      }
      setStreaming(false);
    };
  }, [threadId]);

  // ── 标题持久化：SSE 更新 title 后同步到后端 ──
  const prevTitleRef = useRef(title);
  useEffect(() => {
    const currentThreadId = threadId;
    if (title && title !== prevTitleRef.current && title !== "新会话" && currentThreadId) {
      threadsAPI.updateTitle(currentThreadId, title).catch(console.error);
    }
    prevTitleRef.current = title;
  }, [title, threadId]);

  return (
    <div className="flex h-full">
      {/* ── 左侧: 主聊天区 ── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* ── Team 状态栏 ── */}
        {propMode === "team" && projectId && (
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

        <MessageList messages={messages} isStreaming={isStreaming} />

        {error && (
          <div className="mx-4 mb-2 p-2 bg-destructive/10 border border-destructive/20 rounded-lg flex items-center gap-2 text-xs text-destructive">
            <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
            <span className="flex-1 truncate">{error}</span>
            <button onClick={() => setError(null)} className="text-destructive/70">&times;</button>
          </div>
        )}

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

        <ClarificationDialog threadId={threadId || ""} />
      </div>

      {/* ── 右侧: SubAgent 详情面板 ── */}
      <SubagentDetailPanel />
    </div>
  );
}
