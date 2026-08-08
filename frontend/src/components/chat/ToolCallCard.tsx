"use client";

import React, { useState } from "react";
import { ChevronDown, ChevronRight, Wrench, Clock } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ToolCallCardProps {
  message: ChatMessage;
}

/**
 * 工具调用/结果 — 极简文本行 (无方框)。
 * 展开后详情为左边线缩进的等宽块, 无边框无底色卡片。
 */
const ToolCallCard = React.memo(function ToolCallCard({ message }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const isCall = message.msgType === "tool_call";
  const toolName = (message.metadata?.tool_name as string) || "unknown";

  return (
    <div className="animate-fade-in-up">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 py-0.5 text-xs text-slate-500 hover:text-slate-700 transition-colors"
      >
        {expanded ? (
          <ChevronDown className="w-3 h-3 text-slate-400" />
        ) : (
          <ChevronRight className="w-3 h-3 text-slate-400" />
        )}
        <Wrench className={cn("w-3 h-3", isCall ? "text-amber-500" : "text-emerald-500")} />
        <span>
          {isCall ? "调用" : "结果"}: <span className="font-medium">{toolName}</span>
        </span>
        {message.metadata?.duration_ms ? (
          <span className="text-slate-400 flex items-center gap-0.5">
            <Clock className="w-3 h-3" />
            {message.metadata.duration_ms as number}ms
          </span>
        ) : null}
      </button>

      {expanded && (
        <div className="ml-[18px] mt-0.5 mb-1 border-l-2 border-slate-200 pl-3 text-xs font-mono max-h-60 overflow-y-auto animate-scale-in">
          {isCall ? (
            <div>
              <p className="text-slate-400 mb-1 font-sans">输入参数:</p>
              <pre className="whitespace-pre-wrap text-slate-600">
                {JSON.stringify(message.metadata?.tool_args || {}, null, 2)}
              </pre>
            </div>
          ) : (
            <div>
              <p className="text-slate-400 mb-1 font-sans">返回结果:</p>
              <pre className="whitespace-pre-wrap text-slate-600">{message.content}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
});

export default ToolCallCard;
