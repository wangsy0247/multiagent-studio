"use client";

import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent } from "react";
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

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 128) + "px";
    }
  }, [text]);

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
    <div className="border-t bg-white p-3 space-y-2">
      {todos.length > 0 && (
        <div className="flex items-center gap-2 px-1">
          <ListTodo className="w-3.5 h-3.5 text-slate-400" />
          <div className="flex gap-1.5 overflow-x-auto">
            {todos.map((todo) => (
              <span
                key={todo.id}
                className={cn(
                  "px-2 py-0.5 rounded-md text-[10px] font-medium whitespace-nowrap",
                  todo.status === "completed" && "bg-emerald-50 text-emerald-700",
                  todo.status === "in_progress" && "bg-blue-50 text-blue-700",
                  todo.status === "failed" && "bg-red-50 text-red-700",
                  todo.status === "pending" && "bg-slate-100 text-slate-600",
                )}
              >
                {todo.description}
              </span>
            ))}
          </div>
        </div>
      )}

      {attachedFiles.length > 0 && (
        <div className="flex flex-wrap gap-2 px-1">
          {attachedFiles.map((file) => (
            <div
              key={file.filename}
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
                  onClick={() => onRemoveFile(file.filename)}
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
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入任务指令... (Enter 发送, Shift+Enter 换行)"
          rows={1}
          className="flex-1 resize-none px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus max-h-32 bg-slate-50"
          disabled={isStreaming}
        />
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
