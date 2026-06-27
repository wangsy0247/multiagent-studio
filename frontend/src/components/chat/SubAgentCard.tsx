"use client";

import React from "react";
import { Network, Clock, CheckCircle, XCircle, Loader2 } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import { cn, formatDuration } from "@/lib/utils";

interface SubAgentCardProps {
  message: ChatMessage;
}

const SubAgentCard = React.memo(function SubAgentCard({ message }: SubAgentCardProps) {
  const isStart = message.msgType === "subagent_start";
  const name = (message.metadata?.subagent_name as string) || "unknown";
  const instruction = message.metadata?.instruction as string;
  const status = message.metadata?.status as string;
  const durationMs = message.metadata?.duration_ms as number;

  const statusStyle = !isStart
    ? status === "success"
      ? { bg: "bg-emerald-100", icon: CheckCircle, color: "text-emerald-600" }
      : { bg: "bg-red-100", icon: XCircle, color: "text-red-600" }
    : { bg: "bg-emerald-100", icon: Loader2, color: "text-emerald-600" };

  const StatusIcon = statusStyle.icon;

  return (
    <div className="flex gap-3 animate-fade-in-up">
      <div className="w-8 h-8 rounded-full bg-emerald-100 flex items-center justify-center flex-shrink-0 shadow-sm">
        <Network className="w-4 h-4 text-emerald-600" />
      </div>
      <div className="max-w-[80%] min-w-[220px]">
        <div
          className={cn(
            "border rounded-xl px-4 py-3 text-xs",
            isStart
              ? "bg-emerald-50 border-emerald-200"
              : status === "success"
              ? "bg-emerald-50 border-emerald-200"
              : "bg-red-50 border-red-200"
          )}
        >
          <div className="flex items-center gap-2 mb-1.5">
            <StatusIcon className={cn("w-4 h-4", isStart && "animate-spin", statusStyle.color)} />
            <span className="font-medium text-slate-800">{name}</span>
            {durationMs ? (
              <span className="ml-auto text-slate-500 flex items-center gap-1">
                <Clock className="w-3 h-3" />
                {formatDuration(durationMs)}
              </span>
            ) : null}
          </div>

          {isStart && instruction && (
            <p className="text-slate-500 truncate">指令: {instruction}</p>
          )}

          {!isStart && message.content && (
            <div className="mt-2 p-2.5 bg-white/70 rounded-lg text-xs font-mono max-h-32 overflow-y-auto border border-slate-200/50">
              <pre className="whitespace-pre-wrap text-slate-700">{message.content}</pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
});

export default SubAgentCard;
