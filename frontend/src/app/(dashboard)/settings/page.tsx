"use client";

import { useState, useEffect } from "react";
import { Save, CheckCircle, User, Cpu, Wrench, Server } from "lucide-react";
import { configsAPI, authAPI } from "@/lib/api-client";
import { cn } from "@/lib/utils";

type SettingsTab = "profile" | "models" | "tools" | "mcp";

export default function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>("profile");
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(false);

  const [displayName, setDisplayName] = useState("");
  const [defaultModel, setDefaultModel] = useState("gpt-4o");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [toolsEnabled, setToolsEnabled] = useState<string[]>([]);
  const [maxSubagents, setMaxSubagents] = useState(3);

  useEffect(() => {
    loadConfig();
  }, []);

  async function loadConfig() {
    try {
      const { data: user } = await authAPI.getMe();
      setDisplayName(user.display_name || "");
    } catch {}

    try {
      const { data: config } = await configsAPI.get();
      setDefaultModel(config.default_model || "gpt-4o");
      setToolsEnabled(config.tools_enabled || []);
      setMaxSubagents(config.max_concurrent_subagents || 3);
    } catch {}
  }

  async function saveConfig() {
    setLoading(true);
    try {
      await authAPI.updateMe({ display_name: displayName });
      await configsAPI.update({
        default_model: defaultModel,
        tools_enabled: toolsEnabled,
        max_concurrent_subagents: maxSubagents,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      console.error("保存配置失败", err);
    } finally {
      setLoading(false);
    }
  }

  const tabItems: { id: SettingsTab; label: string; icon: typeof User }[] = [
    { id: "profile", label: "个人信息", icon: User },
    { id: "models", label: "模型配置", icon: Cpu },
    { id: "tools", label: "工具设置", icon: Wrench },
    { id: "mcp", label: "MCP 配置", icon: Server },
  ];

  const availableTools = ["web_search", "python", "bash", "file_read", "file_write", "list_files", "arxiv_search", "chart_generate", "csv_process"];

  return (
    <div className="max-w-2xl mx-auto p-8">
      <h2 className="text-xl font-bold text-slate-900 mb-6">设置</h2>

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
            <t.icon className="w-4 h-4" />
            {t.label}
          </button>
        ))}
      </div>

      {/* Profile */}
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

      {/* Models */}
      {tab === "models" && (
        <div className="space-y-4 animate-fade-in">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">默认模型</label>
            <select
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus bg-white"
            >
              <option value="gpt-4o">GPT-4o</option>
              <option value="gpt-4o-mini">GPT-4o-mini</option>
              <option value="claude-sonnet-4-6">Claude Sonnet 4</option>
              <option value="claude-fable-5">Claude Fable 5</option>
              <option value="qwen3.6-plus">通义千问 3.6 Plus</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">API Key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              className="w-full px-3.5 py-2.5 text-sm border border-slate-200 rounded-xl input-focus"
            />
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
          </div>
        </div>
      )}

      {/* Tools */}
      {tab === "tools" && (
        <div className="space-y-4 animate-fade-in">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-2.5">已启用工具</label>
            <div className="grid grid-cols-2 gap-2">
              {availableTools.map((tool) => {
                const checked = toolsEnabled.includes(tool);
                return (
                  <label
                    key={tool}
                    className={cn(
                      "flex items-center gap-2.5 p-3 border rounded-xl text-sm cursor-pointer transition-all duration-150",
                      checked
                        ? "bg-slate-900 text-white border-slate-900"
                        : "bg-white border-slate-200 hover:border-slate-300 text-slate-700"
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => {
                        if (e.target.checked) setToolsEnabled([...toolsEnabled, tool]);
                        else setToolsEnabled(toolsEnabled.filter((t) => t !== tool));
                      }}
                      className="rounded accent-slate-900"
                    />
                    {tool}
                  </label>
                );
              })}
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">
              最大并发 SubAgent: <span className="text-slate-900">{maxSubagents}</span>
            </label>
            <input
              type="range"
              min={1}
              max={8}
              value={maxSubagents}
              onChange={(e) => setMaxSubagents(parseInt(e.target.value))}
              className="w-full accent-slate-700"
            />
            <div className="flex justify-between text-[10px] text-slate-400 mt-1">
              <span>1</span><span>4</span><span>8</span>
            </div>
          </div>
        </div>
      )}

      {/* MCP */}
      {tab === "mcp" && (
        <div className="space-y-4 animate-fade-in">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1.5">MCP 配置 (JSON)</label>
            <textarea
              rows={10}
              placeholder='{"mcpServers": {...}}'
              className="w-full px-3.5 py-2.5 text-xs font-mono border border-slate-200 rounded-xl input-focus"
            />
          </div>
        </div>
      )}

      {/* Save button */}
      <div className="mt-8 flex items-center gap-3">
        <button
          onClick={saveConfig}
          disabled={loading}
          className="flex items-center gap-2 px-5 py-2.5 bg-slate-900 text-white rounded-xl text-sm hover:bg-slate-800 transition-colors disabled:opacity-50 shadow-sm font-medium"
        >
          {saved ? (
            <>
              <CheckCircle className="w-4 h-4" />
              已保存
            </>
          ) : (
            <>
              <Save className="w-4 h-4" />
              {loading ? "保存中..." : "保存设置"}
            </>
          )}
        </button>
      </div>
    </div>
  );
}
