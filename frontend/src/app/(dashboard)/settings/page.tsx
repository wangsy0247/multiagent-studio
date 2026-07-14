"use client";

import { useState, useEffect } from "react";
import { Save, CheckCircle, User, Cpu, Key, Brain, Shield, AlertTriangle } from "lucide-react";
import { authAPI } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import apiClient from "@/lib/api-client";

type SettingsTab = "api" | "models" | "memory" | "profile";

function getUserId(): string {
  if (typeof window === "undefined") return "default";
  try {
    const stored = localStorage.getItem("auth-storage");
    if (stored) {
      const { state } = JSON.parse(stored);
      return state?.user?.id || "default";
    }
  } catch {}
  return "default";
}

const MODEL_OPTIONS = [
  { value: "gpt-4o", label: "GPT-4o" },
  { value: "gpt-4o-mini", label: "GPT-4o-mini" },
  { value: "gpt-4.1", label: "GPT-4.1" },
  { value: "claude-sonnet-4-6", label: "Claude Sonnet 4" },
  { value: "claude-opus-4-8", label: "Claude Opus 4" },
  { value: "claude-haiku-4-5", label: "Claude Haiku 4" },
  { value: "qwen3.6-plus", label: "通义千问 3.6 Plus" },
  { value: "deepseek-v4", label: "DeepSeek V4" },
];

export default function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>("api");
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(false);
  const [bootstrapOk, setBootstrapOk] = useState(true);

  const [displayName, setDisplayName] = useState("");
  // ── API 配置 ──
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://api.openai.com/v1");
  // ── 模型配置 ──
  const [defaultModel, setDefaultModel] = useState("gpt-4o");
  const [summaryModel, setSummaryModel] = useState("");
  const [titleModel, setTitleModel] = useState("");
  const [memoryModel, setMemoryModel] = useState("");
  // ── 记忆配置 ──
  const [memoryMaxInjectionTokens, setMemoryMaxInjectionTokens] = useState(500);
  const [memoryDebounceSeconds, setMemoryDebounceSeconds] = useState(120);
  const [memoryFactConfidenceThreshold, setMemoryFactConfidenceThreshold] = useState(0.7);
  // ── 功能开关 ──
  const [summarizationEnabled, setSummarizationEnabled] = useState(true);
  const [titleEnabled, setTitleEnabled] = useState(true);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    // 1. Profile
    try {
      const { data: user } = await authAPI.getMe();
      setDisplayName(user.display_name || "");
    } catch {}

    // 2. L1 user global config
    try {
      const uid = getUserId();
      const { data } = await apiClient.get(`/v1/agents/config/global?user_id=${uid}`);
      if (data.exists) {
        const cfg = data.config;
        // API
        setApiKey(cfg.api_key || "");
        setBaseUrl(cfg.base_url || "https://api.openai.com/v1");
        // Models
        setDefaultModel(cfg.default_model || "gpt-4o");
        setSummaryModel(cfg.summary_model || "");
        setTitleModel(cfg.title_model || "");
        setMemoryModel(cfg.memory_model || "");
        // Memory
        const mem = cfg.memory || {};
        setMemoryMaxInjectionTokens(mem.max_injection_tokens ?? 500);
        setMemoryDebounceSeconds(mem.debounce_seconds ?? 120);
        setMemoryFactConfidenceThreshold(mem.fact_confidence_threshold ?? 0.7);
        // Feature toggles
        const s = cfg.summarization || {};
        setSummarizationEnabled(s.enabled !== false);
        const t = cfg.title || {};
        setTitleEnabled(t.enabled !== false);
      }
    } catch (err) {
      console.warn("Failed to load L1 config:", err);
    }

    // 3. Bootstrap status
    try {
      const { data: bs } = await apiClient.get("/api/v1/system/bootstrap-status");
      setBootstrapOk(bs.ok);
    } catch {}
  }

  async function saveAll() {
    setLoading(true);
    try {
      // Save profile
      await authAPI.updateMe({ display_name: displayName });

      // Save L1 global config
      const uid = getUserId();
      const config: Record<string, unknown> = {
        api_key: apiKey,
        base_url: baseUrl,
        default_model: defaultModel,
        summary_model: summaryModel,
        title_model: titleModel,
        memory_model: memoryModel,
        memory: {
          max_injection_tokens: memoryMaxInjectionTokens,
          debounce_seconds: memoryDebounceSeconds,
          fact_confidence_threshold: memoryFactConfidenceThreshold,
        },
        summarization: { enabled: summarizationEnabled },
        title: { enabled: titleEnabled },
      };
      await apiClient.put(`/v1/agents/config/global`, {
        user_id: uid,
        config,
      });
      setSaved(true);
      setBootstrapOk(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      console.error("保存配置失败", err);
    } finally {
      setLoading(false);
    }
  }

  const tabItems: { id: SettingsTab; label: string; icon: typeof Key }[] = [
    { id: "api", label: "API 配置", icon: Key },
    { id: "models", label: "模型配置", icon: Cpu },
    { id: "memory", label: "记忆配置", icon: Brain },
    { id: "profile", label: "个人信息", icon: User },
  ];

  return (
    <div className="max-w-2xl mx-auto p-8">
      <h2 className="text-xl font-bold text-slate-900 mb-2">设置</h2>
      <p className="text-xs text-slate-400 mb-6">
        配置 API Key、模型和记忆 — 这些设置对所有 Agent 生效
      </p>

      {/* ── Bootstrap Warning ── */}
      {!bootstrapOk && (
        <div className="mb-6 p-4 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-medium text-amber-800">需要完成初始配置</p>
            <p className="text-xs text-amber-600 mt-1">
              在下方「API 配置」中填入你的 API Key 后即可开始使用。
              如果你还没有 API Key，请前往你的模型服务商获取。
            </p>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-slate-200 mb-8">
        {tabItems.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "flex items-center gap-2 px-4 py-2.5 text-sm transition-all duration-150 border-b-2 -mb-px",
              tab === t.id
                ? "border-slate-900 text-slate-900 font-medium"
                : "border-transparent text-slate-500 hover:text-slate-700"
            )}
          >
            <t.icon className="w-4 h-4" /> {t.label}
          </button>
        ))}
      </div>

      {/* ═══ API 配置 ═══ */}
      {tab === "api" && (
        <div className="space-y-5 animate-fade-in">
          <div className="p-4 bg-blue-50 border border-blue-200 rounded-xl text-xs text-blue-700">
            <strong>🔑 API Key</strong> 是 LLM 调用的凭证。你的 Key 仅存储在服务器上，不会泄露给第三方。
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              API Key <span className="text-red-400">*</span>
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-your-api-key-here"
              className={cn(
                "w-full px-3.5 py-2.5 text-sm border rounded-xl input-focus font-mono",
                !apiKey ? "border-red-200 bg-red-50" : "border-slate-200"
              )}
            />
            {!apiKey && (
              <p className="text-[10px] text-red-400 mt-1">未设置 — LLM 调用将失败</p>
            )}
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">Base URL</label>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.openai.com/v1"
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
            />
            <p className="text-[10px] text-slate-400 mt-1">
              OpenAI: https://api.openai.com/v1 · 通义千问: https://dashscope.aliyuncs.com/compatible-mode/v1
            </p>
          </div>

          <div className="flex items-center gap-3">
            <Shield className="w-4 h-4 text-slate-400" />
            <span className="text-xs text-slate-500">
              配置保存在{" "}
              <code className="bg-slate-100 px-1 rounded">
                ~/.multiagent-studio/users/&lt;uid&gt;/config.yaml
              </code>
            </span>
          </div>
        </div>
      )}

      {/* ═══ 模型配置 ═══ */}
      {tab === "models" && (
        <div className="space-y-5 animate-fade-in">
          <div className="p-3 bg-slate-50 border border-slate-200 rounded-xl text-xs text-slate-600">
            为不同用途配置不同模型可节省成本：主聊天用强模型，摘要/标题/记忆用轻量模型。<br />
            <strong>留空 = 回退到默认模型</strong>
          </div>

          {/* 默认模型 (主聊天) */}
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              默认模型 <span className="text-red-400">*</span>
            </label>
            <p className="text-[10px] text-slate-400 mb-1.5">
              主聊天对话使用的模型，也是其他模型的回退值
            </p>
            <select
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
            >
              {MODEL_OPTIONS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>

          {/* 摘要模型 */}
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              摘要模型
            </label>
            <p className="text-[10px] text-slate-400 mb-1.5">
              长对话上下文压缩时使用，建议用轻量模型节省成本
            </p>
            <select
              value={summaryModel}
              onChange={(e) => setSummaryModel(e.target.value)}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
            >
              <option value="">与默认模型相同</option>
              {MODEL_OPTIONS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>

          {/* 标题模型 */}
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              标题生成模型
            </label>
            <p className="text-[10px] text-slate-400 mb-1.5">
              自动生成对话标题时使用，建议用轻量模型
            </p>
            <select
              value={titleModel}
              onChange={(e) => setTitleModel(e.target.value)}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
            >
              <option value="">与默认模型相同</option>
              {MODEL_OPTIONS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>

          {/* 记忆模型 */}
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              记忆更新模型
            </label>
            <p className="text-[10px] text-slate-400 mb-1.5">
              提取和更新用户记忆时使用，建议用轻量模型
            </p>
            <select
              value={memoryModel}
              onChange={(e) => setMemoryModel(e.target.value)}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
            >
              <option value="">与默认模型相同</option>
              {MODEL_OPTIONS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </div>

          {/* 功能开关 */}
          <div className="border-t border-slate-100 pt-4 space-y-3">
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={summarizationEnabled}
                onChange={(e) => setSummarizationEnabled(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-slate-300"
              />
              <span className="text-sm text-slate-600">启用对话摘要 (长上下文自动压缩)</span>
            </label>
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={titleEnabled}
                onChange={(e) => setTitleEnabled(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-slate-300"
              />
              <span className="text-sm text-slate-600">启用对话标题自动生成</span>
            </label>
          </div>
        </div>
      )}

      {/* ═══ 记忆配置 ═══ */}
      {tab === "memory" && (
        <div className="space-y-5 animate-fade-in">
          <div className="p-3 bg-slate-50 border border-slate-200 rounded-xl text-xs text-slate-600">
            记忆系统在对话过程中提取用户偏好和重要信息，下次对话时自动注入到 System Prompt 中。
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              记忆注入最大 Token 数
            </label>
            <p className="text-[10px] text-slate-400 mb-1.5">
              注入 System Prompt 的记忆内容最大 token 数 (100-8000)
            </p>
            <input
              type="number"
              value={memoryMaxInjectionTokens}
              onChange={(e) => setMemoryMaxInjectionTokens(Number(e.target.value))}
              min={100}
              max={8000}
              step={100}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              记忆更新防抖 (秒)
            </label>
            <p className="text-[10px] text-slate-400 mb-1.5">
              对话结束后等待 N 秒再进行记忆提取，避免频繁调用 (30-300)
            </p>
            <input
              type="number"
              value={memoryDebounceSeconds}
              onChange={(e) => setMemoryDebounceSeconds(Number(e.target.value))}
              min={30}
              max={300}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              事实置信度阈值
            </label>
            <p className="text-[10px] text-slate-400 mb-1.5">
              只有置信度 ≥ 该值的记忆才会被存储 (0.0-1.0)
            </p>
            <input
              type="number"
              value={memoryFactConfidenceThreshold}
              onChange={(e) => setMemoryFactConfidenceThreshold(Number(e.target.value))}
              min={0}
              max={1}
              step={0.05}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
            />
          </div>
        </div>
      )}

      {/* ═══ Profile ═══ */}
      {tab === "profile" && (
        <div className="space-y-4 animate-fade-in">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">显示名称</label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
            />
          </div>
        </div>
      )}

      {/* ── Save ── */}
      <div className="mt-8 flex items-center gap-3">
        <button
          onClick={saveAll}
          disabled={loading}
          className="flex items-center gap-2 px-5 py-2.5 bg-slate-900 text-white rounded-xl text-sm hover:bg-slate-800 transition-colors disabled:opacity-50 shadow-sm font-medium"
        >
          {saved ? (
            <><CheckCircle className="w-4 h-4" /> 已保存</>
          ) : (
            <><Save className="w-4 h-4" /> {loading ? "保存中..." : "保存设置"}</>
          )}
        </button>
      </div>
    </div>
  );
}
