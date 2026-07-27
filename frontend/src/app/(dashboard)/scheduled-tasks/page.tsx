"use client";

import { useCallback, useEffect, useState } from "react";
import { CalendarClock, Plus, RefreshCw } from "lucide-react";
import { scheduledTasksAPI } from "@/lib/api-client";
import { useUnreadStore } from "@/lib/unread-store";
import { ScheduledTask } from "@/lib/types";
import ScheduledTaskCard from "@/components/scheduled-tasks/ScheduledTaskCard";
import TaskFormDialog from "@/components/scheduled-tasks/TaskFormDialog";

const POLL_INTERVAL = 30_000; // 静默轮询，追踪任务状态变化

export default function ScheduledTasksPage() {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null);
  const refreshUnread = useUnreadStore((s) => s.refresh);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const { data } = await scheduledTasksAPI.list();
      setTasks(data || []);
      setError("");
      refreshUnread();
    } catch (err: any) {
      if (!silent) setError(err?.response?.data?.detail || "加载定时任务失败");
    } finally {
      setLoading(false);
    }
  }, [refreshUnread]);

  useEffect(() => {
    load();
    const timer = setInterval(() => load(true), POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [load]);

  function openCreate() {
    setEditingTask(null);
    setDialogOpen(true);
  }

  function openEdit(task: ScheduledTask) {
    setEditingTask(task);
    setDialogOpen(true);
  }

  const enabledCount = tasks.filter((t) => t.enabled).length;

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-3xl mx-auto p-8">
        {/* Header */}
        <div className="flex items-start justify-between mb-6">
          <div>
            <h2 className="text-xl font-bold font-display text-slate-900">定时任务</h2>
            <p className="text-xs text-slate-400 mt-1">
              按 cron 时间表自动执行 Agent 任务，结果写入会话，随时回看
            </p>
          </div>
          <button
            onClick={openCreate}
            className="flex items-center gap-2 px-4 py-2.5 bg-slate-900 text-white rounded-xl text-sm hover:bg-slate-800 transition-colors shadow-sm font-medium"
          >
            <Plus className="w-4 h-4" /> 新建任务
          </button>
        </div>

        {/* 统计行 */}
        {tasks.length > 0 && (
          <div className="flex items-center gap-3 mb-4 text-xs text-slate-400">
            <span>共 {tasks.length} 个任务</span>
            <span className="w-1 h-1 rounded-full bg-slate-300" />
            <span>{enabledCount} 个启用中</span>
            <button
              onClick={() => load()}
              className="ml-auto flex items-center gap-1 hover:text-slate-600 transition-colors"
              title="刷新"
            >
              <RefreshCw className="w-3 h-3" /> 刷新
            </button>
          </div>
        )}

        {/* 内容区 */}
        {loading ? (
          <div className="flex justify-center py-20">
            <div className="w-5 h-5 border-2 border-slate-400 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : error ? (
          <div className="text-center py-20">
            <p className="text-sm text-red-500">{error}</p>
            <button onClick={() => load()} className="mt-3 text-xs text-slate-400 hover:text-slate-600">
              重试
            </button>
          </div>
        ) : tasks.length === 0 ? (
          <div className="text-center py-20">
            <CalendarClock className="w-12 h-12 text-slate-200 mx-auto mb-4" />
            <p className="text-sm text-slate-500 mb-1">还没有定时任务</p>
            <p className="text-xs text-slate-400 mb-6">
              创建后，Agent 会按你设定的时间自动执行，例如「每天早上 9 点生成工作日报」
            </p>
            <button
              onClick={openCreate}
              className="inline-flex items-center gap-2 px-4 py-2.5 bg-slate-900 text-white rounded-xl text-sm hover:bg-slate-800 transition-colors font-medium"
            >
              <Plus className="w-4 h-4" /> 创建第一个任务
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {tasks.map((task) => (
              <ScheduledTaskCard
                key={task.id}
                task={task}
                onEdit={openEdit}
                onChanged={() => load(true)}
              />
            ))}
          </div>
        )}
      </div>

      <TaskFormDialog
        open={dialogOpen}
        task={editingTask}
        onClose={() => setDialogOpen(false)}
        onSaved={() => load(true)}
      />
    </div>
  );
}
