"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, MessageCircle, CheckSquare, Users, Plus, X } from "lucide-react";
import { projectsAPI, agentsAPI } from "@/lib/api-client";
import { useProjectStore } from "@/lib/project-store";
import { useTeamStore } from "@/lib/team-store";
import ChatPanel from "@/components/chat/ChatPanel";
import type { ProjectTaskStatus } from "@/lib/types";

interface Project { id: string; name: string; description: string; members: string[]; thread_count: number; task_count: number; }
interface Task { id: string; title: string; description: string; status: string; assigned_agent: string | null; priority: string; }
interface Agent { name: string; display_name: string; description: string; }

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
  const { projectThreads, projectAgents, fetchProjectThreads } = useProjectStore();

  useEffect(() => {
    useProjectStore.getState().fetchProjectAgents(
      useProjectStore.getState().currentProject?.members || [],
    );
    fetchProjectThreads(projectId);
  }, [projectId]);

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

      {/* 右侧聊天面板 */}
      <div className="flex-1 min-w-0">
        {selectedThreadId || selectedAgentName ? (
          <ChatPanel
            threadId={selectedThreadId || undefined}
            projectId={projectId}
            agentName={selectedAgentName || undefined}
            mode={mode}
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
  );
}

// ══════════════════════════════════════════════════════════════════════════════
// Tasks Tab — 7 列看板 + Agent 分配
// ══════════════════════════════════════════════════════════════════════════════

function TasksTab({ projectId }: { projectId: string }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newAssignedAgent, setNewAssignedAgent] = useState("");
  // 从 team-store 读取运行时状态
  const teamTasks = useTeamStore((state) => state.tasks);

  useEffect(() => { loadTasks(); loadAgents(); }, [projectId]);

  async function loadTasks() {
    try {
      const { data } = await projectsAPI.listTasks(projectId);
      setTasks(data.tasks || []);
    } catch (err) { console.error(err); }
    finally { setLoading(false); }
  }

  async function loadAgents() {
    try {
      const { data } = await agentsAPI.list();
      setAgents(data.agents || []);
    } catch (err) { console.error(err); }
  }

  async function handleCreate() {
    if (!newTitle.trim()) return;
    try {
      await projectsAPI.createTask(projectId, {
        title: newTitle,
        user_id: "default",
        assigned_agent: newAssignedAgent || undefined,
      });
      setNewTitle(""); setNewAssignedAgent(""); setShowCreate(false);
      loadTasks();
    } catch (err) { console.error(err); }
  }

  async function handleStatusChange(taskId: string, newStatus: string) {
    try {
      await projectsAPI.updateTask(projectId, taskId, { status: newStatus, user_id: "default" });
      loadTasks();
    } catch (err) { console.error(err); }
  }

  async function handleDelete(taskId: string) {
    try { await projectsAPI.deleteTask(projectId, taskId, "default"); loadTasks(); }
    catch (err) { console.error(err); }
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

  const columns: { key: ProjectTaskStatus; label: string; color: string }[] = [
    { key: "todo", label: "待办", color: "bg-slate-100" },
    { key: "in_progress", label: "进行中", color: "bg-blue-100" },
    { key: "in_review", label: "审阅中", color: "bg-yellow-100" },
    { key: "completed", label: "已完成", color: "bg-green-100" },
    { key: "failed", label: "失败", color: "bg-red-100" },
    { key: "rejected", label: "已驳回", color: "bg-orange-100" },
    { key: "merged", label: "已合并", color: "bg-purple-100" },
  ];

  return (
    <div className="h-full p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-slate-700">任务面板</h3>
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
              {col.label} ({allTasks.filter((t) => t.status === col.key).length})
            </div>
            <div className="flex-1 overflow-y-auto space-y-1.5">
              {allTasks.filter((t) => t.status === col.key).map((task) => (
                <div key={task.id} className="border border-slate-200 rounded-lg p-2 bg-white text-xs group">
                  <p className="text-slate-800 font-medium truncate">{task.title}</p>
                  {task.assigned_agent && (
                    <p className="text-slate-400 mt-0.5">@{task.assigned_agent}</p>
                  )}
                  <div className="flex items-center gap-0.5 mt-1.5 opacity-0 group-hover:opacity-100 transition-opacity flex-wrap">
                    {columns.filter((c) => c.key !== col.key).slice(0, 4).map((c) => (
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
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  // 从 team-store 读取成员运行时状态
  const memberStatuses = useTeamStore((state) => state.members);

  useEffect(() => { loadAgents(); }, []);

  async function loadAgents() {
    try {
      const { data } = await agentsAPI.list();
      setAgents(data.agents || []);
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

  const statusLabels: Record<string, string> = { idle: "空闲", busy: "执行中", done: "完成", failed: "失败" };
  const statusColors: Record<string, string> = { idle: "bg-slate-300", busy: "bg-blue-500", done: "bg-green-500", failed: "bg-red-500" };

  return (
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
        <div className="space-y-2">
          {memberAgents.map((a) => {
            const runtime = memberStatuses.find((m) => m.agent_name === a.name);
            const status = runtime?.status || "idle";
            return (
              <div key={a.name} className="flex items-center justify-between border border-slate-200 rounded-lg p-3">
                <div className="flex items-center gap-3">
                  <span className={`w-2.5 h-2.5 rounded-full ${statusColors[status] || "bg-slate-300"} ${status === "busy" ? "animate-pulse" : ""}`} />
                  <div>
                    <p className="text-sm font-medium text-slate-700">
                      🤖 {a.display_name || a.name}
                      <span className="ml-2 text-xs text-slate-400">({statusLabels[status] || status})</span>
                    </p>
                    <p className="text-xs text-slate-400">{a.description?.slice(0, 60)}</p>
                    {runtime?.current_task_title && (
                      <p className="text-xs text-blue-600 mt-0.5">📋 {runtime.current_task_title}</p>
                    )}
                  </div>
                </div>
                <button onClick={() => handleRemove(a.name)} className="text-xs text-red-400 hover:text-red-600 px-2 py-1 rounded hover:bg-red-50">移除</button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
