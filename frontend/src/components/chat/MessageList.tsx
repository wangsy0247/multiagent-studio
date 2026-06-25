"use client";

import { useRef, useEffect } from "react";
import { Bot, User, Wrench, Network } from "lucide-react";
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
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <div className="w-12 h-12 mx-auto mb-3 rounded-full bg-primary/10 flex items-center justify-center">
            <Bot className="w-6 h-6 text-primary" />
          </div>
          <h3 className="text-sm font-medium mb-1">开始对话</h3>
          <p className="text-xs text-muted-foreground max-w-sm">
            输入任务指令启动多Agent协作。你可以先在画布中配置Agent拓扑，或直接使用默认编排。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
      {messages.map((msg) => (
        <MessageItem key={msg.id} message={msg} />
      ))}

      {/* 流式指示器 */}
      {isStreaming && (
        <div className="flex items-center gap-2 text-muted-foreground text-xs py-2">
          <div className="flex gap-1">
            <span className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
            <span className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
            <span className="w-2 h-2 bg-primary rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
          </div>
          AI 正在思考...
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
