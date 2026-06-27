"use client";

import React, { useState } from "react";
import { ChevronDown, ChevronRight, Wrench, Clock } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ToolCallCardProps {
  message: ChatMessage;
}

const ToolCallCard = React.memo(function ToolCallCard({ message }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const isCall = message.msgType === "tool_call";
  const toolName = (message.metadata?.tool_name as string) || "unknown";

  return (
    <div className="flex gap-3 animate-fade-in-up">
      <div className="w-8 h-8 rounded-full bg-amber-100 flex items-center justify-center flex-shrink-0 shadow-sm">
        <Wrench className="w-4 h-4 text-amber-600" />
      </div>
      <div className="max-w-[80%] min-w-[200px]">
        <button
          onClick={() => setExpanded(!expanded)}
          className={cn(
            "w-full flex items-center gap-2 px-3.5 py-2.5 rounded-xl border text-xs transition-all duration-150",
            isCall
              ? "bg-amber-50 border-amber-200 hover:border-amber-300"
              : "bg-emerald-50 border-emerald-200 hover:border-emerald-300"
          )}
        >
          {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
          <span className="font-medium text-slate-700">
            {isCall ? "调用" : "结果"}: {toolName}
          </span>
          {message.metadata?.duration_ms ? (
            <span className="ml-auto text-slate-500 flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {message.metadata.duration_ms as number}ms
            </span>
          ) : null}
        </button>

        {expanded && (
          <div className="mt-1.5 p-3 bg-slate-50 rounded-xl border border-slate-200 text-xs font-mono max-h-60 overflow-y-auto animate-scale-in">
            {isCall ? (
              <div>
                <p className="text-slate-500 mb-1 font-sans">输入参数:</p>
                <pre className="whitespace-pre-wrap text-slate-700">
                  {JSON.stringify(message.metadata?.tool_args || {}, null, 2)}
                </pre>
              </div>
            ) : (
              <div>
                <p className="text-slate-500 mb-1 font-sans">返回结果:</p>
                <pre className="whitespace-pre-wrap text-slate-700">{message.content}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

export default ToolCallCard;
