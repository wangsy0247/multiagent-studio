"use client";

import { useEffect, useState, lazy, Suspense } from "react";
import { useParams } from "next/navigation";
import { ArrowLeft, MessageCircle, CheckSquare, Users, Plus, X } from "lucide-react";
import { projectsAPI, agentsAPI } from "@/lib/api-client";

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

  useEffect(() => { loadProject(); }, [id]);

  async function loadProject() {
    try {
      const { data } = await projectsAPI.get(id);
      setProject(data);
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
        {/* Tabs */}
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

// ── Chat Tab ──────────────────────────────────────────────────────────────

function ChatTab({ projectId }: { projectId: string }) {
  return (
    <div className="flex items-center justify-center h-full text-slate-400 text-sm">
      <div className="text-center">
        <MessageCircle className="w-8 h-8 mx-auto mb-2 text-slate-300" />
        <p>选择一个线程或创建新的对话</p>
        <p className="text-xs mt-1">对话功能将在此集成</p>
      </div>
    </div>
  );
}

// ── Tasks Tab ──────────────────────────────────────────────────────────────

function TasksTab({ projectId }: { projectId: string }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState("");

  useEffect(() => { loadTasks(); }, [projectId]);

  async function loadTasks() {
    try {
      const { data } = await projectsAPI.listTasks(projectId);
      setTasks(data.tasks || []);
    } catch (err) { console.error(err); }
    finally { setLoading(false); }
  }

  async function handleCreate() {
    if (!newTitle.trim()) return;
    try {
      await projectsAPI.createTask(projectId, { title: newTitle, user_id: "default" });
      setNewTitle(""); setShowCreate(false);
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

  const columns = [
    { key: "todo", label: "待办", color: "bg-slate-100" },
    { key: "in_progress", label: "进行中", color: "bg-blue-100" },
    { key: "done", label: "已完成", color: "bg-green-100" },
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
          <button onClick={handleCreate} className="px-4 py-1.5 bg-slate-900 text-white text-sm rounded-lg">添加</button>
          <button onClick={() => setShowCreate(false)} className="px-3 py-1.5 text-slate-500 text-sm hover:bg-slate-100 rounded-lg"><X className="w-4 h-4" /></button>
        </div>
      )}

      <div className="grid grid-cols-3 gap-4 h-[calc(100%-4rem)]">
        {columns.map((col) => (
          <div key={col.key} className="flex flex-col">
            <div className={`text-xs font-medium px-3 py-1.5 rounded-lg mb-2 ${col.color} text-slate-700`}>
              {col.label} ({tasks.filter((t) => t.status === col.key).length})
            </div>
            <div className="flex-1 overflow-y-auto space-y-2">
              {tasks.filter((t) => t.status === col.key).map((task) => (
                <div key={task.id} className="border border-slate-200 rounded-lg p-3 bg-white text-sm group">
                  <p className="text-slate-800">{task.title}</p>
                  {task.assigned_agent && <p className="text-xs text-slate-400 mt-1">@{task.assigned_agent}</p>}
                  <div className="flex items-center gap-1 mt-2 opacity-0 group-hover:opacity-100 transition-opacity">
                    {col.key !== "todo" && (
                      <button onClick={() => handleStatusChange(task.id, "todo")} className="text-xs px-2 py-0.5 bg-slate-100 rounded hover:bg-slate-200">← 待办</button>
                    )}
                    {col.key !== "in_progress" && (
                      <button onClick={() => handleStatusChange(task.id, "in_progress")} className="text-xs px-2 py-0.5 bg-blue-50 rounded hover:bg-blue-100">进行中</button>
                    )}
                    {col.key !== "done" && (
                      <button onClick={() => handleStatusChange(task.id, "done")} className="text-xs px-2 py-0.5 bg-green-50 rounded hover:bg-green-100">完成</button>
                    )}
                    <button onClick={() => handleDelete(task.id)} className="text-xs px-2 py-0.5 text-red-400 hover:bg-red-50 rounded ml-auto">删除</button>
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

// ── Members Tab ────────────────────────────────────────────────────────────

function MembersTab({ projectId, members, onUpdate }: { projectId: string; members: string[]; onUpdate: () => void }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);

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

  const memberAgents = agents.filter((a) => members.includes(a.name));
  const availableAgents = agents.filter((a) => !members.includes(a.name));

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
          {memberAgents.map((a) => (
            <div key={a.name} className="flex items-center justify-between border border-slate-200 rounded-lg p-3">
              <div>
                <p className="text-sm font-medium text-slate-700">🤖 {a.display_name || a.name}</p>
                <p className="text-xs text-slate-400">{a.description?.slice(0, 60)}</p>
              </div>
              <button onClick={() => handleRemove(a.name)} className="text-xs text-red-400 hover:text-red-600 px-2 py-1 rounded hover:bg-red-50">移除</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
