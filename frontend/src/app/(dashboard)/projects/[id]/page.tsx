"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, MessageCircle, CheckSquare, Users, Plus, X, Wrench, Brain, Bot } from "lucide-react";
import { projectsAPI, agentsAPI, threadsAPI } from "@/lib/api-client";
import type { ThreadSummary, AgentLogEntry, AgentCard } from "@/lib/types";
import { useProjectStore } from "@/lib/project-store";
import { useTeamStore } from "@/lib/team-store";
import ChatPanel from "@/components/chat/ChatPanel";
import type { ProjectTaskStatus } from "@/lib/types";

interface Project { id: string; name: string; description: string; members: string[]; thread_count: number; task_count: number; }
interface Task { id: string; title: string; description: string; status: string; assigned_agent: string | null; priority: string; revision_count?: number; review_feedback?: string; output?: string; }
interface Agent { name: string; display_name: string; description: string; }

function ElapsedTimer({ startedAt }: { startedAt: string }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const start = new Date(startedAt).getTime();
    const tick = () => setElapsed(Math.floor((Date.now() - start) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt]);
  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  const display = m > 0 ? `${m}m ${s}s` : `${s}s`;
  return <span className="text-[10px] text-hermes-500 font-mono ml-1">{display}</span>;
}

type TabType = "chat" | "tasks" | "members";

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [tab, setTab] = useState<TabType>("chat");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const { fetchProject } = useProjectStore();
  const { initMembers, reset } = useTeamStore();

  useEffect(() => {
    loadProject();
    return () => { reset(); };
  }, [id]);

  async function loadProject() {
    try {
      const { data } = await projectsAPI.get(id);
      setProject(data);
      // 同步到 project-store，确保 ChatTab 能读取到 currentProject 和 projectAgents
      useProjectStore.setState({ currentProject: data });
      useProjectStore.getState().fetchProjectAgents(data.members || []);
      useProjectStore.getState().fetchProjectThreads(id);
      // 初始化 team-store 成员
      if (data.members?.length > 0) {
        const agentsResp = await agentsAPI.list();
        const allAgents: Agent[] = agentsResp.data.agents || [];
        const displayNames: Record<string, string> = {};
        allAgents.forEach((a: Agent) => { displayNames[a.name] = a.display_name || a.name; });
        initMembers(data.members, displayNames);
      }
    } catch (err) {
      setError("加载项目失败");
    } finally { setLoading(false); }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-6 h-6 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
      </div>
    );
  }

  if (error || !project) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <p className="text-red-600 mb-3">{error || "项目不存在"}</p>
          <button onClick={() => window.history.back()} className="text-sm text-slate-600 underline">返回</button>
        </div>
      </div>
    );
  }

  const tabs = [
    { id: "chat" as TabType, icon: MessageCircle, label: "对话" },
    { id: "tasks" as TabType, icon: CheckSquare, label: "任务面板" },
    { id: "members" as TabType, icon: Users, label: "团队成员" },
  ];

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-slate-200 bg-white flex-shrink-0">
        <button onClick={() => window.history.back()} className="p-1 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100">
          <ArrowLeft className="w-4 h-4" />
        </button>
        <div>
          <h2 className="font-semibold text-slate-900">{project.name}</h2>
          <p className="text-xs text-slate-400">{project.members?.length || 0} 个成员</p>
        </div>
        <div className="flex-1" />
        <div className="flex items-center gap-0.5">
          {tabs.map((t) => {
            const isActive = tab === t.id;
            return (
              <button key={t.id} onClick={() => setTab(t.id)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm transition-all ${isActive ? "bg-slate-100 text-slate-900 font-medium" : "text-slate-500 hover:text-slate-700 hover:bg-slate-50"}`}>
                <t.icon className="w-3.5 h-3.5" /> {t.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {tab === "chat" && <ChatTab projectId={id} />}
        {tab === "tasks" && <TasksTab projectId={id} />}
        {tab === "members" && <MembersTab projectId={id} members={project.members || []} onUpdate={loadProject} />}
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Chat Tab — 线程列表 + 聊天面板
// ══════════════════════════════════════════════════════════════════════════════

function ChatTab({ projectId }: { projectId: string }) {
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [selectedAgentName, setSelectedAgentName] = useState<string | null>(null);
  const [mode, setMode] = useState<"single" | "team">("team");
  // ── Agent 隔离视图状态 ──
  const [viewAgent, setViewAgent] = useState<string | null>(null); // null = "全部" (团队视图)
  const [agentLogEntries, setAgentLogEntries] = useState<AgentLogEntry[]>([]);
  const [agentLogsLoading, setAgentLogsLoading] = useState(false);
  const { projectThreads, projectAgents, fetchProjectThreads } = useProjectStore();

  useEffect(() => {
    useProjectStore.getState().fetchProjectAgents(
      useProjectStore.getState().currentProject?.members || [],
    );
    fetchProjectThreads(projectId);
  }, [projectId]);

  const teamMembers = useTeamStore((s) => s.members);
  // 当前正在查看的 agent 的运行时状态
  const viewedAgentStatus = viewAgent
    ? teamMembers.find((m) => m.agent_name === viewAgent)?.status
    : null;
  const isViewedAgentWorking = viewedAgentStatus === "working";

  // 选中 team thread 时加载 agent 日志列表 (用于 agent 标签栏)
  useEffect(() => {
    if (!selectedThreadId || mode !== "team") return;
    // 重置 agent 视图
    setViewAgent(null);
    setAgentLogEntries([]);
  }, [selectedThreadId, mode]);

  // 切换到具体 agent 时加载该 agent 的日志 + 运行时轮询
  useEffect(() => {
    if (!viewAgent || !selectedThreadId) return;

    let cancelled = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;

    const loadLog = async () => {
      if (cancelled) return;
      try {
        const { data } = await projectsAPI.getAgentLog(projectId, selectedThreadId, viewAgent);
        if (!cancelled) {
          setAgentLogEntries(data.entries || []);
          setAgentLogsLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          console.error("加载 agent 日志失败", err);
          setAgentLogEntries([]);
          setAgentLogsLoading(false);
        }
      }
    };

    // 首次加载
    setAgentLogsLoading(true);
    loadLog();

    // 轮询: agent 正在执行时每 3 秒刷新
    const startPolling = () => {
      // 用闭包捕获的 isViewedAgentWorking (由 useEffect 依赖控制更新)
      if (isViewedAgentWorking) {
        pollTimer = setTimeout(() => {
          loadLog().then(() => {
            if (!cancelled) startPolling();
          });
        }, 3000);
      }
    };
    // 延迟启动轮询 (等首次加载完成)
    const initialPollTimer = setTimeout(() => {
      if (!cancelled) startPolling();
    }, 3500);

    return () => {
      cancelled = true;
      if (pollTimer) clearTimeout(pollTimer);
      clearTimeout(initialPollTimer);
    };
  }, [viewAgent, selectedThreadId, projectId, isViewedAgentWorking]);

  const handleCreateThread = async (agentName?: string, threadMode?: "single" | "team") => {
    const created = await useProjectStore
      .getState()
      .createProjectThread(projectId, {
        title: agentName ? `与 ${agentName} 的对话` : "团队对话",
        agent_name: agentName,
        mode: threadMode || (agentName ? "single" : "team"),
      });
    setSelectedThreadId(created.id);
    setSelectedAgentName(agentName || null);
    setMode(threadMode || (agentName ? "single" : "team"));
  };

  // 当前项目的成员列表 (用于 agent 标签栏)
  const memberNames = useProjectStore.getState().currentProject?.members || [];

  return (
    <div className="flex h-full">
      {/* 左侧边栏 */}
      <div className="w-64 border-r border-slate-200 bg-slate-50 flex flex-col flex-shrink-0">
        <div className="p-3 border-b border-slate-200">
          <h3 className="text-xs font-semibold text-slate-500 uppercase">团队对话</h3>
          <button
            onClick={() => handleCreateThread(undefined, "team")}
            className="mt-2 w-full px-3 py-1.5 bg-slate-900 text-white text-xs rounded-lg hover:bg-slate-800 transition-colors"
          >
            + 新建团队对话
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {projectThreads.length === 0 && (
            <p className="text-xs text-slate-400 text-center py-4">暂无对话</p>
          )}
          {projectThreads.map((t: any) => (
            <button
              key={t.id}
              onClick={() => {
                setSelectedThreadId(t.id);
                setSelectedAgentName(t.agent_name || null);
                setMode((t.mode as any) || "team");
              }}
              className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                selectedThreadId === t.id
                  ? "bg-white shadow-sm border border-slate-200"
                  : "hover:bg-white/50"
              }`}
            >
              <p className="font-medium text-slate-700 truncate">{t.title}</p>
              <p className="text-xs text-slate-400">
                {t.mode === "single" ? `单聊: ${t.agent_name}` : "团队协作"}
              </p>
            </button>
          ))}
        </div>

        {/* 快速单聊 */}
        {projectAgents.length > 0 && (
          <div className="p-3 border-t border-slate-200">
            <h3 className="text-xs font-semibold text-slate-500 uppercase mb-1">快速单聊</h3>
            <div className="space-y-0.5 max-h-40 overflow-y-auto">
              {projectAgents.map((a: Agent) => (
                <button
                  key={a.name}
                  onClick={() => {
                    setSelectedAgentName(a.name);
                    setMode("single");
                    setSelectedThreadId(null);
                  }}
                  className="w-full text-left px-2 py-1.5 text-xs text-slate-600 hover:bg-white rounded transition-colors flex items-center gap-2"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-slate-300 flex-shrink-0" />
                  <span className="truncate">@{a.display_name || a.name}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* 右侧聊天面板 + agent 标签栏 */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* ── Agent 视角切换标签栏 (仅 team thread 时显示) ── */}
        {mode === "team" && selectedThreadId && memberNames.length > 0 && (
          <div className="flex items-center gap-0.5 px-3 py-1.5 border-b border-slate-200 bg-white flex-shrink-0 overflow-x-auto">
            <button
              onClick={() => { setViewAgent(null); setAgentLogEntries([]); }}
              className={`px-3 py-1 text-xs rounded-md whitespace-nowrap transition-colors ${
                viewAgent === null
                  ? "bg-slate-900 text-white"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              全部
            </button>
            <span className="w-px h-3 bg-slate-200 mx-0.5" />
            {memberNames.map((name) => (
              <button
                key={name}
                onClick={() => setViewAgent(viewAgent === name ? null : name)}
                className={`px-3 py-1 text-xs rounded-md whitespace-nowrap transition-colors flex items-center gap-1 ${
                  viewAgent === name
                    ? "bg-slate-900 text-white"
                    : "text-slate-500 hover:bg-slate-100"
                }`}
              >
                {projectAgents.find((a: Agent) => a.name === name)?.display_name || name}
              </button>
            ))}
          </div>
        )}

        {/* ChatPanel */}
        <div className="flex-1 min-h-0">
          {selectedThreadId || selectedAgentName ? (
            <ChatPanel
              threadId={selectedThreadId || undefined}
              projectId={projectId}
              agentName={selectedAgentName || undefined}
              mode={mode}
              viewMode={viewAgent ? "agent" : "team"}
              viewAgentName={viewAgent || undefined}
              agentLogEntries={viewAgent ? agentLogEntries : undefined}
              agentLogsLoading={viewAgent ? agentLogsLoading : false}
              onThreadCreated={(id) => {
                setSelectedThreadId(id);
                fetchProjectThreads(projectId);
              }}
            />
          ) : (
            <div className="flex items-center justify-center h-full text-slate-400 text-sm">
              <div className="text-center">
                <MessageCircle className="w-8 h-8 mx-auto mb-2 text-slate-300" />
                <p>选择一个线程或点击左侧按钮创建新对话</p>
                <p className="text-xs mt-1">支持 Team 协作和单 Agent 对话两种模式</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Tasks Tab — 7 列看板 + Agent 分配
// ══════════════════════════════════════════════════════════════════════════════

function TasksTab({ projectId }: { projectId: string }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [selectedThreadId, setSelectedThreadId] = useState("");
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newAssignedAgent, setNewAssignedAgent] = useState("");
  // 从 team-store 读取运行时状态
  const teamTasks = useTeamStore((state) => state.tasks);

  useEffect(() => { loadAll(); }, [projectId]);
  useEffect(() => { loadTasks(); }, [selectedThreadId]);

  async function loadAll() {
    try {
      await Promise.all([loadTasks(), loadAgents(), loadThreads()]);
    } finally { setLoading(false); }
  }

  async function loadTasks() {
    try {
      const { data } = await projectsAPI.listTasks(projectId, selectedThreadId || undefined);
      setTasks(data.tasks || []);
    } catch (err) { console.error(err); }
  }

  async function loadAgents() {
    try {
      const { data } = await agentsAPI.list();
      setAgents(data.agents || []);
    } catch (err) { console.error(err); }
  }

  async function loadThreads() {
    try {
      const { data } = await threadsAPI.listByProject(projectId);
      setThreads(data.threads || []);
    } catch (err) { console.error(err); }
  }

  async function handleCreate() {
    if (!newTitle.trim()) return;
    try {
      await projectsAPI.createTask(projectId, {
        title: newTitle,
        assigned_agent: newAssignedAgent || undefined,
      }, selectedThreadId || undefined);
      setNewTitle(""); setNewAssignedAgent(""); setShowCreate(false);
      loadTasks();
    } catch (err) { console.error(err); }
  }

  async function handleStatusChange(taskId: string, newStatus: string) {
    try {
      await projectsAPI.updateTask(projectId, taskId, { status: newStatus }, selectedThreadId || undefined);
      loadTasks();
    } catch (err) { console.error(err); }
  }

  async function handleDelete(taskId: string) {
    try {
      await projectsAPI.deleteTask(projectId, taskId, selectedThreadId || undefined);
      loadTasks();
    } catch (err) { console.error(err); }
  }

  if (loading) {
    return <div className="flex items-center justify-center h-full"><div className="w-5 h-5 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" /></div>;
  }

  // 合并持久化任务和 team-store 运行时任务
  const allTasks = [...tasks];
  teamTasks.forEach((rt) => {
    if (!allTasks.some((t) => t.id === rt.id)) {
      allTasks.push({
        id: rt.id,
        title: rt.title,
        description: rt.description || "",
        status: rt.status,
        assigned_agent: rt.assigned_agent || null,
        priority: rt.priority,
      });
    }
  });

  // 7 态看板 — 与后端 TeamTaskStatus 对齐 (含 Review 流程)
  const columns: { key: string; label: string; color: string; aliases?: string[] }[] = [
    { key: "pending", label: "待办", color: "bg-slate-100" },
    { key: "in_progress", label: "进行中", color: "bg-hermes-100" },
    { key: "in_review", label: "审查中", color: "bg-blue-100" },
    { key: "revision_needed", label: "需修改", color: "bg-amber-100" },
    { key: "completed", label: "已完成", color: "bg-green-100", aliases: ["approved"] },
    { key: "failed", label: "失败", color: "bg-red-100" },
    { key: "cancelled", label: "已取消", color: "bg-orange-100" },
  ];

  return (
    <div className="h-full p-4">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-semibold text-slate-700">任务面板</h3>
          <select
            value={selectedThreadId}
            onChange={(e) => setSelectedThreadId(e.target.value)}
            className="text-xs px-2 py-1 border border-slate-200 rounded-lg text-slate-600 bg-white focus:outline-none focus:ring-1 focus:ring-slate-300"
          >
            <option value="">全部会话</option>
            {threads.map((t) => (
              <option key={t.id} value={t.id}>
                {t.title?.slice(0, 30) || t.id.slice(0, 8)}
              </option>
            ))}
          </select>
        </div>
        <button onClick={() => setShowCreate(true)} className="flex items-center gap-1 text-xs px-3 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-800">
          <Plus className="w-3 h-3" /> 添加任务
        </button>
      </div>

      {showCreate && (
        <div className="mb-4 flex gap-2">
          <input type="text" value={newTitle} onChange={(e) => setNewTitle(e.target.value)}
            placeholder="任务标题..." className="flex-1 px-3 py-1.5 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            onKeyDown={(e) => e.key === "Enter" && handleCreate()} />
          <select value={newAssignedAgent} onChange={(e) => setNewAssignedAgent(e.target.value)}
            className="px-3 py-1.5 border rounded-lg text-sm text-slate-600">
            <option value="">未分配</option>
            {agents.map((a) => (
              <option key={a.name} value={a.name}>{a.display_name || a.name}</option>
            ))}
          </select>
          <button onClick={handleCreate} className="px-4 py-1.5 bg-slate-900 text-white text-sm rounded-lg">添加</button>
          <button onClick={() => setShowCreate(false)} className="px-3 py-1.5 text-slate-500 text-sm hover:bg-slate-100 rounded-lg"><X className="w-4 h-4" /></button>
        </div>
      )}

      <div className="grid grid-cols-7 gap-2 h-[calc(100%-4rem)]">
        {columns.map((col) => (
          <div key={col.key} className="flex flex-col">
            <div className={`text-xs font-medium px-2 py-1.5 rounded-lg mb-2 ${col.color} text-slate-700`}>
              {col.label} ({allTasks.filter((t) => t.status === col.key || (col.aliases?.includes(t.status) ?? false)).length})
            </div>
            <div className="flex-1 overflow-y-auto space-y-1.5">
              {allTasks.filter((t) => t.status === col.key || (col.aliases?.includes(t.status) ?? false)).map((task) => (
                <div key={task.id} className="border border-slate-200 rounded-lg p-2 bg-white text-xs group">
                  <p className="text-slate-800 font-medium truncate">{task.title}</p>
                  {task.assigned_agent && (
                    <p className="text-slate-400 mt-0.5 flex items-center gap-1">
                      @{task.assigned_agent}
                      {task.status === "in_progress" && (
                        <span className="text-[10px] px-1 py-0 bg-green-50 text-green-600 rounded" title="已认领并执行中">🎯 执行中</span>
                      )}
                      {task.status === "in_review" && (
                        <span className="text-[10px] px-1 py-0 bg-blue-50 text-blue-600 rounded" title="等待 Lead 审查">👁️ 待审查</span>
                      )}
                      {task.status === "approved" && (
                        <span className="text-[10px] px-1 py-0 bg-green-100 text-green-600 rounded" title="Lead 审查通过">✅ 已审查</span>
                      )}
                    </p>
                  )}
                  {task.status === "revision_needed" && (
                    <div className="mt-1">
                      <span className="text-[10px] px-1 py-0 bg-amber-50 text-amber-600 rounded">
                        ↩️ 第{task.revision_count || 1}次修改
                      </span>
                      {task.review_feedback && (
                        <p className="text-[10px] text-slate-400 mt-0.5 truncate" title={task.review_feedback}>
                          {task.review_feedback.slice(0, 40)}
                        </p>
                      )}
                    </div>
                  )}
                  <div className="flex items-center gap-0.5 mt-1.5 opacity-0 group-hover:opacity-100 transition-opacity flex-wrap">
                    {columns.filter((c) => c.key !== col.key && !(col.aliases?.includes(c.key) ?? false)).slice(0, 5).map((c) => (
                      <button
                        key={c.key}
                        onClick={() => handleStatusChange(task.id, c.key)}
                        className="px-1.5 py-0.5 rounded text-[10px] bg-slate-50 hover:bg-slate-100"
                      >
                        →{c.label}
                      </button>
                    ))}
                    <button onClick={() => handleDelete(task.id)} className="px-1.5 py-0.5 text-[10px] text-red-400 hover:bg-red-50 rounded ml-auto">删</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Members Tab — 成员列表 + 运行时状态
// ══════════════════════════════════════════════════════════════════════════════

function MembersTab({ projectId, members, onUpdate }: { projectId: string; members: string[]; onUpdate: () => void }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [cards, setCards] = useState<Record<string, AgentCard>>({});
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  // 从 team-store 读取成员运行时状态
  const memberStatuses = useTeamStore((state) => state.members);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    try {
      const [agentsRes, cardsRes] = await Promise.all([
        agentsAPI.list(),
        projectsAPI.getAgentCards(projectId).catch(() => ({ data: { cards: {} } })),
      ]);
      setAgents(agentsRes.data.agents || []);
      setCards(cardsRes.data.cards || {});
    } catch (err) { console.error(err); }
    finally { setLoading(false); }
  }

  async function handleAdd(agentName: string) {
    try {
      await projectsAPI.addMember(projectId, agentName);
      onUpdate();
      setShowAdd(false);
    } catch (err) { console.error(err); }
  }

  async function handleRemove(agentName: string) {
    try {
      await projectsAPI.removeMember(projectId, agentName);
      onUpdate();
    } catch (err) { console.error(err); }
  }

  if (loading) {
    return <div className="flex items-center justify-center h-full"><div className="w-5 h-5 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" /></div>;
  }

  const memberAgents = members.map((name) => {
    const agent = agents.find((a) => a.name === name);
    return { name, display_name: agent?.display_name || name, description: agent?.description || "" };
  });
  const availableAgents = agents.filter((a) => !members.includes(a.name));

  /** 成员状态标签 — 对齐后端 TeammateStatus 枚举 */
  const statusLabels: Record<string, string> = {
    spawning: "启动中", idle: "空闲", working: "执行中",
    shutting_down: "关闭中", shutdown: "已关闭", failed: "失败",
  };
  const statusColors: Record<string, string> = {
    spawning: "bg-slate-300 animate-pulse", idle: "bg-slate-300",
    working: "bg-hermes-500 animate-pulse", shutting_down: "bg-amber-400 animate-pulse",
    shutdown: "bg-slate-400", failed: "bg-red-500",
  };

  return (
    <div className="h-full overflow-y-auto">
    <div className="max-w-2xl mx-auto p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-slate-700">团队成员</h3>
        {availableAgents.length > 0 && (
          <button onClick={() => setShowAdd(!showAdd)} className="flex items-center gap-1 text-xs px-3 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-800">
            <Plus className="w-3 h-3" /> 添加 Agent
          </button>
        )}
      </div>

      {showAdd && (
        <div className="mb-4 border border-slate-200 rounded-lg p-3 space-y-1">
          {availableAgents.map((a) => (
            <button key={a.name} onClick={() => handleAdd(a.name)}
              className="w-full text-left px-3 py-2 rounded-lg text-sm hover:bg-slate-50 flex items-center justify-between">
              <span className="font-medium text-slate-700">{a.display_name || a.name}</span>
              <span className="text-xs text-slate-400">{a.description?.slice(0, 40)}</span>
            </button>
          ))}
        </div>
      )}

      {memberAgents.length === 0 ? (
        <p className="text-sm text-slate-400 text-center py-10">暂无团队成员，请添加 Agent</p>
      ) : (
        <div className="space-y-3">
          {memberAgents.map((a) => {
            const runtime = memberStatuses.find((m) => m.agent_name === a.name);
            const status = runtime?.status || "idle";
            const card = cards[a.name];
            return (
              <div key={a.name} className="border border-slate-200 rounded-xl p-4 bg-white transition-all">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-3 flex-1 min-w-0">
                    <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${statusColors[status] || "bg-slate-300"}`} />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-slate-700 flex items-center gap-1.5 flex-wrap">
                        <Bot className="w-4 h-4 text-hermes-500" />
                        {a.display_name || a.name}
                        <span className="text-xs text-slate-400 font-normal">({statusLabels[status] || status})</span>
                        {runtime?.started_at && status === "working" && (
                          <ElapsedTimer startedAt={runtime.started_at} />
                        )}
                      </p>
                      <p className="text-xs text-slate-400 mt-0.5">{a.description?.slice(0, 80)}</p>

                      {/* ── Agent Card 能力展示 ── */}
                      {card && (
                        <div className="mt-3 space-y-2">
                          {/* 模型 + 工具 */}
                          <div className="flex items-center gap-3 text-[10px] text-slate-500 flex-wrap">
                            {card.model && (
                              <span className="flex items-center gap-1 px-1.5 py-0.5 bg-slate-100 rounded">
                                <Brain className="w-3 h-3" />
                                {card.model}
                              </span>
                            )}
                            {card.tools.length > 0 && (
                              <span className="flex items-center gap-1">
                                <Wrench className="w-3 h-3" />
                                <span className="flex gap-1 flex-wrap">
                                  {card.tools.slice(0, 6).map((t) => (
                                    <span key={t} className="px-1.5 py-0.5 bg-hermes-50 text-hermes-600 rounded font-mono">{t}</span>
                                  ))}
                                  {card.tools.length > 6 && (
                                    <span className="text-slate-400">…+{card.tools.length - 6}</span>
                                  )}
                                </span>
                              </span>
                            )}
                          </div>
                          {/* 技能 */}
                          {card.skills.length > 0 && (
                            <div className="flex items-center gap-1 flex-wrap">
                              <span className="text-[10px] text-slate-400 mr-1">技能:</span>
                              {card.skills.map((s) => (
                                <span key={s} className="text-[10px] px-1.5 py-0.5 bg-purple-50 text-purple-600 rounded">{s}</span>
                              ))}
                            </div>
                          )}
                        </div>
                      )}

                      {runtime?.current_task_title && (
                        <p className="text-xs text-hermes-600 mt-1.5">📋 {runtime.current_task_title}</p>
                      )}
                    </div>
                  </div>
                  <button onClick={() => handleRemove(a.name)} className="text-xs text-red-400 hover:text-red-600 px-2 py-1 rounded hover:bg-red-50 shrink-0">移除</button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
    </div>
  );
}
