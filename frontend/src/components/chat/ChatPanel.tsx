"use client";

import { useRef, useEffect, useCallback, useState } from "react";
import { Send, Square, AlertTriangle } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { SSEClient } from "@/lib/sse-client";
import { useCanvasStore } from "@/lib/canvas-store";
import { threadsAPI, filesAPI } from "@/lib/api-client";
import { ChatMessage, AttachedFile } from "@/lib/types";
import MessageList from "./MessageList";
import ClarificationDialog from "./ClarificationDialog";
import InputBar from "./InputBar";

interface ChatPanelProps {
  threadId: string;
  threadTitle: string;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ChatPanel({ threadId, threadTitle }: ChatPanelProps) {
  const {
    messages, isStreaming, error, title, handleSSEEvent,
    setStreaming, addMessage, setError, _stopClarificationFn,
    setActiveThread, setThreadMessages,
  } = useChatStore();
  const { exportGraph } = useCanvasStore();
  const sseRef = useRef<SSEClient | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);

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
        .upload(threadId, file)
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

    // 只在用户配置了完整的 SubAgent 画布时才发送 execution_graph
    const canvasNodes = useCanvasStore.getState().nodes;
    const hasConfiguredSubagents = canvasNodes.some(
      (n) => !n.data.isEntryPoint && n.data.config.name && n.data.config.name.trim()
    );
    const graph = hasConfiguredSubagents ? exportGraph() : undefined;

    const sse = new SSEClient({
      onEvent: (event) => {
        handleSSEEvent(event);
        if (event.type === "finished" || event.type === "error") {
          setStreaming(false);
        }
      },
      onStatus: (status) => {
        if (status === "error") setStreaming(false);
      },
    });
    sseRef.current = sse;

    try {
      await sse.connect("/api/execute", {
        thread_id: threadId,
        message: text,
        execution_graph: graph || undefined,
        files: filesPayload.length > 0 ? filesPayload : undefined,
      });
    } catch (err: any) {
      console.error(err);
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
    if (!threadId) return;

    // 停止上一次的 SSE 连接，防止旧线程事件污染新线程
    if (sseRef.current) {
      sseRef.current.stop();
      sseRef.current = null;
    }
    setStreaming(false);
    setActiveThread(threadId);
    setAttachedFiles([]);

    const loadHistory = async () => {
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
    if (title && title !== prevTitleRef.current && title !== "新会话" && threadId) {
      threadsAPI.updateTitle(threadId, title).catch(console.error);
    }
    prevTitleRef.current = title;
  }, [title, threadId]);

  return (
    <div className="flex flex-col h-full">
      {/* 消息列表 */}
      <MessageList messages={messages} isStreaming={isStreaming} />

      {/* 错误横幅 */}
      {error && (
        <div className="mx-4 mb-2 p-2 bg-destructive/10 border border-destructive/20 rounded-lg flex items-center gap-2 text-xs text-destructive">
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
          <span className="flex-1 truncate">{error}</span>
          <button onClick={() => setError(null)} className="text-destructive/70">&times;</button>
        </div>
      )}

      {/* 输入栏 */}
      <InputBar
        onSend={sendMessage}
        onStop={stopExecution}
        isStreaming={isStreaming}
        attachedFiles={attachedFiles}
        onAttachFiles={handleAttachFiles}
        onRemoveFile={handleRemoveFile}
      />

      {/* 澄清弹窗 */}
      <ClarificationDialog threadId={threadId} />
    </div>
  );
}
