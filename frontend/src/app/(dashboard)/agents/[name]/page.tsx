"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Save, Trash2, Brain, Shield } from "lucide-react";
import { agentsAPI } from "@/lib/api-client";

const MODEL_OPTIONS = [
  "gpt-4o", "gpt-4o-mini", "gpt-4.1",
  "claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-8",
  "qwen3.6-plus", "deepseek-v4",
];

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
  const [model, setModel] = useState("gpt-4o");  // 必选, 不再有 inherit
  const [temperature, setTemperature] = useState(0.3);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [toolGroups, setToolGroups] = useState("");
  const [memoryData, setMemoryData] = useState<Record<string, unknown> | null>(null);
  // ── Memory 配置 ──
  const [memoryBackend, setMemoryBackend] = useState("file");
  const [memoryMaxFacts, setMemoryMaxFacts] = useState(10);
  const [memoryInjection, setMemoryInjection] = useState(true);
  // ── Agent Team 扩展字段 ──
  const [canBeLead, setCanBeLead] = useState(false);
  const [canDelegate, setCanDelegate] = useState(true);
  const [maxTurns, setMaxTurns] = useState(50);
  const [timeoutSeconds, setTimeoutSeconds] = useState(900);
  const [subagentMaxConcurrent, setSubagentMaxConcurrent] = useState(3);
  // ── Features ──
  const [featureSummarization, setFeatureSummarization] = useState(true);
  const [featureSubagent, setFeatureSubagent] = useState(true);

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
      setModel(agent.model || "gpt-4o");
      setTemperature(agent.temperature ?? 0.3);
      setMaxTokens(agent.max_tokens ?? 4096);
      setToolGroups((agent.tool_groups || []).join(", "));
      // 嵌套子模型 (向后兼容: 可能不存在)
      const mem = agent.memory || {};
      setMemoryBackend(mem.backend || "file");
      setMemoryMaxFacts(mem.max_facts ?? 10);
      setMemoryInjection(mem.injection_enabled ?? true);
      // team
      const team = agent.team || {};
      setCanBeLead(team.can_be_lead ?? agent.can_be_lead ?? false);
      setCanDelegate(team.can_delegate ?? agent.can_delegate ?? true);
      // limits
      const limits = agent.limits || {};
      setMaxTurns(limits.max_turns ?? agent.max_turns ?? 50);
      setTimeoutSeconds(limits.timeout_seconds ?? agent.timeout_seconds ?? 900);
      // subagents
      const sub = agent.subagents || {};
      setSubagentMaxConcurrent(sub.max_concurrent ?? 3);
      // features
      const feat = agent.features || {};
      setFeatureSummarization(feat.summarization ?? true);
      setFeatureSubagent(feat.subagent ?? true);
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
    if (!model) {
      setError("请选择模型");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const tg = toolGroups.split(",").map((s) => s.trim()).filter(Boolean);
      const payload: Record<string, unknown> = {
        name: agentName.trim(),
        model,
        display_name: displayName.trim() || undefined,
        description: description.trim() || undefined,
        soul: soul.trim() || undefined,
        tool_groups: tg,
        temperature,
        max_tokens: maxTokens,
        memory: { backend: memoryBackend, max_facts: memoryMaxFacts, injection_enabled: memoryInjection },
        features: { summarization: featureSummarization, subagent: featureSubagent, langfuse: true, guardrail: false },
        limits: { max_turns: maxTurns, timeout_seconds: timeoutSeconds },
        team: { can_be_lead: canBeLead, can_delegate: canDelegate, memory_scope: "agent" },
        subagents: { max_concurrent: subagentMaxConcurrent, timeout_seconds: 900 },
      };
      if (isNew) {
        await agentsAPI.create(payload as any);
        router.push(`/agents/${agentName.trim()}`);
      } else {
        await agentsAPI.update(name, payload);
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
    } catch (err: any) {
      const detail = err.response?.data?.detail || "";
      if (err.response?.status === 403) {
        setError("无法删除 default agent — 它是系统必需的");
      } else {
        setError(detail || "删除失败");
      }
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

  const isDefault = agentName === "default";

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
          {isDefault && (
            <span className="text-xs px-2 py-0.5 bg-amber-100 text-amber-700 rounded font-medium mt-1 inline-block">
              <Shield className="w-3 h-3 inline mr-1" />系统默认 Agent — 不可删除
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {!isNew && !isDefault && (
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

        {/* Model & Tools */}
        <div className="grid grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">模型 *</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            >
              {MODEL_OPTIONS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">温度</label>
            <input
              type="number"
              value={temperature}
              onChange={(e) => setTemperature(parseFloat(e.target.value) || 0.3)}
              min={0} max={2} step={0.1}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">最大 Token</label>
            <input
              type="number"
              value={maxTokens}
              onChange={(e) => setMaxTokens(parseInt(e.target.value) || 4096)}
              min={256} max={128000} step={256}
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            />
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1">工具组（逗号分隔，扩展到系统默认）</label>
          <input
            type="text"
            value={toolGroups}
            onChange={(e) => setToolGroups(e.target.value)}
            placeholder="例如: code (为空则使用系统默认 search, files, files_readonly, mcp)"
            className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
          />
          <p className="text-[10px] text-slate-400 mt-1">系统默认工具组: search, files, files_readonly, mcp — Agent 可在此基础上扩展</p>
        </div>

        {/* ── 记忆配置 ── */}
        <div className="border border-slate-200 rounded-xl p-4">
          <h3 className="text-sm font-medium text-slate-700 mb-3">记忆配置</h3>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">后端</label>
              <select value={memoryBackend} onChange={(e) => setMemoryBackend(e.target.value)}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200">
                <option value="file">File (本地 JSON)</option>
                <option value="mem0">mem0 (pgvector)</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">最大记忆数</label>
              <input type="number" value={memoryMaxFacts} onChange={(e) => setMemoryMaxFacts(parseInt(e.target.value) || 10)}
                min={1} max={100}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200" />
            </div>
            <div className="flex items-end">
              <label className="flex items-center gap-2 cursor-pointer pb-1.5">
                <input type="checkbox" checked={memoryInjection} onChange={(e) => setMemoryInjection(e.target.checked)}
                  className="w-3.5 h-3.5 rounded border-slate-300" />
                <span className="text-xs text-slate-600">启用记忆注入</span>
              </label>
            </div>
          </div>
        </div>

        {/* ── Agent Team 配置 ── */}
        <div className="border border-slate-200 rounded-xl p-4">
          <h3 className="text-sm font-medium text-slate-700 mb-3">Team & 限制</h3>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">最大轮次</label>
              <input type="number" value={maxTurns} onChange={(e) => setMaxTurns(parseInt(e.target.value) || 50)}
                min={1} max={200}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">超时（秒）</label>
              <input type="number" value={timeoutSeconds} onChange={(e) => setTimeoutSeconds(parseInt(e.target.value) || 900)}
                min={30} max={3600}
                className="w-full px-2.5 py-1.5 border border-slate-200 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-slate-200" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">并行 SubAgent 数</label>
              <input type="number" value={subagentMaxConcurrent} onChange={(e) => setSubagentMaxConcurrent(parseInt(e.target.value) || 3)}
                min={1} max={10}
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

        {/* ── 功能开关 ── */}
        <div className="border border-slate-200 rounded-xl p-4">
          <h3 className="text-sm font-medium text-slate-700 mb-3">功能开关</h3>
          <div className="flex gap-6">
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={featureSummarization} onChange={(e) => setFeatureSummarization(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-slate-300" />
              <span className="text-xs text-slate-600">对话摘要</span>
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={featureSubagent} onChange={(e) => setFeatureSubagent(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-slate-300" />
              <span className="text-xs text-slate-600">子 Agent 委派</span>
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
            placeholder={"# Agent Soul\n\n你是一个专业的编程助手..."}
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
