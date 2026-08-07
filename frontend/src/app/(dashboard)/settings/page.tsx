"use client";

import { useState, useEffect } from "react";
import { Save, CheckCircle, User, Brain, AlertTriangle } from "lucide-react";
import { authAPI, getCurrentUserId } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import apiClient from "@/lib/api-client";

type SettingsTab = "memory" | "profile";

// 文件系统 user_id 统一为 username — 与 api-client.getCurrentUserId 保持一致
function getUserId(): string {
  return getCurrentUserId();
}

export default function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>("memory");
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [loading, setLoading] = useState(false);
  const [bootstrapOk, setBootstrapOk] = useState(true);

  const [displayName, setDisplayName] = useState("");
  // ── 记忆配置 ──
  const [memoryMaxFacts, setMemoryMaxFacts] = useState(100);
  const [memoryTtlDays, setMemoryTtlDays] = useState(90);
  const [memoryMaxInjectionTokens, setMemoryMaxInjectionTokens] = useState(500);
  const [memoryDebounceSeconds, setMemoryDebounceSeconds] = useState(120);
  const [memoryFactConfidenceThreshold, setMemoryFactConfidenceThreshold] = useState(0.7);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    // 1. Profile
    try {
      const { data: user } = await authAPI.getMe();
      setDisplayName(user.display_name || "");
    } catch {}

    // 2. L1 user global config (模型 API 由服务器统一配置, 此处只有记忆策略)
    try {
      const uid = getUserId();
      const { data } = await apiClient.get(`/v1/agents/config/global?user_id=${uid}`);
      if (data.exists) {
        const cfg = data.config;
        const mem = cfg.memory || {};
        setMemoryMaxFacts(mem.max_facts ?? 100);
        setMemoryTtlDays(mem.ttl_days ?? 90);
        setMemoryMaxInjectionTokens(mem.max_injection_tokens ?? 500);
        setMemoryDebounceSeconds(mem.debounce_seconds ?? 120);
        setMemoryFactConfidenceThreshold(mem.fact_confidence_threshold ?? 0.7);
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
    setSaveError("");
    try {
      // Save profile
      await authAPI.updateMe({ display_name: displayName });

      // Save L1 global config (仅记忆配置; 模型 API 为服务器管理字段, 后端会忽略)
      const uid = getUserId();
      const config: Record<string, unknown> = {
        memory: {
          max_facts: memoryMaxFacts,
          ttl_days: memoryTtlDays,
          max_injection_tokens: memoryMaxInjectionTokens,
          debounce_seconds: memoryDebounceSeconds,
          fact_confidence_threshold: memoryFactConfidenceThreshold,
        },
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
      setSaveError("保存失败，请检查网络或稍后重试");
    } finally {
      setLoading(false);
    }
  }

  const tabItems: { id: SettingsTab; label: string; icon: typeof Brain }[] = [
    { id: "memory", label: "记忆配置", icon: Brain },
    { id: "profile", label: "个人信息", icon: User },
  ];

  return (
    <div className="h-full overflow-y-auto">
    <div className="max-w-2xl mx-auto p-8">
      <h2 className="text-xl font-bold font-display text-slate-900 mb-2">设置</h2>
      <p className="text-xs text-slate-400 mb-6">
        配置记忆策略和个人信息 — 模型与 API 由服务器统一提供
      </p>

      {/* ── Bootstrap Warning (服务器模型 API 未配置) ── */}
      {!bootstrapOk && (
        <div className="mb-6 p-4 bg-amber-50 border border-amber-200 rounded-xl flex items-start gap-3">
          <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-medium text-amber-800">服务器模型 API 未配置</p>
            <p className="text-xs text-amber-600 mt-1">
              模型调用暂时不可用。请联系管理员在服务器的 harness/.env 中配置
              OPENAI_API_KEY 后重启服务。
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

      {/* ═══ 记忆配置 ═══ */}
      {tab === "memory" && (
        <div className="space-y-5 animate-fade-in">
          <div className="p-3 bg-slate-50 border border-slate-200 rounded-xl text-xs text-slate-600">
            记忆系统在对话过程中提取用户偏好和重要信息，下次对话时自动注入到 System Prompt 中。
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">
                最大事实数
              </label>
              <p className="text-[10px] text-slate-400 mb-1.5">
                保留最新的 N 条事实 (10-500)，超出后淘汰最旧的
              </p>
              <input
                type="number"
                value={memoryMaxFacts}
                onChange={(e) => setMemoryMaxFacts(Number(e.target.value))}
                min={10}
                max={500}
                step={10}
                className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">
                事实过期天数
              </label>
              <p className="text-[10px] text-slate-400 mb-1.5">
                超过 N 天的事实自动清理 (0=永不过期)
              </p>
              <input
                type="number"
                value={memoryTtlDays}
                onChange={(e) => setMemoryTtlDays(Number(e.target.value))}
                min={0}
                max={730}
                step={30}
                className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
              />
            </div>
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
        {saveError && <p className="text-xs text-red-500">{saveError}</p>}
      </div>
    </div>
    </div>
  );
}
