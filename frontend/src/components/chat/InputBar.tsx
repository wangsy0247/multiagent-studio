"use client";

import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent, ClipboardEvent, DragEvent } from "react";
import { Send, Square, Paperclip, ListTodo, X, FileText } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { AttachedFile, AgentDefinition } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AgentMentionPicker } from "@/components/team/AgentMentionPicker";
import AgentSelector from "./AgentSelector";

interface InputBarProps {
  onSend: (text: string, files: AttachedFile[], targetAgents?: string[]) => void;
  onStop: () => void;
  isStreaming: boolean;
  attachedFiles?: AttachedFile[];
  onAttachFiles?: (files: FileList | File[] | null) => void;
  onRemoveFile?: (fileId: string) => void;
  // ── Agent Team 扩展 ──
  members?: AgentDefinition[];
  mode?: "single" | "team";
  // ── Agent 选择 ──
  selectedAgent?: string;
  onAgentChange?: (agentName: string) => void;
  // ── Plan 模式 (仅 single) ──
  planMode?: boolean;
  onPlanModeChange?: (v: boolean) => void;
}

export default function InputBar({
  onSend,
  onStop,
  isStreaming,
  attachedFiles = [],
  onAttachFiles,
  onRemoveFile,
  members = [],
  mode,
  selectedAgent,
  onAgentChange,
  planMode = false,
  onPlanModeChange,
}: InputBarProps) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { todos } = useChatStore();

  // ── @mention 状态 ──
  const [showMentions, setShowMentions] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 128) + "px";
    }
  }, [text]);

  // ── 检测 @ 提及 ──
  function handleInputChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const value = e.target.value;
    setText(value);
    // only enable @mention in team mode
    if (mode !== "team" || members.length === 0) {
      setShowMentions(false);
      return;
    }
    const lastAtIndex = value.lastIndexOf("@");
    if (lastAtIndex >= 0) {
      const afterAt = value.slice(lastAtIndex + 1);
      if (!afterAt.includes(" ") && afterAt.length <= 30) {
        setMentionQuery(afterAt);
        setShowMentions(true);
        return;
      }
    }
    setShowMentions(false);
  }

  function handleMentionSelect(agentName: string) {
    const lastAtIndex = text.lastIndexOf("@");
    const before = text.slice(0, lastAtIndex);
    const newText = `${before}@${agentName} `;
    setText(newText);
    setShowMentions(false);
    setMentionQuery("");
  }

  function handleSend() {
    if (!text.trim() && attachedFiles.length === 0) return;
    // 解析 @mentions
    const mentions = Array.from(
      text.matchAll(/@([A-Za-z0-9_-]+)/g),
    ).map((m) => m[1]);
    const uniqueMentions = [...new Set(mentions)];
    onSend(text.trim(), attachedFiles, uniqueMentions);
    setText("");
  }

  function handleKeyDown(e: KeyboardEvent) {
    // IME 组合状态 (中文/日文输入法选词) 中的 Enter 是确认候选词, 不是发送
    if (e.nativeEvent.isComposing) return;
    if (e.key === "Enter" && !e.shiftKey && !showMentions) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleFileSelect(e: ChangeEvent<HTMLInputElement>) {
    onAttachFiles?.(e.target.files);
    e.target.value = "";
  }

  // ── 粘贴上传: 剪贴板中的文件 (截图/复制的文件) 走与选择文件相同的路径 ──
  function handlePaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const items = e.clipboardData?.items;
    if (!items || !onAttachFiles) return;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length > 0) {
      e.preventDefault(); // 阻止把图片占位文本塞进输入框
      onAttachFiles(files);
    }
  }

  // ── 拖拽上传: 仅输入区容器接收, 带悬停高亮 ──
  const isUploading = attachedFiles.some((f) => f.status === "uploading");
  const [dragActive, setDragActive] = useState(false);
  const canDrop = !isStreaming && !isUploading;

  function handleDragOver(e: DragEvent) {
    if (!canDrop || !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    setDragActive(true);
  }

  function handleDragLeave(e: DragEvent) {
    // 子元素间移动不关闭高亮, 仅真正离开容器时关闭
    if (!e.currentTarget.contains(e.relatedTarget as Node)) {
      setDragActive(false);
    }
  }

  function handleDrop(e: DragEvent) {
    e.preventDefault();
    setDragActive(false);
    if (!canDrop || !onAttachFiles) return;
    if (e.dataTransfer.files.length > 0) {
      onAttachFiles(e.dataTransfer.files);
    }
  }

  return (
    <div
      className="relative border-t bg-white p-3 space-y-2"
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {dragActive && (
        <div className="pointer-events-none absolute inset-0 z-10 border-2 border-dashed border-hermes-400 bg-hermes-50/70 flex items-center justify-center">
          <span className="text-xs font-medium text-hermes-600">松开以上传文件</span>
        </div>
      )}
      {/* 任务全部完成/失败后隐藏清单卡片 */}
      {todos.length > 0 && todos.some((t) => t.status !== "completed" && t.status !== "failed") && (
        <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
          <div className="flex items-center gap-1.5 px-3 py-2 border-b border-slate-100 bg-slate-50/80">
            <ListTodo className="w-3.5 h-3.5 text-slate-500" />
            <span className="text-xs font-medium text-slate-600">任务清单</span>
            <span className="text-[10px] text-slate-400 ml-auto">
              {todos.filter((t) => t.status === "completed" || t.status === "failed").length}/{todos.length}
            </span>
          </div>
          <ol className="divide-y divide-slate-100 max-h-48 overflow-y-auto">
            {todos.map((todo, index) => (
              <li key={todo.id} className="flex items-center gap-2.5 px-3 py-2">
                <span
                  className={cn(
                    "w-[18px] h-[18px] rounded-full text-[10px] font-semibold flex items-center justify-center flex-shrink-0",
                    todo.status === "completed" && "bg-emerald-100 text-emerald-700",
                    todo.status === "in_progress" && "bg-hermes-100 text-hermes-700",
                    todo.status === "failed" && "bg-red-100 text-red-700",
                    todo.status === "pending" && "bg-slate-100 text-slate-500",
                  )}
                >
                  {index + 1}
                </span>
                <span
                  className={cn(
                    "flex-1 text-xs leading-5",
                    todo.status === "completed" && "line-through text-slate-400",
                    todo.status === "failed" && "text-red-500",
                    todo.status === "pending" && "text-slate-600",
                    todo.status === "in_progress" && "text-slate-800 font-medium",
                  )}
                >
                  {todo.description}
                </span>
                <span
                  className={cn(
                    "px-1.5 py-0.5 rounded text-[10px] font-medium whitespace-nowrap",
                    todo.status === "completed" && "bg-emerald-50 text-emerald-700",
                    todo.status === "in_progress" && "bg-hermes-50 text-hermes-700",
                    todo.status === "failed" && "bg-red-50 text-red-700",
                    todo.status === "pending" && "bg-slate-100 text-slate-500",
                  )}
                >
                  {todo.status === "completed" ? "已完成"
                    : todo.status === "in_progress" ? "进行中"
                    : todo.status === "failed" ? "失败" : "待处理"}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {attachedFiles.length > 0 && (
        <div className="flex flex-wrap gap-2 px-1">
          {attachedFiles.map((file) => (
            <div
              key={file.id}
              className={cn(
                "flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs border",
                file.status === "error"
                  ? "bg-red-50 border-red-200 text-red-700"
                  : "bg-slate-50 border-slate-200 text-slate-700"
              )}
            >
              <FileText className="w-3 h-3 flex-shrink-0" />
              <span className="max-w-[150px] truncate">{file.original_name || file.filename}</span>
              {file.status === "uploading" && (
                <span className="w-3 h-3 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
              )}
              {onRemoveFile && file.status !== "uploading" && (
                <button
                  onClick={() => onRemoveFile(file.id)}
                  className="ml-0.5 hover:text-red-500 transition-colors"
                  aria-label={`Remove ${file.filename}`}
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="flex items-end gap-2">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileSelect}
          disabled={isStreaming || isUploading}
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={isStreaming || isUploading}
          className="p-2 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-lg transition-colors disabled:opacity-50"
          aria-label="Attach files"
        >
          <Paperclip className="w-4 h-4" />
        </button>

        {/* ── Agent 选择器 ── */}
        {onAgentChange && (
          <AgentSelector
            value={selectedAgent || "default"}
            onChange={onAgentChange}
          />
        )}

        {/* ── 模式标签 ── */}
        {mode && (
          <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
            mode === "team"
              ? "bg-hermes-100 text-hermes-700"
              : "bg-slate-100 text-slate-600"
          }`}>
            {mode === "team" ? "Team" : "Single"}
          </span>
        )}

        {/* ── Plan 模式切换 (仅 single 模式; team 由 Lead 任务系统拆解) ── */}
        {mode !== "team" && onPlanModeChange && (
          <button
            onClick={() => onPlanModeChange(!planMode)}
            disabled={isStreaming}
            title={planMode ? "Plan 模式：先规划再执行（点击切回正常模式）" : "正常模式（点击切换到 Plan 模式）"}
            className={cn(
              "flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-medium transition-colors disabled:opacity-50",
              planMode
                ? "bg-hermes-600 text-white hover:bg-hermes-700"
                : "bg-slate-100 text-slate-500 hover:bg-slate-200"
            )}
          >
            <ListTodo className="w-3 h-3" />
            {planMode ? "Plan" : "正常"}
          </button>
        )}

        {/* ── @mention 补全 ── */}
        <div className="relative flex-1">
          {showMentions && (
            <AgentMentionPicker
              members={members}
              query={mentionQuery}
              onSelect={handleMentionSelect}
            />
          )}
          <textarea
            ref={textareaRef}
            value={text}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            placeholder={
              mode === "team"
                ? "输入团队目标... (@agent 点名, Enter 发送)"
                : "输入任务指令... (Enter 发送, Shift+Enter 换行)"
            }
            rows={1}
            className="w-full resize-none px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus max-h-32 bg-slate-50"
            disabled={isStreaming}
          />
        </div>
        {isStreaming ? (
          <button
            onClick={onStop}
            className="p-2.5 bg-red-500 text-white rounded-xl hover:bg-red-600 transition-colors shadow-sm"
          >
            <Square className="w-4 h-4" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={(!text.trim() && attachedFiles.length === 0) || isUploading}
            className="p-2.5 bg-slate-900 text-white rounded-xl hover:bg-slate-800 transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed shadow-sm active:scale-95"
          >
            <Send className="w-4 h-4" />
          </button>
        )}
      </div>

      <p className="text-[10px] text-slate-400 text-center">
        多 Agent 协作模式 | 支持工具调用、SubAgent 委托和 Plan Mode
      </p>
    </div>
  );
}
