"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Save, Trash2, Brain } from "lucide-react";
import { agentsAPI } from "@/lib/api-client";

export default function AgentEditPage() {
  const { name } = useParams<{ name: string }>();
  const router = useRouter();
  const isNew = name === "new";

  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [agentName, setAgentName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [soul, setSoul] = useState("");
  const [model, setModel] = useState("inherit");
  const [toolGroups, setToolGroups] = useState("");
  const [memoryData, setMemoryData] = useState<Record<string, unknown> | null>(null);
  // ── Agent Team 扩展字段 ──
  const [memoryScope, setMemoryScope] = useState("team");
  const [canBeLead, setCanBeLead] = useState(false);
  const [canDelegate, setCanDelegate] = useState(true);
  const [maxTurns, setMaxTurns] = useState(50);
  const [timeoutSeconds, setTimeoutSeconds] = useState(900);
  const [isolation, setIsolation] = useState("none");

  useEffect(() => {
    if (!isNew) loadAgent();
  }, [name]);

  async function loadAgent() {
    try {
      const { data } = await agentsAPI.get(name);
      const agent = data.agent;
      setAgentName(agent.name);
      setDisplayName(agent.display_name || "");
      setDescription(agent.description || "");
      setSoul(data.soul || "");
      setModel(agent.model || "inherit");
      setToolGroups((agent.tool_groups || []).join(", "));
      // ── Agent Team 字段 ──
      setMemoryScope(agent.memory_scope || "team");
      setCanBeLead(agent.can_be_lead ?? false);
      setCanDelegate(agent.can_delegate ?? true);
      setMaxTurns(agent.max_turns || 50);
      setTimeoutSeconds(agent.timeout_seconds || 900);
      setIsolation(agent.isolation || "none");
      // Load memory
      const memResp = await agentsAPI.getMemory(name);
      setMemoryData(memResp.data.memory);
    } catch (err) {
      setError("加载 Agent 失败");
    } finally {
      setLoading(false);
    }
  }

  async function handleSave() {
    if (!agentName.trim()) {
      setError("Agent 名称不能为空");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const tg = toolGroups.split(",").map((s) => s.trim()).filter(Boolean);
      if (isNew) {
        await agentsAPI.create({
          name: agentName.trim(),
          display_name: displayName.trim(),
          description: description.trim(),
          soul: soul.trim(),
          model,
          tool_groups: tg,
          memory_scope: memoryScope,
          can_be_lead: canBeLead,
          can_delegate: canDelegate,
          max_turns: maxTurns,
          timeout_seconds: timeoutSeconds,
          isolation,
        });
        router.push(`/agents/${agentName.trim()}`);
      } else {
        await agentsAPI.update(name, {
          display_name: displayName.trim(),
          description: description.trim(),
          soul: soul.trim(),
          model,
          tool_groups: tg,
          memory_scope: memoryScope,
          can_be_lead: canBeLead,
          can_delegate: canDelegate,
          max_turns: maxTurns,
          timeout_seconds: timeoutSeconds,
          isolation,
        });
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!confirm("确定删除此 Agent 及其所有数据吗？")) return;
    try {
      await agentsAPI.delete(name);
      router.push("/agents");
    } catch (err) {
      setError("删除失败");
    }
  }

  async function handleClearMemory() {
    if (!confirm("确定清除此 Agent 的长期记忆吗？")) return;
    try {
      await agentsAPI.clearMemory(name);
      setMemoryData(null);
    } catch (err) {
      console.error("Failed to clear memory:", err);
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
    <div className="max-w-3xl mx-auto p-6">
      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <button onClick={() => router.back()} className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100">
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold text-slate-900">
            {isNew ? "新建 Agent" : `编辑: ${displayName || agentName}`}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {!isNew && (
            <button onClick={handleDelete} className="flex items-center gap-1.5 px-3 py-2 text-sm text-red-600 hover:bg-red-50 rounded-lg transition-colors">
              <Trash2 className="w-4 h-4" /> 删除
            </button>
          )}
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-1.5 px-4 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-800 disabled:opacity-50 transition-colors text-sm"
          >
            <Save className="w-4 h-4" /> {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-600">
          {error}
        </div>
      )}

      {/* Form */}
      <div className="space-y-5">
        {/* Basic Info */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">Agent 名称 *</label>
            <input
              type="text"
              value={agentName}
              onChange={(e) => setAgentName(e.target.value)}
              disabled={!isNew}
              placeholder="my-coder"
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200 disabled:bg-slate-50"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">显示名称</label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="My Coder"
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            />
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">描述</label>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="此 Agent 的简短描述"
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">模型</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            >
              <option value="inherit">继承 (inherit)</option>
              <option value="gpt-4o">gpt-4o</option>
              <option value="gpt-4o-mini">gpt-4o-mini</option>
              <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
              <option value="claude-haiku-4-5">claude-haiku-4-5</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">工具组（逗号分隔）</label>
            <input
              type="text"
              value={toolGroups}
              onChange={(e) => setToolGroups(e.target.value)}
              placeholder="coding, search"
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            />
          </div>
        </div>

        {/* ── Agent Team 配置 ── */}
        <div className="border border-slate-200 rounded-xl p-4">
          <h3 className="text-sm font-medium text-slate-700 mb-3">Agent Team 配置</h3>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">记忆范围</label>
              <select value={memoryScope} onChange={(e) => setMemoryScope(e.target.value)}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200">
                <option value="user">用户级 (user)</option>
                <option value="project">项目级 (project)</option>
                <option value="local">本地私有 (local)</option>
                <option value="team">团队内私有 (team)</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">隔离方式</label>
              <select value={isolation} onChange={(e) => setIsolation(e.target.value)}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200">
                <option value="none">共享 workspace</option>
                <option value="worktree">独立 worktree</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">模型</label>
              <select value={model} onChange={(e) => setModel(e.target.value)}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200">
                <option value="inherit">继承 (inherit)</option>
                <option value="gpt-4o">gpt-4o</option>
                <option value="gpt-4o-mini">gpt-4o-mini</option>
                <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                <option value="claude-haiku-4-5">claude-haiku-4-5</option>
              </select>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4 mt-3">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">最大轮次</label>
              <input type="number" value={maxTurns} onChange={(e) => setMaxTurns(parseInt(e.target.value) || 50)}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">超时（秒）</label>
              <input type="number" value={timeoutSeconds} onChange={(e) => setTimeoutSeconds(parseInt(e.target.value) || 900)}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200" />
            </div>
          </div>
          <div className="flex gap-6 mt-3">
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={canBeLead} onChange={(e) => setCanBeLead(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-slate-300" />
              <span className="text-xs text-slate-600">可担任 Project Lead</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={canDelegate} onChange={(e) => setCanDelegate(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-slate-300" />
              <span className="text-xs text-slate-600">可委派任务</span>
            </label>
          </div>
        </div>

        {/* SOUL Editor */}
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">
            SOUL.md — Agent 人格定义
          </label>
          <p className="text-xs text-slate-400 mb-2">
            定义 Agent 的行为准则、人格特征和专业领域。此内容将作为 Agent 的系统提示词。
          </p>
          <textarea
            value={soul}
            onChange={(e) => setSoul(e.target.value)}
            placeholder="# Agent Soul&#10;&#10;你是一个专业的编程助手...&#10;"
            rows={12}
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-slate-200 resize-y"
          />
        </div>

        {/* Memory */}
        {!isNew && (
          <div className="border border-slate-200 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-slate-700 flex items-center gap-2">
                <Brain className="w-4 h-4 text-slate-400" />
                长期记忆
              </h3>
              {memoryData && (
                <button
                  onClick={handleClearMemory}
                  className="text-xs text-red-500 hover:text-red-600"
                >
                  清除记忆
                </button>
              )}
            </div>
            {memoryData ? (
              <div className="text-xs text-slate-500">
                事实数: {(memoryData as any).facts?.length || 0}
                {" · "}
                最后更新: {(memoryData as any).lastUpdated || "未知"}
              </div>
            ) : (
              <p className="text-xs text-slate-400">暂无记忆数据</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
