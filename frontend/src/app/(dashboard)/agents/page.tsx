"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Bot, Plus, Trash2, Edit, Brain, Clock, Wrench, Zap, User, BookOpen, Shield, AlertTriangle, Star } from "lucide-react";
import { agentsAPI } from "@/lib/api-client";

// ── 预设 Agent 定义 (与后端 presets.py 对齐) ──────────────────────────
const PRESET_AGENTS = [
  {
    name: "researcher", display_name: "信息检索专家", icon: "🔍",
    description: "Web search, literature lookup, data collection",
    tools: ["web_search", "arxiv_search", "web_fetch"],
    max_turns: 60, timeout_seconds: 900,
  },
  {
    name: "coder", display_name: "代码执行专家", icon: "💻",
    description: "Writing, running, debugging code in sandbox",
    tools: ["bash", "file_read", "file_write", "list_files", "glob_tool", "grep_tool", "str_replace"],
    max_turns: 60, timeout_seconds: 600,
  },
  {
    name: "analyst", display_name: "数据分析专家", icon: "📊",
    description: "Data cleaning, statistical analysis, visualization",
    tools: ["bash", "file_read", "file_write", "web_search"],
    max_turns: 60, timeout_seconds: 900,
  },
  {
    name: "writer", display_name: "文档撰写专家", icon: "📝",
    description: "Structured documents, reports, config files",
    tools: ["file_read", "file_write", "str_replace", "list_files"],
    max_turns: 40, timeout_seconds: 600,
  },
  {
    name: "reviewer", display_name: "审查专家", icon: "🔎",
    description: "Code review, document proofreading, quality inspection",
    tools: ["file_read", "list_files", "glob_tool", "grep_tool"],
    max_turns: 30, timeout_seconds: 600,
  },
];

interface AgentListItem {
  name: string; display_name: string; description: string;
  model: string; tool_groups: string[]; skills: string[] | null;
  created_at: string; updated_at: string;
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedPreset, setExpandedPreset] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState("");

  const hasDefault = agents.some((a) => a.name === "default");

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
    if (name === "default") {
      setDeleteError("default agent 是系统必需的，不可删除");
      return;
    }
    if (!confirm(`确定删除 Agent "${name}" 吗？此操作不可撤销。`)) return;
    try {
      await agentsAPI.delete(name);
      setAgents((prev) => prev.filter((a) => a.name !== name));
      setDeleteError("");
    } catch (err: any) {
      if (err.response?.status === 403) {
        setDeleteError("无法删除 default agent — 它是系统必需的");
      } else {
        setDeleteError(err.response?.data?.detail || "删除失败");
      }
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
      {/* ── Header ── */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-900">🤖 我的 Agent</h1>
          <p className="text-sm text-slate-500 mt-1">管理预设和自定义 AI Agent</p>
        </div>
        <Link
          href="/agents/new"
          className="flex items-center gap-2 px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-800 transition-colors text-sm"
        >
          <Plus className="w-4 h-4" /> 新建 Agent
        </Link>
      </div>

      {/* ── Default Agent 缺失警告 ── */}
      {!hasDefault && agents.length > 0 && (
        <div className="mb-6 p-4 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-sm font-medium text-amber-800">未检测到 default agent</p>
            <p className="text-xs text-amber-600 mt-1">
              系统需要一个 default agent 用于单 Agent 模式和 Team Lead。
              请创建一个名为 <code className="bg-amber-100 px-1 rounded">default</code> 的 Agent，
              或编辑一个现有 Agent 并将其设为 default。
            </p>
          </div>
        </div>
      )}

      {/* ── Default Agent 空状态引导 ── */}
      {agents.length === 0 && (
        <div className="mb-6 p-5 bg-blue-50 border border-blue-200 rounded-xl">
          <div className="flex items-start gap-3">
            <Shield className="w-5 h-5 text-blue-500 shrink-0 mt-0.5" />
            <div className="flex-1">
              <p className="text-sm font-medium text-blue-800">欢迎使用 Agent 配置系统</p>
              <p className="text-xs text-blue-600 mt-1 mb-3">
                每个用户至少需要一个 Agent 来运行任务。系统推荐先创建一个 default agent：
              </p>
              <ul className="text-xs text-blue-600 space-y-1 mb-3 list-disc list-inside">
                <li>default agent 用于单 Agent 模式和 Team 的 Lead</li>
                <li>创建后还可以添加更多专业 Agent（如 coder、researcher）</li>
                <li>每个 Agent 可以配置不同的模型、工具和记忆策略</li>
              </ul>
              <Link
                href="/agents/new"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 transition-colors"
              >
                <Plus className="w-3.5 h-3.5" /> 创建 default Agent
              </Link>
            </div>
          </div>
        </div>
      )}

      {deleteError && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-600 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          {deleteError}
        </div>
      )}

      {/* ════════════════════════════════════════════════════════════
          Section 1: 系统预设
          ════════════════════════════════════════════════════════════ */}
      <div className="mb-10">
        <div className="flex items-center gap-2 mb-4">
          <Zap className="w-4 h-4 text-amber-500" />
          <h2 className="text-sm font-semibold text-slate-700 uppercase tracking-wide">系统预设</h2>
          <span className="text-xs text-slate-400">({PRESET_AGENTS.length} 个)</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {PRESET_AGENTS.map((preset) => {
            const isExpanded = expandedPreset === preset.name;
            return (
              <div key={preset.name} className="border border-slate-200 rounded-xl bg-white hover:border-slate-300 hover:shadow-sm transition-all">
                <button onClick={() => setExpandedPreset(isExpanded ? null : preset.name)} className="w-full text-left p-4">
                  <div className="flex items-start gap-3">
                    <span className="text-2xl">{preset.icon}</span>
                    <div className="flex-1 min-w-0">
                      <h3 className="font-semibold text-slate-900 text-sm">{preset.display_name}</h3>
                      <p className="text-xs text-slate-500 mt-0.5 line-clamp-1">{preset.description}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 mt-3 text-[10px] text-slate-400">
                    <span className="flex items-center gap-1"><Wrench className="w-3 h-3" />{preset.tools.length} 工具</span>
                    <span className="flex items-center gap-1"><Clock className="w-3 h-3" />{preset.timeout_seconds}s</span>
                    <span className="flex items-center gap-1"><Brain className="w-3 h-3" />{preset.max_turns} turns</span>
                  </div>
                </button>
                {isExpanded && (
                  <div className="px-4 pb-4 border-t border-slate-100 pt-3">
                    <p className="text-[10px] font-medium text-slate-400 uppercase tracking-wider mb-2">已配置工具</p>
                    <div className="flex flex-wrap gap-1.5">
                      {preset.tools.map((t) => (
                        <span key={t} className="text-[10px] px-2 py-0.5 bg-slate-100 text-slate-600 rounded font-mono">{t}</span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* ════════════════════════════════════════════════════════════
          Section 2: 自定义 Agent
          ════════════════════════════════════════════════════════════ */}
      <div>
        <div className="flex items-center gap-2 mb-4">
          <User className="w-4 h-4 text-blue-500" />
          <h2 className="text-sm font-semibold text-slate-700 uppercase tracking-wide">自定义 Agent</h2>
          <span className="text-xs text-slate-400">({agents.length} 个)</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {agents.map((agent) => (
            <div key={agent.name} className="border border-slate-200 rounded-xl p-5 hover:border-slate-300 hover:shadow-sm transition-all bg-white">
              <div className="flex items-start justify-between">
                <div className="flex-1 min-w-0">
                  <Link href={`/agents/${agent.name}`} className="group">
                    <h3 className="font-semibold text-slate-900 group-hover:text-slate-700 flex items-center gap-2 text-sm">
                      {agent.name === "default"
                        ? <Shield className="w-4 h-4 text-amber-400" />
                        : <Bot className="w-4 h-4 text-blue-400" />
                      }
                      {agent.display_name || agent.name}
                      {agent.name === "default" && (
                        <span className="text-[10px] px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded font-medium">系统默认</span>
                      )}
                      {agent.team?.can_be_lead && agent.name !== "default" && (
                        <span className="text-[10px] px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded font-medium flex items-center gap-0.5">
                          <Star className="w-3 h-3" /> Team Lead
                        </span>
                      )}
                    </h3>
                  </Link>
                  <p className="text-xs text-slate-500 mt-1 line-clamp-2">{agent.description || "暂无描述"}</p>
                  <div className="flex items-center gap-2 mt-3 flex-wrap">
                    <span className="text-[10px] px-2 py-0.5 bg-slate-100 text-slate-600 rounded font-mono">{agent.model}</span>
                    {agent.tool_groups?.map((tg) => (
                      <span key={tg} className="text-[10px] px-2 py-0.5 bg-blue-50 text-blue-600 rounded">{tg}</span>
                    ))}
                    {(!agent.tool_groups || agent.tool_groups.length === 0) && (
                      <span className="text-[10px] px-2 py-0.5 bg-slate-50 text-slate-400 rounded">系统默认工具</span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1 ml-3">
                  <Link href={`/agents/${agent.name}`} className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100">
                    <Edit className="w-4 h-4" />
                  </Link>
                  {agent.name !== "default" && (
                    <button onClick={() => handleDelete(agent.name)}
                      className="p-1.5 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50">
                      <Trash2 className="w-4 h-4" />
                    </button>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-3 mt-3 pt-3 border-t border-slate-100 text-[10px] text-slate-400">
                <span className="flex items-center gap-1"><BookOpen className="w-3 h-3" />记忆系统</span>
                <span>{new Date(agent.updated_at).toLocaleDateString()}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
