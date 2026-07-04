"use client";

import React from "react";
import {
  Network, Clock, CheckCircle, XCircle, Loader2,
  AlertTriangle, Cpu, ChevronRight,
} from "lucide-react";
import { ChatMessage, SubAgentResultData } from "@/lib/types";
import { cn, formatDuration } from "@/lib/utils";
import { useChatStore } from "@/lib/chat-store";

interface SubAgentCardProps {
  message: ChatMessage;
}

// ── 紧凑状态样式 ──────────────────────────────────────────────────────
const STATUS_STYLE: Record<
  string,
  { bg: string; border: string; icon: typeof CheckCircle; color: string; label: string }
> = {
  running: {
    bg: "bg-blue-50", border: "border-blue-200",
    icon: Loader2, color: "text-blue-500", label: "执行中",
  },
  success: {
    bg: "bg-emerald-50", border: "border-emerald-200",
    icon: CheckCircle, color: "text-emerald-500", label: "完成",
  },
  error: {
    bg: "bg-red-50", border: "border-red-200",
    icon: XCircle, color: "text-red-500", label: "失败",
  },
  cancelled: {
    bg: "bg-amber-50", border: "border-amber-200",
    icon: AlertTriangle, color: "text-amber-500", label: "已取消",
  },
  timed_out: {
    bg: "bg-orange-50", border: "border-orange-200",
    icon: Clock, color: "text-orange-500", label: "超时",
  },
  max_iterations_reached: {
    bg: "bg-indigo-50", border: "border-indigo-200",
    icon: Cpu, color: "text-indigo-500", label: "达到上限",
  },
};

const SubAgentCard = React.memo(function SubAgentCard({ message }: SubAgentCardProps) {
  const isStart = message.msgType === "subagent_start";
  const isProgress = message.msgType === "subagent_progress";
  const isEnd = message.msgType === "subagent_end";

  const name = (message.metadata?.subagent_name as string) || "unknown";
  const instruction = message.metadata?.instruction as string;
  const durationMs = message.metadata?.duration_ms as number;
  const result = message.metadata?.subagent_result as SubAgentResultData | undefined;
  const resultStatus = result?.status || (isStart || isProgress ? "running" : "success");
  const statusStyle = STATUS_STYLE[resultStatus] || STATUS_STYLE.success;
  const StatusIcon = statusStyle.icon;

  // Progress
  const progressPercent = isProgress
    ? message.metadata?.iterations && message.metadata?.max_turns
      ? Math.round(
          ((message.metadata.iterations as number) /
            (message.metadata.max_turns as number)) * 100,
        )
      : 0
    : isEnd
      ? 100
      : 0;

  // Token 汇总
  const totalTokens =
    result?.token_usage_records?.reduce((s, r) => s + (r.total_tokens || 0), 0) ?? 0;

  // ── 点击打开详情 (执行中 / 已完CD成均可点击) ──
  const { selectSubagent, selectedSubagentId } = useChatStore();
  const isSelected = selectedSubagentId === message.id;
  const handleClick = () => {
    selectSubagent(isSelected ? null : message.id);
  };

  return (
    <div className="flex gap-3 animate-fade-in-up">
      {/* Avatar */}
      <div
        className={cn(
          "w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 shadow-sm",
          statusStyle.bg,
        )}
      >
        <Network className={cn("w-4 h-4", statusStyle.color)} />
      </div>

      {/* Card */}
      <div
        onClick={handleClick}
        className={cn(
          "max-w-[75%] min-w-[200px] border rounded-xl px-3.5 py-2.5 text-xs transition-all",
          "cursor-pointer hover:shadow-md hover:border-slate-300 active:scale-[0.99]",
          statusStyle.bg,
          statusStyle.border,
          isSelected && "ring-2 ring-slate-400 shadow-md",
        )}
      >
        {/* ── Header row ── */}
        <div className="flex items-center gap-2">
          <StatusIcon
            className={cn(
              "w-3.5 h-3.5 flex-shrink-0",
              resultStatus === "running" && "animate-spin",
              statusStyle.color,
            )}
          />
          <span className="font-medium text-slate-800 truncate">{name}</span>
          <span
            className={cn(
              "text-[9px] px-1.5 py-0.5 rounded-full font-medium ml-auto flex-shrink-0",
              statusStyle.bg,
              statusStyle.color,
            )}
          >
            {statusStyle.label}
          </span>
          {durationMs != null ? (
            <span className="text-[10px] text-slate-400 flex items-center gap-1 flex-shrink-0">
              <Clock className="w-3 h-3" />
              {formatDuration(durationMs)}
            </span>
          ) : null}
          {isEnd && (
            <ChevronRight className="w-3.5 h-3.5 text-slate-400 flex-shrink-0" />
          )}
        </div>

        {/* ── Instruction (truncated) ── */}
        {instruction && (
          <p className="text-[10px] text-slate-500 mt-1 truncate">📋 {instruction}</p>
        )}

        {/* ── Progress bar ── */}
        {(isStart || isProgress) && (
          <div className="mt-2">
            <div className="flex items-center justify-between text-[9px] text-slate-400 mb-1">
              <span>
                {message.metadata?.iterations || 0}/{message.metadata?.max_turns || "?"} turns
              </span>
              {isProgress && <span>{progressPercent}%</span>}
            </div>
            <div className="w-full h-1 bg-white/60 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-400 rounded-full transition-all duration-500"
                style={{ width: `${Math.min(progressPercent, 100)}%` }}
              />
            </div>
          </div>
        )}

        {/* ── Footer stats (end only) ── */}
        {isEnd && (
          <div className="flex items-center gap-3 mt-1.5 text-[9px] text-slate-400">
            {result?.iterations ? <span>🔄 {result.iterations} turns</span> : null}
            {totalTokens > 0 ? <span>🪙 {(totalTokens / 1000).toFixed(1)}k</span> : null}
            {result?.error ? (
              <span className="text-red-400 truncate">⚠️ {result.error.slice(0, 50)}</span>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
});

export default SubAgentCard;
