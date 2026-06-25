"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Wrench, Clock } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ToolCallCardProps {
  message: ChatMessage;
}

export default function ToolCallCard({ message }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const isCall = message.msgType === "tool_call";
  const toolName = (message.metadata?.tool_name as string) || "unknown";

  return (
    <div className="flex gap-3">
      <div className="w-7 h-7 rounded-full bg-amber-500/10 flex items-center justify-center flex-shrink-0">
        <Wrench className="w-3.5 h-3.5 text-amber-500" />
      </div>
      <div className="max-w-[75%] min-w-[200px]">
        <button
          onClick={() => setExpanded(!expanded)}
          className={cn(
            "w-full flex items-center gap-2 px-3 py-2 rounded-lg border text-xs transition",
            isCall ? "bg-amber-50 dark:bg-amber-950/20 border-amber-200" : "bg-green-50 dark:bg-green-950/20 border-green-200"
          )}
        >
          {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          <span className="font-medium">
            {isCall ? "🔧 调用" : "✅ 结果"}: {toolName}
          </span>
          {message.metadata?.duration_ms ? (
            <span className="ml-auto text-muted-foreground flex items-center gap-1">
              <Clock className="w-3 h-3" />
              {message.metadata.duration_ms as number}ms
            </span>
          ) : null}
        </button>

        {expanded && (
          <div className="mt-1 p-2 bg-muted/50 rounded-lg text-xs font-mono max-h-60 overflow-y-auto">
            {isCall ? (
              <div>
                <p className="text-muted-foreground mb-1">输入参数:</p>
                <pre className="whitespace-pre-wrap">
                  {JSON.stringify(message.metadata?.tool_args || {}, null, 2)}
                </pre>
              </div>
            ) : (
              <div>
                <p className="text-muted-foreground mb-1">返回结果:</p>
                <pre className="whitespace-pre-wrap">{message.content}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
