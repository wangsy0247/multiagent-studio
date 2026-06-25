"use client";

import { Network, Clock, CheckCircle, XCircle, Loader2 } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import { cn, formatDuration } from "@/lib/utils";

interface SubAgentCardProps {
  message: ChatMessage;
}

export default function SubAgentCard({ message }: SubAgentCardProps) {
  const isStart = message.msgType === "subagent_start";
  const name = (message.metadata?.subagent_name as string) || "unknown";
  const instruction = message.metadata?.instruction as string;
  const status = message.metadata?.status as string;
  const durationMs = message.metadata?.duration_ms as number;

  return (
    <div className="flex gap-3">
      <div className="w-7 h-7 rounded-full bg-emerald-500/10 flex items-center justify-center flex-shrink-0">
        <Network className="w-3.5 h-3.5 text-emerald-500" />
      </div>
      <div className="max-w-[75%] min-w-[220px]">
        <div
          className={cn(
            "border rounded-lg px-3 py-2 text-xs",
            isStart
              ? "bg-emerald-50 dark:bg-emerald-950/20 border-emerald-200"
              : status === "success"
              ? "bg-green-50 dark:bg-green-950/20 border-green-200"
              : "bg-red-50 dark:bg-red-950/20 border-red-200"
          )}
        >
          {/* 头部 */}
          <div className="flex items-center gap-2 mb-1">
            {isStart ? (
              <Loader2 className="w-3.5 h-3.5 text-emerald-500 animate-spin" />
            ) : status === "success" ? (
              <CheckCircle className="w-3.5 h-3.5 text-green-500" />
            ) : (
              <XCircle className="w-3.5 h-3.5 text-red-500" />
            )}
            <span className="font-medium">{name}</span>
            {durationMs && (
              <span className="ml-auto text-muted-foreground flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {formatDuration(durationMs)}
              </span>
            )}
          </div>

          {/* 指令 */}
          {isStart && instruction && (
            <p className="text-muted-foreground truncate">指令: {instruction}</p>
          )}

          {/* 结果 */}
          {!isStart && message.content && (
            <div className="mt-1 p-2 bg-muted/50 rounded text-xs font-mono max-h-32 overflow-y-auto">
              <pre className="whitespace-pre-wrap">{message.content}</pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
