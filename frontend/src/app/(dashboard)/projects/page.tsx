"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { FolderKanban, Plus, Trash2, Users, CheckSquare, MessageCircle } from "lucide-react";
import { projectsAPI } from "@/lib/api-client";

interface Project {
  id: string; name: string; description: string;
  members: string[]; thread_count: number; task_count: number;
  created_at: string; updated_at: string;
}

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");

  useEffect(() => { loadProjects(); }, []);

  async function loadProjects() {
    try {
      const { data } = await projectsAPI.list();
      setProjects(data.projects || []);
    } catch (err) {
      console.error("Failed to load projects:", err);
    } finally { setLoading(false); }
  }

  async function handleCreate() {
    if (!newName.trim()) return;
    try {
      await projectsAPI.create({ name: newName, description: newDesc });
      setNewName(""); setNewDesc(""); setShowCreate(false);
      loadProjects();
    } catch (err) { console.error("Failed to create project:", err); }
  }

  async function handleDelete(id: string) {
    if (!confirm("确定删除此项目吗？")) return;
    try { await projectsAPI.delete(id); loadProjects(); }
    catch (err) { console.error(err); }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-6 h-6 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
    <div className="max-w-5xl mx-auto p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold font-display text-slate-900 flex items-center gap-2">
            <FolderKanban className="w-6 h-6 text-hermes-500" /> 项目
          </h1>
          <p className="text-sm text-slate-500 mt-1">Agent 团队协作空间</p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-800 transition-colors text-sm"
        >
          <Plus className="w-4 h-4" /> 新建项目
        </button>
      </div>

      {/* Create Dialog */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => setShowCreate(false)}>
          <div className="bg-white rounded-xl p-6 w-96 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-semibold mb-4">新建项目</h2>
            <input type="text" value={newName} onChange={(e) => setNewName(e.target.value)}
              placeholder="项目名称" className="w-full px-3 py-2 border rounded-lg text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-slate-200" />
            <textarea value={newDesc} onChange={(e) => setNewDesc(e.target.value)}
              placeholder="描述（可选）" rows={3} className="w-full px-3 py-2 border rounded-lg text-sm mb-4 focus:outline-none focus:ring-2 focus:ring-slate-200 resize-none" />
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowCreate(false)} className="px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 rounded-lg">取消</button>
              <button onClick={handleCreate} className="px-4 py-2 text-sm bg-slate-900 text-white rounded-lg hover:bg-slate-800">创建</button>
            </div>
          </div>
        </div>
      )}

      {projects.length === 0 ? (
        <div className="text-center py-20">
          <FolderKanban className="w-12 h-12 text-slate-300 mx-auto mb-3" />
          <p className="text-slate-500">还没有创建任何项目</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {projects.map((p) => (
            <Link key={p.id} href={`/projects/${p.id}`}
              className="border border-slate-200 rounded-xl p-5 hover:border-slate-300 hover:shadow-sm transition-all bg-white block">
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <h3 className="font-semibold text-slate-900 flex items-center gap-2">
                    <FolderKanban className="w-4 h-4 text-slate-400" /> {p.name}
                  </h3>
                  <p className="text-sm text-slate-500 mt-1 line-clamp-2">{p.description || "暂无描述"}</p>
                  <div className="flex items-center gap-4 mt-3 text-xs text-slate-400">
                    <span className="flex items-center gap-1"><Users className="w-3 h-3" />{p.members?.length || 0} 成员</span>
                    <span className="flex items-center gap-1"><MessageCircle className="w-3 h-3" />{p.thread_count || 0} 对话</span>
                    <span className="flex items-center gap-1"><CheckSquare className="w-3 h-3" />{p.task_count || 0} 任务</span>
                  </div>
                </div>
                <button onClick={(e) => { e.preventDefault(); handleDelete(p.id); }}
                  className="p-1.5 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50">
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
    </div>
  );
}
