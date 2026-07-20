"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { X, Clock, CalendarClock, Bot, Users, ChevronDown, ChevronUp, AlertTriangle } from "lucide-react";
import { agentsAPI, projectsAPI, scheduledTasksAPI } from "@/lib/api-client";
import { AgentListItem, Project, ScheduledTask } from "@/lib/types";
import { cn, formatDateTimeFull } from "@/lib/utils";

const CRON_TEMPLATES = [
  { label: "每分钟", value: "* * * * *" },
  { label: "每小时整点", value: "0 * * * *" },
  { label: "每天 9:00", value: "0 9 * * *" },
  { label: "每天 18:00", value: "0 18 * * *" },
  { label: "工作日 9:00", value: "0 9 * * 1-5" },
  { label: "每周一 9:00", value: "0 9 * * 1" },
];

const TIMEZONE_OPTIONS = [
  "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore", "UTC",
  "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles",
];

/** ISO(UTC naive/aware) → datetime-local 输入框值（本地时区） */
function toLocalInputValue(iso: string | null | undefined): string {
  if (!iso) return "";
  const normalized = iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z";
  const d = new Date(normalized);
  if (isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

interface Props {
  open: boolean;
  /** null = 创建模式 */
  task: ScheduledTask | null;
  onClose: () => void;
  onSaved: () => void;
}

export default function TaskFormDialog({ open, task, onClose, onSaved }: Props) {
  const isEdit = task !== null;

  // ── 表单字段 ──
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [scheduleType, setScheduleType] = useState<"cron" | "once">("cron");
  const [cronExpr, setCronExpr] = useState("0 9 * * *");
  const [runAt, setRunAt] = useState("");
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [mode, setMode] = useState<"single" | "team">("single");
  const [projectId, setProjectId] = useState("");
  const [agentName, setAgentName] = useState("");
  const [threadStrategy, setThreadStrategy] = useState<"new" | "fixed">("new");
  const [expiresAt, setExpiresAt] = useState("");
  const [allowSilent, setAllowSilent] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  // ── 辅助数据 ──
  const [projects, setProjects] = useState<Project[]>([]);
  const [agents, setAgents] = useState<AgentListItem[]>([]);
  const [previewTimes, setPreviewTimes] = useState<string[]>([]);
  const [previewError, setPreviewError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  // 记录初始值，编辑时只提交变更的调度字段（避免无谓重置 next_run_at）
  const initialRef = useRef({ cronExpr: "", runAt: "", timezone: "" });

  // 打开时初始化表单
  useEffect(() => {
    if (!open) return;
    setError("");
    setSubmitting(false);
    setShowAdvanced(false);
    if (task) {
      setName(task.name);
      setPrompt(task.prompt);
      setScheduleType(task.recurring ? "cron" : "once");
      setCronExpr(task.cron_expr || "0 9 * * *");
      setRunAt(toLocalInputValue(task.next_run_at));
      setTimezone(task.timezone);
      setMode(task.mode);
      setProjectId(task.project_id || "");
      setAgentName(task.agent_name || "");
      setThreadStrategy(task.thread_strategy);
      setExpiresAt(toLocalInputValue(task.expires_at));
      setAllowSilent(task.allow_silent);
      initialRef.current = {
        cronExpr: task.cron_expr || "",
        runAt: toLocalInputValue(task.next_run_at),
        timezone: task.timezone,
      };
    } else {
      setName("");
      setPrompt("");
      setScheduleType("cron");
      setCronExpr("0 9 * * *");
      setRunAt("");
      setTimezone("Asia/Shanghai");
      setMode("single");
      setProjectId("");
      setAgentName("");
      setThreadStrategy("new");
      setExpiresAt("");
      setAllowSilent(false);
      initialRef.current = { cronExpr: "", runAt: "", timezone: "" };
    }
    agentsAPI.list().then(({ data }) => setAgents(data.agents || [])).catch(() => {});
    projectsAPI.list().then(({ data }) => setProjects(data.projects || [])).catch(() => {});
  }, [open, task]);

  // cron 实时预览（防抖 500ms）— 让用户在提交前看清未来触发时间
  useEffect(() => {
    if (!open || scheduleType !== "cron" || !cronExpr.trim()) {
      setPreviewTimes([]);
      setPreviewError("");
      return;
    }
    const timer = setTimeout(async () => {
      try {
        const { data } = await scheduledTasksAPI.preview(cronExpr.trim(), timezone, 5);
        setPreviewTimes(data.times || []);
        setPreviewError("");
      } catch (err: any) {
        setPreviewTimes([]);
        setPreviewError(err?.response?.data?.detail || "cron 表达式无效");
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [open, scheduleType, cronExpr, timezone]);

  const matchedTemplate = useMemo(
    () => CRON_TEMPLATES.find((t) => t.value === cronExpr.trim())?.label,
    [cronExpr]
  );

  async function handleSubmit() {
    setError("");
    if (!name.trim()) return setError("请填写任务名称");
    if (!prompt.trim()) return setError("请填写执行指令");
    if (scheduleType === "cron" && !cronExpr.trim()) return setError("请填写 cron 表达式");
    if (scheduleType === "once" && !runAt) return setError("请选择执行时间");
    if (scheduleType === "cron" && previewError) return setError("cron 表达式无效，请修正");
    if (mode === "team" && !projectId) return setError("团队模式需要选择项目");

    setSubmitting(true);
    try {
      const payload: Record<string, unknown> = {
        name: name.trim(),
        prompt: prompt.trim(),
        mode,
        project_id: mode === "team" ? projectId : null,
        agent_name: agentName || null,
        thread_strategy: threadStrategy,
        allow_silent: allowSilent,
      };

      if (!isEdit) {
        // 创建：全量提交
        Object.assign(payload, {
          timezone,
          expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        });
        if (scheduleType === "cron") payload.cron_expr = cronExpr.trim();
        else payload.run_at = new Date(runAt).toISOString();
        await scheduledTasksAPI.create(payload);
      } else {
        // 编辑：仅提交变更的调度字段（避免无谓重置 next_run_at）
        const init = initialRef.current;
        const typeChanged = (scheduleType === "cron") !== task!.recurring;
        if (timezone !== init.timezone) payload.timezone = timezone;
        if (scheduleType === "cron" && (typeChanged || cronExpr.trim() !== init.cronExpr)) {
          payload.cron_expr = cronExpr.trim();
        }
        if (scheduleType === "once" && (typeChanged || runAt !== init.runAt)) {
          payload.run_at = new Date(runAt).toISOString();
        }
        payload.expires_at = expiresAt ? new Date(expiresAt).toISOString() : null;
        await scheduledTasksAPI.update(task!.id, payload);
      }
      onSaved();
      onClose();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "保存失败，请检查输入");
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-[2px]" onClick={onClose}>
      <div
        className="w-[560px] max-h-[85vh] overflow-y-auto bg-white rounded-2xl shadow-xl animate-fade-in"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <h3 className="text-base font-semibold text-slate-900">
            {isEdit ? "编辑定时任务" : "新建定时任务"}
          </h3>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-slate-100 text-slate-400">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-6 py-5 space-y-6">
          {/* ── 基本信息 ── */}
          <section className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">
                任务名称 <span className="text-red-400">*</span>
              </label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="例如：每日晨报"
                maxLength={100}
                className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">
                执行指令 <span className="text-red-400">*</span>
              </label>
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="到点后发送给 Agent 的消息，例如：汇总昨天的 git 提交，生成一份开发日报"
                rows={3}
                className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus resize-none"
              />
              <p className="text-[10px] text-slate-400 mt-1">
                定时执行为无人值守模式：指令需自包含，Agent 无法向你追问澄清
              </p>
            </div>
          </section>

          {/* ── 调度方式 ── */}
          <section>
            <label className="block text-sm font-medium text-slate-700 mb-2">调度方式</label>
            <div className="flex gap-1 p-1 bg-slate-100 rounded-xl w-fit mb-4">
              {([
                { v: "cron", label: "周期任务", icon: Clock },
                { v: "once", label: "一次性", icon: CalendarClock },
              ] as const).map((opt) => (
                <button
                  key={opt.v}
                  onClick={() => setScheduleType(opt.v)}
                  className={cn(
                    "flex items-center gap-1.5 px-3.5 py-1.5 text-xs rounded-lg transition-all",
                    scheduleType === opt.v ? "bg-white text-slate-900 font-medium shadow-sm" : "text-slate-500 hover:text-slate-700"
                  )}
                >
                  <opt.icon className="w-3.5 h-3.5" /> {opt.label}
                </button>
              ))}
            </div>

            {scheduleType === "cron" ? (
              <div className="space-y-3">
                {/* 常用模板 */}
                <div className="flex flex-wrap gap-1.5">
                  {CRON_TEMPLATES.map((t) => (
                    <button
                      key={t.value}
                      onClick={() => setCronExpr(t.value)}
                      className={cn(
                        "px-2.5 py-1 text-[11px] rounded-lg border transition-all",
                        cronExpr.trim() === t.value
                          ? "border-slate-900 bg-slate-900 text-white"
                          : "border-slate-200 text-slate-500 hover:border-slate-300 hover:text-slate-700"
                      )}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
                <div className="flex gap-2">
                  <div className="flex-1">
                    <input
                      value={cronExpr}
                      onChange={(e) => setCronExpr(e.target.value)}
                      placeholder="分 时 日 月 星期，如 0 9 * * *"
                      className={cn(
                        "w-full px-3.5 py-2.5 text-sm border rounded-xl input-focus font-mono",
                        previewError ? "border-red-300 bg-red-50" : "border-slate-200"
                      )}
                    />
                  </div>
                  <select
                    value={timezone}
                    onChange={(e) => setTimezone(e.target.value)}
                    className="px-3 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
                  >
                    {TIMEZONE_OPTIONS.map((tz) => (
                      <option key={tz} value={tz}>{tz}</option>
                    ))}
                  </select>
                </div>
                {/* 实时预览 — 提交前看清未来触发时间 */}
                <div className="p-3 bg-slate-50 border border-slate-200 rounded-xl">
                  {previewError ? (
                    <p className="text-xs text-red-500 flex items-center gap-1.5">
                      <AlertTriangle className="w-3.5 h-3.5" /> {previewError}
                    </p>
                  ) : previewTimes.length > 0 ? (
                    <div>
                      <p className="text-[10px] text-slate-400 mb-1.5">
                        未来 {previewTimes.length} 次触发（{timezone}）
                        {matchedTemplate && <span className="ml-1 text-slate-500">· {matchedTemplate}</span>}
                      </p>
                      <div className="flex flex-wrap gap-x-4 gap-y-1">
                        {previewTimes.map((t, i) => (
                          <span key={i} className="text-xs text-slate-600 font-mono">
                            {formatDateTimeFull(t)}
                          </span>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <p className="text-xs text-slate-400">输入 cron 表达式后显示未来触发时间</p>
                  )}
                </div>
              </div>
            ) : (
              <div>
                <input
                  type="datetime-local"
                  value={runAt}
                  onChange={(e) => setRunAt(e.target.value)}
                  className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
                />
                <p className="text-[10px] text-slate-400 mt-1">执行一次后任务自动停用</p>
              </div>
            )}
          </section>

          {/* ── 执行方式 ── */}
          <section>
            <label className="block text-sm font-medium text-slate-700 mb-2">执行方式</label>
            <div className="flex gap-1 p-1 bg-slate-100 rounded-xl w-fit mb-4">
              {([
                { v: "single", label: "单 Agent", icon: Bot },
                { v: "team", label: "Agent 团队", icon: Users },
              ] as const).map((opt) => (
                <button
                  key={opt.v}
                  onClick={() => setMode(opt.v)}
                  className={cn(
                    "flex items-center gap-1.5 px-3.5 py-1.5 text-xs rounded-lg transition-all",
                    mode === opt.v ? "bg-white text-slate-900 font-medium shadow-sm" : "text-slate-500 hover:text-slate-700"
                  )}
                >
                  <opt.icon className="w-3.5 h-3.5" /> {opt.label}
                </button>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-slate-500 mb-1.5">执行 Agent</label>
                <select
                  value={agentName}
                  onChange={(e) => setAgentName(e.target.value)}
                  className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
                >
                  <option value="">默认 Agent</option>
                  {agents.map((a) => (
                    <option key={a.name} value={a.name}>{a.display_name || a.name}</option>
                  ))}
                </select>
              </div>
              {mode === "team" && (
                <div>
                  <label className="block text-xs text-slate-500 mb-1.5">
                    项目 <span className="text-red-400">*</span>
                  </label>
                  <select
                    value={projectId}
                    onChange={(e) => setProjectId(e.target.value)}
                    className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
                  >
                    <option value="">选择项目</option>
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </div>
              )}
              <div>
                <label className="block text-xs text-slate-500 mb-1.5">会话策略</label>
                <select
                  value={threadStrategy}
                  onChange={(e) => setThreadStrategy(e.target.value as "new" | "fixed")}
                  className="w-full px-3 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
                >
                  <option value="new">每次新建会话</option>
                  <option value="fixed">固定会话（上下文连续）</option>
                </select>
              </div>
            </div>
            <p className="text-[10px] text-slate-400 mt-1.5">
              {threadStrategy === "new"
                ? "每次执行使用全新会话，互不干扰"
                : "所有执行共用同一会话，Agent 能看到上次结果（适合跟进类任务）"}
            </p>
          </section>

          {/* ── 高级选项 ── */}
          <section>
            <button
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="flex items-center gap-1 text-xs text-slate-400 hover:text-slate-600"
            >
              {showAdvanced ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
              高级选项
            </button>
            {showAdvanced && (
              <div className="mt-3 space-y-4">
                <div>
                  <label className="block text-xs text-slate-500 mb-1.5">过期时间（可选）</label>
                  <input
                    type="datetime-local"
                    value={expiresAt}
                    onChange={(e) => setExpiresAt(e.target.value)}
                    className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
                  />
                  <p className="text-[10px] text-slate-400 mt-1">到期后任务自动停用，留空表示永不过期</p>
                </div>
                <label className="flex items-start gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={allowSilent}
                    onChange={(e) => setAllowSilent(e.target.checked)}
                    className="w-3.5 h-3.5 rounded border-slate-300 mt-0.5"
                  />
                  <span>
                    <span className="text-sm text-slate-600">静默模式</span>
                    <p className="text-[10px] text-slate-400 mt-0.5">
                      Agent 判断"无事可报"时不写入会话、不产生未读提醒（适合高频巡检类任务）
                    </p>
                  </span>
                </label>
              </div>
            )}
          </section>

          {/* 错误提示 */}
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-xl text-xs text-red-600 flex items-center gap-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-2 px-6 py-4 border-t border-slate-100">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 rounded-xl transition-colors"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="px-5 py-2 text-sm bg-slate-900 text-white rounded-xl hover:bg-slate-800 transition-colors disabled:opacity-50 font-medium"
          >
            {submitting ? "保存中..." : isEdit ? "保存修改" : "创建任务"}
          </button>
        </div>
      </div>
    </div>
  );
}
