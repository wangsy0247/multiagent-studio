"use client";

import { useEffect, useRef, useState } from "react";
import {
  Bot, CalendarClock, ChevronDown, ChevronUp, Clock, History,
  MoonStar, Pencil, Play, Trash2, Users,
} from "lucide-react";
import { scheduledTasksAPI } from "@/lib/api-client";
import { useUnreadStore } from "@/lib/unread-store";
import { ScheduledTask } from "@/lib/types";
import { cn, formatDateTime, formatRelativeTime } from "@/lib/utils";
import StatusBadge from "./StatusBadge";
import TaskRunHistory from "./TaskRunHistory";

interface Props {
  task: ScheduledTask;
  onEdit: (task: ScheduledTask) => void;
  /** 任何变更后回调，让父组件刷新列表 */
  onChanged: () => void;
}

export default function ScheduledTaskCard({ task, onEdit, onChanged }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [acting, setActing] = useState(false);
  const [feedback, setFeedback] = useState("");
  const unreadCount = useUnreadStore((s) => s.byTask[task.id] || 0);
  // 记录 setTimeout id，卸载时清理，避免回调在组件卸载后触发
  const feedbackTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (feedbackTimer.current) clearTimeout(feedbackTimer.current);
      if (confirmTimer.current) clearTimeout(confirmTimer.current);
    };
  }, []);

  async function toggleEnabled() {
    setActing(true);
    try {
      await scheduledTasksAPI.update(task.id, { enabled: !task.enabled });
      onChanged();
    } finally {
      setActing(false);
    }
  }

  async function handleTrigger() {
    setActing(true);
    try {
      await scheduledTasksAPI.trigger(task.id);
      setFeedback("已触发，将在约 15 秒内开始执行");
      feedbackTimer.current = setTimeout(() => {
        setFeedback("");
        onChanged();
      }, 2000);
    } finally {
      setActing(false);
    }
  }

  async function handleDelete() {
    if (!confirmDelete) {
      setConfirmDelete(true);
      confirmTimer.current = setTimeout(() => setConfirmDelete(false), 3000);
      return;
    }
    setActing(true);
    try {
      await scheduledTasksAPI.delete(task.id);
      onChanged();
    } finally {
      setActing(false);
      setConfirmDelete(false);
    }
  }

  return (
    <div className={cn(
      "bg-white border rounded-2xl transition-all",
      task.enabled ? "border-slate-200" : "border-slate-200/60 opacity-70"
    )}>
      <div className="px-5 py-4">
        <div className="flex items-start gap-4">
          {/* 启用开关 */}
          <button
            onClick={toggleEnabled}
            disabled={acting}
            title={task.enabled ? "点击停用" : "点击启用"}
            className={cn(
              "mt-1 w-9 h-5 rounded-full relative transition-colors shrink-0",
              task.enabled ? "bg-slate-900" : "bg-slate-200"
            )}
          >
            <span className={cn(
              "absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all",
              task.enabled ? "left-[18px]" : "left-0.5"
            )} />
          </button>

          {/* 主体信息 */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-semibold text-slate-900">{task.name}</h3>
              {task.recurring ? (
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-100 text-[10px] font-mono text-slate-600">
                  <Clock className="w-3 h-3" /> {task.cron_expr}
                  <span className="font-sans text-slate-400">({task.timezone})</span>
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-100 text-[10px] text-slate-600">
                  <CalendarClock className="w-3 h-3" /> 一次性
                </span>
              )}
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-100 text-[10px] text-slate-600">
                {task.mode === "team" ? <Users className="w-3 h-3" /> : <Bot className="w-3 h-3" />}
                {task.mode === "team" ? "团队" : "单 Agent"}
              </span>
              {task.created_by === "agent" && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-violet-50 text-[10px] text-violet-600 border border-violet-200"
                  title="由 Agent 在对话中创建"
                >
                  <Bot className="w-3 h-3" /> Agent 创建
                </span>
              )}
              {task.allow_silent && (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-slate-100 text-[10px] text-slate-500"
                  title="静默模式：无事可报时不写会话、不提醒"
                >
                  <MoonStar className="w-3 h-3" /> 静默
                </span>
              )}
            </div>
            <p className="mt-1 text-xs text-slate-400 line-clamp-1">{task.prompt}</p>

            {/* 追踪信息行 */}
            <div className="mt-2 flex items-center gap-4 text-xs flex-wrap">
              <span className="text-slate-500">
                下次执行：{task.enabled && task.next_run_at ? (
                  <>
                    <span className="text-slate-700 font-medium">{formatRelativeTime(task.next_run_at)}</span>
                    <span className="text-slate-400 ml-1">({formatDateTime(task.next_run_at)})</span>
                  </>
                ) : (
                  <span className="text-slate-300">{task.enabled ? "—" : "已停用"}</span>
                )}
              </span>
              {task.last_run_at && (
                <span className="flex items-center gap-1.5 text-slate-500">
                  上次：{formatRelativeTime(task.last_run_at)}
                  <StatusBadge status={task.last_status} />
                </span>
              )}
            </div>
            {task.last_error && (
              <p className="mt-1 text-[11px] text-red-400 line-clamp-1">{task.last_error}</p>
            )}
            {feedback && (
              <p className="mt-1 text-[11px] text-green-500">{feedback}</p>
            )}
          </div>

          {/* 操作按钮 */}
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={handleTrigger}
              disabled={acting}
              title="立即运行一次"
              className="p-2 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors disabled:opacity-40"
            >
              <Play className="w-4 h-4" />
            </button>
            <button
              onClick={() => onEdit(task)}
              title="编辑"
              className="p-2 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors"
            >
              <Pencil className="w-4 h-4" />
            </button>
            <button
              onClick={handleDelete}
              disabled={acting}
              title={confirmDelete ? "再次点击确认删除" : "删除"}
              className={cn(
                "p-2 rounded-lg transition-colors disabled:opacity-40",
                confirmDelete
                  ? "text-white bg-red-500 hover:bg-red-600 text-[10px] font-medium px-2"
                  : "text-slate-400 hover:text-red-500 hover:bg-red-50"
              )}
            >
              {confirmDelete ? "确认?" : <Trash2 className="w-4 h-4" />}
            </button>
            <button
              onClick={() => setExpanded(!expanded)}
              title="执行历史"
              className={cn(
                "relative p-2 rounded-lg transition-colors",
                expanded ? "text-slate-700 bg-slate-100" : "text-slate-400 hover:text-slate-700 hover:bg-slate-100"
              )}
            >
              {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
              {unreadCount > 0 && (
                <span className="absolute -top-1 -right-1 min-w-[16px] h-4 px-0.5 rounded-full bg-red-500 text-white text-[9px] font-medium flex items-center justify-center">
                  {unreadCount > 99 ? "99+" : unreadCount}
                </span>
              )}
            </button>
          </div>
        </div>
      </div>

      {/* 执行历史（可追踪） */}
      {expanded && (
        <div className="px-5 py-4 border-t border-slate-100 bg-slate-50/50 rounded-b-2xl">
          <div className="flex items-center gap-1.5 mb-3 text-xs text-slate-400">
            <History className="w-3.5 h-3.5" /> 执行历史
          </div>
          <TaskRunHistory taskId={task.id} />
        </div>
      )}
    </div>
  );
}
