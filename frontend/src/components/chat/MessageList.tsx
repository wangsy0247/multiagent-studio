"use client";

import { useRef, useEffect } from "react";
import { Bot, MessageSquare, GitBranch } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import MessageItem from "./MessageItem";

interface MessageListProps {
  messages: ChatMessage[];
  isStreaming: boolean;
}

export default function MessageList({ messages, isStreaming }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-gradient-to-b from-slate-50 to-white">
        <div className="text-center max-w-sm px-6 animate-fade-in">
          <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-slate-100 to-slate-200 flex items-center justify-center shadow-sm">
            <Bot className="w-7 h-7 text-slate-600" />
          </div>
          <h3 className="text-base font-semibold text-slate-900 mb-2">开始对话</h3>
          <p className="text-sm text-slate-500 mb-6">
            输入任务指令启动多 Agent 协作
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-white border rounded-xl p-3 text-left">
              <MessageSquare className="w-4 h-4 text-slate-400 mb-2" />
              <p className="text-xs font-medium text-slate-700">直接对话</p>
              <p className="text-[10px] text-slate-400 mt-0.5">使用默认编排启动</p>
            </div>
            <div className="bg-white border rounded-xl p-3 text-left">
              <GitBranch className="w-4 h-4 text-slate-400 mb-2" />
              <p className="text-xs font-medium text-slate-700">画布编排</p>
              <p className="text-[10px] text-slate-400 mt-0.5">先配置 Agent 拓扑</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
      {messages.map((msg) => (
        <MessageItem key={msg.id} message={msg} />
      ))}

      {isStreaming && (
        <div className="flex items-center gap-3 pl-11 py-2 animate-fade-in">
          <div className="w-7 h-7 rounded-full bg-slate-100 flex items-center justify-center">
            <Bot className="w-3.5 h-3.5 text-slate-500" />
          </div>
          <div className="flex gap-1.5">
            <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
            <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
            <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
          </div>
          <span className="text-xs text-slate-400">AI 正在思考...</span>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
