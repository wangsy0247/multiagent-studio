"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Bot, Plus, Trash2, Edit, Brain } from "lucide-react";
import { agentsAPI } from "@/lib/api-client";

interface Agent {
  name: string;
  display_name: string;
  description: string;
  model: string;
  tool_groups: string[];
  skills: string[] | null;
  created_at: string;
  updated_at: string;
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => { loadAgents(); }, []);

  async function loadAgents() {
    try {
      const { data } = await agentsAPI.list();
      setAgents(data.agents || []);
    } catch (err) {
      console.error("Failed to load agents:", err);
    } finally {
      setLoading(false);
    }
  }

  async function handleDelete(name: string) {
    if (!confirm(`确定删除 Agent "${name}" 吗？此操作不可撤销。`)) return;
    try {
      await agentsAPI.delete(name);
      setAgents((prev) => prev.filter((a) => a.name !== name));
    } catch (err) {
      console.error("Failed to delete agent:", err);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-6 h-6 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">🤖 我的 Agent</h1>
          <p className="text-sm text-slate-500 mt-1">创建和管理自定义 AI Agent</p>
        </div>
        <Link
          href="/agents/new"
          className="flex items-center gap-2 px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-800 transition-colors text-sm"
        >
          <Plus className="w-4 h-4" />
          新建 Agent
        </Link>
      </div>

      {agents.length === 0 ? (
        <div className="text-center py-20">
          <Bot className="w-12 h-12 text-slate-300 mx-auto mb-3" />
          <p className="text-slate-500">还没有创建任何 Agent</p>
          <Link href="/agents/new" className="text-sm text-slate-900 underline mt-2 inline-block">
            创建第一个 Agent
          </Link>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {agents.map((agent) => (
            <div
              key={agent.name}
              className="border border-slate-200 rounded-xl p-5 hover:border-slate-300 hover:shadow-sm transition-all bg-white"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <Link href={`/agents/${agent.name}`} className="group">
                    <h3 className="font-semibold text-slate-900 group-hover:text-slate-700 flex items-center gap-2">
                      <Bot className="w-4 h-4 text-slate-400" />
                      {agent.display_name || agent.name}
                    </h3>
                  </Link>
                  <p className="text-sm text-slate-500 mt-1 line-clamp-2">
                    {agent.description || "暂无描述"}
                  </p>
                  <div className="flex items-center gap-2 mt-3 flex-wrap">
                    <span className="text-xs px-2 py-0.5 bg-slate-100 text-slate-600 rounded">
                      {agent.model}
                    </span>
                    {agent.tool_groups?.map((tg) => (
                      <span key={tg} className="text-xs px-2 py-0.5 bg-blue-50 text-blue-600 rounded">
                        {tg}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="flex items-center gap-1 ml-3">
                  <Link
                    href={`/agents/${agent.name}`}
                    className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100"
                  >
                    <Edit className="w-4 h-4" />
                  </Link>
                  <button
                    onClick={() => handleDelete(agent.name)}
                    className="p-1.5 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
              <div className="flex items-center gap-3 mt-3 pt-3 border-t border-slate-100 text-xs text-slate-400">
                <span className="flex items-center gap-1">
                  <Brain className="w-3 h-3" />
                  记忆系统
                </span>
                <span>{new Date(agent.updated_at).toLocaleDateString()}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
