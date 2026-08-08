"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Save, Trash2, Brain, Shield } from "lucide-react";
import { agentsAPI, extensionsAPI } from "@/lib/api-client";

interface ExtRow { name: string; description: string; enabled: boolean; }

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
  // 模型由服务器统一配置 (harness/.env), Agent 只可调温度和最大 Token
  const [temperature, setTemperature] = useState(0.3);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [memoryData, setMemoryData] = useState<Record<string, unknown> | null>(null);
  // ── 限制 ──
  const [maxTurns, setMaxTurns] = useState(50);
  const [timeoutSeconds, setTimeoutSeconds] = useState(900);
  const [subagentMaxConcurrent, setSubagentMaxConcurrent] = useState(3);
  // ── Features ──
  const [featureSummarization, setFeatureSummarization] = useState(true);
  const [featureSubagent, setFeatureSubagent] = useState(true);
  // ── 扩展能力 (per-agent 黑名单: 勾选=继承全局, 取消=此 agent 禁用) ──
  const [mcpRows, setMcpRows] = useState<ExtRow[]>([]);
  const [skillRows, setSkillRows] = useState<ExtRow[]>([]);
  const [mcpDisabled, setMcpDisabled] = useState<Set<string>>(new Set());
  const [skillsDisabled, setSkillsDisabled] = useState<Set<string>>(new Set());

  useEffect(() => {
    // 全局 MCP server 和 skill 列表 (新旧 agent 都需要, 用于扩展能力区块)
    (async () => {
      try {
        const [mcpResp, skillResp] = await Promise.all([
          extensionsAPI.listMcpServers(),
          extensionsAPI.listSkills(),
        ]);
        const srv = mcpResp.data.servers || {};
        setMcpRows(
          Object.entries(srv).map(([n, cfg]: [string, any]) => ({
            name: n, description: cfg.description || "", enabled: cfg.enabled !== false,
          }))
        );
        setSkillRows(
          (skillResp.data.skills || []).map((s: any) => ({
            name: s.name, description: s.description || "", enabled: s.enabled !== false,
          }))
        );
      } catch (err) {
        console.warn("加载全局扩展列表失败:", err);
      }
    })();
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
      setTemperature(agent.temperature ?? 0.3);
      setMaxTokens(agent.max_tokens ?? 4096);
      // limits (向后兼容: 可能不存在)
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
      // per-agent 扩展黑名单 (extensions_config.yaml)
      const ext = data.extensions || {};
      setMcpDisabled(new Set(
        Object.entries(ext.mcp_servers || {}).filter(([, v]) => v === false).map(([k]) => k)
      ));
      setSkillsDisabled(new Set(
        Object.entries(ext.skills || {}).filter(([, v]) => v === false).map(([k]) => k)
      ));
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
      const payload: Record<string, unknown> = {
        name: agentName.trim(),
        display_name: displayName.trim(),
        description: description.trim(),
        soul: soul.trim(),
        temperature,
        max_tokens: maxTokens,
        features: { summarization: featureSummarization, subagent: featureSubagent, langfuse: true, guardrail: false },
        limits: { max_turns: maxTurns, timeout_seconds: timeoutSeconds },
        subagents: { max_concurrent: subagentMaxConcurrent, timeout_seconds: 900 },
        // per-agent 扩展黑名单 (false=此 agent 禁用; 不传的 key = 继承全局)
        mcp_servers: Object.fromEntries([...mcpDisabled].map((n) => [n, false])),
        skills_enabled: Object.fromEntries([...skillsDisabled].map((n) => [n, false])),
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
    <div className="h-full overflow-y-auto">
    <div className="max-w-3xl mx-auto p-6">
      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <button onClick={() => router.back()} className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100">
          <ArrowLeft className="w-5 h-5" />
        </button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold font-display text-slate-900">
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
            <p className="text-[10px] text-slate-400 mt-1">
              唯一标识（小写英文/数字/连字符），用于 @提及、任务委派和 API 调用，创建后不可修改
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">显示名称</label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="代码助手"
              className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-200"
            />
            <p className="text-[10px] text-slate-400 mt-1">
              可选，仅用于界面展示的友好名称（支持中文），留空则显示 Agent 名称
            </p>
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

        {/* Model & Limits — Agent 仅可调温度/最大 Token 等调用参数 */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">温度</label>
            <input
              type="number"
              value={temperature}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                setTemperature(Number.isNaN(v) ? 0.3 : v);
              }}
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

        {/* ── 运行限制 ── */}
        <div className="border border-slate-200 rounded-xl p-4">
          <h3 className="text-sm font-medium text-slate-700 mb-3">运行限制</h3>
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
        </div>

        {/* ── 扩展能力 (per-agent 黑名单) ── */}
        <div className="border border-slate-200 rounded-xl p-4">
          <h3 className="text-sm font-medium text-slate-700 mb-1">扩展能力</h3>
          <p className="text-[10px] text-slate-400 mb-3">
            勾选 = 继承全局 (允许); 取消勾选 = 此 Agent 禁用。全局已禁用的项不可在此覆盖,
            全局管理在「扩展」页。
          </p>
          {mcpRows.length > 0 && (
            <div className="mb-3">
              <p className="text-xs font-medium text-slate-600 mb-1.5">MCP 服务</p>
              <div className="space-y-1.5">
                {mcpRows.map((row) => {
                  const globallyOff = !row.enabled;
                  const checked = globallyOff ? false : !mcpDisabled.has(row.name);
                  return (
                    <label key={row.name} className={`flex items-center gap-2 ${globallyOff ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}>
                      <input type="checkbox" checked={checked} disabled={globallyOff}
                        onChange={(e) => {
                          const next = new Set(mcpDisabled);
                          if (e.target.checked) next.delete(row.name);
                          else next.add(row.name);
                          setMcpDisabled(next);
                        }}
                        className="w-3.5 h-3.5 rounded border-slate-300" />
                      <span className="text-xs text-slate-600 font-mono">{row.name}</span>
                      {row.description && <span className="text-[10px] text-slate-400 truncate">{row.description}</span>}
                      {globallyOff && <span className="text-[10px] text-slate-400">(全局已禁用)</span>}
                    </label>
                  );
                })}
              </div>
            </div>
          )}
          {skillRows.length > 0 && (
            <div>
              <p className="text-xs font-medium text-slate-600 mb-1.5">技能</p>
              <div className="space-y-1.5">
                {skillRows.map((row) => {
                  const globallyOff = !row.enabled;
                  const checked = globallyOff ? false : !skillsDisabled.has(row.name);
                  return (
                    <label key={row.name} className={`flex items-center gap-2 ${globallyOff ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}>
                      <input type="checkbox" checked={checked} disabled={globallyOff}
                        onChange={(e) => {
                          const next = new Set(skillsDisabled);
                          if (e.target.checked) next.delete(row.name);
                          else next.add(row.name);
                          setSkillsDisabled(next);
                        }}
                        className="w-3.5 h-3.5 rounded border-slate-300" />
                      <span className="text-xs text-slate-600">{row.name}</span>
                      {row.description && <span className="text-[10px] text-slate-400 truncate">{row.description}</span>}
                      {globallyOff && <span className="text-[10px] text-slate-400">(全局已禁用)</span>}
                    </label>
                  );
                })}
              </div>
            </div>
          )}
          {mcpRows.length === 0 && skillRows.length === 0 && (
            <p className="text-xs text-slate-400">暂无全局 MCP 服务或技能, 可先在「扩展」页添加</p>
          )}
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
    </div>
  );
}
