"use client";

import { useState, useRef, KeyboardEvent, ChangeEvent } from "react";
import { Send, Square, Paperclip, ListTodo, X, FileText } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { AttachedFile } from "@/lib/types";
import { cn } from "@/lib/utils";

interface InputBarProps {
  onSend: (text: string, files: AttachedFile[]) => void;
  onStop: () => void;
  isStreaming: boolean;
  attachedFiles?: AttachedFile[];
  onAttachFiles?: (files: FileList | null) => void;
  onRemoveFile?: (filename: string) => void;
}

export default function InputBar({
  onSend,
  onStop,
  isStreaming,
  attachedFiles = [],
  onAttachFiles,
  onRemoveFile,
}: InputBarProps) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { todos } = useChatStore();

  function handleSend() {
    if (!text.trim() && attachedFiles.length === 0) return;
    onSend(text.trim(), attachedFiles);
    setText("");
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleFileSelect(e: ChangeEvent<HTMLInputElement>) {
    onAttachFiles?.(e.target.files);
    e.target.value = "";
  }

  const isUploading = attachedFiles.some((f) => f.status === "uploading");

  return (
    <div className="border-t bg-card p-3 space-y-2">
      {/* TODO 面板 */}
      {todos.length > 0 && (
        <div className="flex items-center gap-2 px-1">
          <ListTodo className="w-3.5 h-3.5 text-muted-foreground" />
          <div className="flex gap-2 overflow-x-auto">
            {todos.map((todo) => (
              <span
                key={todo.id}
                className={cn(
                  "px-2 py-0.5 rounded text-[10px] whitespace-nowrap",
                  todo.status === "completed" && "bg-green-100 text-green-700",
                  todo.status === "in_progress" && "bg-blue-100 text-blue-700",
                  todo.status === "failed" && "bg-red-100 text-red-700",
                  todo.status === "pending" && "bg-gray-100 text-gray-600",
                )}
              >
                {todo.description}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 已选文件列表 */}
      {attachedFiles.length > 0 && (
        <div className="flex flex-wrap gap-2 px-1">
          {attachedFiles.map((file) => (
            <div
              key={file.filename}
              className={cn(
                "flex items-center gap-1.5 px-2 py-1 rounded-md text-xs border",
                file.status === "error"
                  ? "bg-destructive/10 border-destructive/30 text-destructive"
                  : "bg-muted border-border text-foreground"
              )}
            >
              <FileText className="w-3 h-3 flex-shrink-0" />
              <span className="max-w-[150px] truncate">{file.original_name || file.filename}</span>
              {file.status === "uploading" && (
                <span className="w-3 h-3 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
              )}
              {file.status === "error" && file.error && (
                <span className="text-[10px] opacity-80" title={file.error}>!</span>
              )}
              {onRemoveFile && file.status !== "uploading" && (
                <button
                  onClick={() => onRemoveFile(file.filename)}
                  className="ml-0.5 hover:text-destructive"
                  aria-label={`Remove ${file.filename}`}
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 输入区域 */}
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
          className="p-2 text-muted-foreground hover:text-foreground hover:bg-muted rounded-lg transition disabled:opacity-50"
          aria-label="Attach files"
          title="Attach files"
        >
          <Paperclip className="w-4 h-4" />
        </button>
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入任务指令... (Enter 发送, Shift+Enter 换行)"
          rows={1}
          className="flex-1 resize-none px-3 py-2 text-sm border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/20 max-h-32"
          disabled={isStreaming}
        />
        {isStreaming ? (
          <button
            onClick={onStop}
            className="p-2 bg-destructive text-destructive-foreground rounded-lg hover:bg-destructive/90 transition"
          >
            <Square className="w-4 h-4" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={(!text.trim() && attachedFiles.length === 0) || isUploading}
            className="p-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition disabled:opacity-50"
          >
            <Send className="w-4 h-4" />
          </button>
        )}
      </div>

      <p className="text-[10px] text-muted-foreground text-center">
        多Agent协作模式 | 支持工具调用、SubAgent委托和Plan Mode
      </p>
    </div>
  );
}
