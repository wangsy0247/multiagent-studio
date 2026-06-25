"use client";

import { useState, useRef, KeyboardEvent } from "react";
import { Send, Square, Paperclip, ListTodo } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { cn } from "@/lib/utils";

interface InputBarProps {
  onSend: (text: string) => void;
  onStop: () => void;
  isStreaming: boolean;
}

export default function InputBar({ onSend, onStop, isStreaming }: InputBarProps) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { todos } = useChatStore();

  function handleSend() {
    if (!text.trim()) return;
    onSend(text.trim());
    setText("");
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

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

      {/* 输入区域 */}
      <div className="flex items-end gap-2">
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
            disabled={!text.trim()}
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
