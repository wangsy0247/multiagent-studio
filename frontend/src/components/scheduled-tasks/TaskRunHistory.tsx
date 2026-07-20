"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ExternalLink, Loader2, MoonStar } from "lucide-react";
import { scheduledTasksAPI } from "@/lib/api-client";
import { useUnreadStore } from "@/lib/unread-store";
import { TaskRun } from "@/lib/types";
import { formatDateTimeFull, formatDuration } from "@/lib/utils";
import StatusBadge from "./StatusBadge";

/** 计算执行耗时（started_at/finished_at 为 UTC naive） */
function runDuration(run: TaskRun): string {
  if (!run.finished_at) return "—";
  const norm = (s: string) => (s.endsWith("Z") || s.includes("+") ? s : s + "Z");
  const ms = new Date(norm(run.finished_at)).getTime() - new Date(norm(run.started_at)).getTime();
  return ms < 0 ? "—" : formatDuration(ms);
}

export default function TaskRunHistory({ taskId }: { taskId: string }) {
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const router = useRouter();
  const refreshUnread = useUnreadStore((s) => s.refresh);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await scheduledTasksAPI.listRuns(taskId, 50);
        if (!cancelled) setRuns(data || []);
        // 用户展开历史即视为已读，清除未读红点
        scheduledTasksAPI.markRunsSeen(taskId).then(() => refreshUnread()).catch(() => {});
      } catch (err: any) {
        if (!cancelled) setError(err?.response?.data?.detail || "加载执行历史失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [taskId, refreshUnread]);

  if (loading) {
    return (
      <div className="flex justify-center py-6">
        <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
      </div>
    );
  }

  if (error) {
    return <p className="py-4 text-center text-xs text-red-500">{error}</p>;
  }

  if (runs.length === 0) {
    return <p className="py-4 text-center text-xs text-slate-400">还没有执行记录</p>;
  }

  return (
    <div className="divide-y divide-slate-100">
      {runs.map((run) => (
        <div key={run.id} className="py-3 first:pt-1 last:pb-0">
          <div className="flex items-center gap-3">
            <StatusBadge status={run.status} />
            <span className="text-xs text-slate-500 font-mono">
              {formatDateTimeFull(run.started_at)}
            </span>
            <span className="text-[10px] text-slate-400">耗时 {runDuration(run)}</span>
            {run.thread_id && (
              <button
                onClick={() => router.push(`/threads/${run.thread_id}`)}
                className="ml-auto flex items-center gap-1 text-[11px] text-blue-500 hover:text-blue-600"
              >
                查看会话 <ExternalLink className="w-3 h-3" />
              </button>
            )}
          </div>
          {run.summary && (
            <p className="mt-1.5 text-xs text-slate-500 line-clamp-2 leading-relaxed">
              {run.summary}
            </p>
          )}
          {run.status === "success" && !run.summary && (
            <p className="mt-1.5 text-[11px] text-slate-400 flex items-center gap-1">
              <MoonStar className="w-3 h-3" /> 静默完成（无事可报）
            </p>
          )}
          {run.error && (
            <p className="mt-1.5 text-xs text-red-500 line-clamp-2 leading-relaxed">
              {run.error}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
