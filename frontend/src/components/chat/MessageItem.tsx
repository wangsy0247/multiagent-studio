"use client";

import { Bot, User, Wrench, Network, AlertTriangle } from "lucide-react";
import { ChatMessage, TokenUsage } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";
import ToolCallCard from "./ToolCallCard";
import SubAgentCard from "./SubAgentCard";
import TokenUsageBar from "./TokenUsageBar";

interface MessageItemProps {
  message: ChatMessage;
}

const roleConfig = {
  human: { icon: User, bg: "bg-blue-500/10", iconColor: "text-blue-500", label: "你" },
  ai: { icon: Bot, bg: "bg-primary/10", iconColor: "text-primary", label: "AI" },
  tool: { icon: Wrench, bg: "bg-amber-500/10", iconColor: "text-amber-500", label: "工具" },
  subagent: { icon: Network, bg: "bg-emerald-500/10", iconColor: "text-emerald-500", label: "SubAgent" },
  system: { icon: AlertTriangle, bg: "bg-destructive/10", iconColor: "text-destructive", label: "系统" },
};

export default function MessageItem({ message }: MessageItemProps) {
  const config = roleConfig[message.role] || roleConfig.system;
  const Icon = config.icon;

  // 工具调用卡片
  if (message.msgType === "tool_call" || message.msgType === "tool_result") {
    return <ToolCallCard message={message} />;
  }

  // SubAgent 卡片
  if (message.msgType === "subagent_start" || message.msgType === "subagent_end") {
    return <SubAgentCard message={message} />;
  }

  return (
    <div className={cn("flex gap-3", message.role === "human" && "flex-row-reverse")}>
      <div className={cn("w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0", config.bg)}>
        <Icon className={cn("w-3.5 h-3.5", config.iconColor)} />
      </div>

      {/* 消息内容 */}
      <div className={cn(
        "max-w-[75%] rounded-lg px-3 py-2 text-sm",
        message.role === "human"
          ? "bg-primary text-primary-foreground"
          : message.role === "system"
          ? "bg-destructive/10 border border-destructive/20 text-destructive"
          : "bg-card border"
      )}>
        {/* 角色 + 时间 */}
        <div className="flex items-center gap-2 mb-1">
          <span className="text-[10px] font-medium opacity-70">{config.label}</span>
          <span className="text-[9px] opacity-40">{formatDateTime(message.createdAt)}</span>
          {message.tokenCount > 0 ? (
            <span className="text-[9px] opacity-40">{message.tokenCount} tokens</span>
          ) : null}
        </div>

        <div className="whitespace-pre-wrap break-words leading-relaxed">
          {message.content}
        </div>

        {/* Token 消耗 */}
        {message.metadata?.tokens ? <TokenUsageBar tokens={message.metadata.tokens as TokenUsage} /> : null}
      </div>
    </div>
  );
}
