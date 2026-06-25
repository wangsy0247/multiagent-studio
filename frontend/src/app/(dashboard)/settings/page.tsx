"use client";

import { useState, useEffect } from "react";
import { Save, CheckCircle } from "lucide-react";
import { configsAPI, authAPI } from "@/lib/api-client";
import { cn } from "@/lib/utils";

type SettingsTab = "profile" | "models" | "tools" | "mcp";

export default function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>("profile");
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(false);

  // Profile
  const [displayName, setDisplayName] = useState("");

  // Models
  const [defaultModel, setDefaultModel] = useState("gpt-4o");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");

  // Tools
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

  const tabs: { id: SettingsTab; label: string }[] = [
    { id: "profile", label: "个人信息" },
    { id: "models", label: "模型配置" },
    { id: "tools", label: "工具设置" },
    { id: "mcp", label: "MCP 配置" },
  ];

  const availableTools = ["web_search", "python", "bash", "file_read", "file_write", "list_files", "arxiv_search", "chart_generate", "csv_process"];

  return (
    <div className="max-w-2xl mx-auto p-6">
      <h2 className="text-lg font-semibold mb-6">设置</h2>

      {/* Tabs */}
      <div className="flex gap-1 border-b mb-6">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "px-4 py-2 text-sm transition border-b-2 -mb-px",
              tab === t.id
                ? "border-primary text-primary font-medium"
                : "border-transparent text-muted-foreground hover:text-foreground"
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Profile */}
      {tab === "profile" && (
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">显示名称</label>
            <input
              type="text"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="w-full px-3 py-2 text-sm border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/20"
            />
          </div>
        </div>
      )}

      {/* Models */}
      {tab === "models" && (
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">默认模型</label>
            <select
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              className="w-full px-3 py-2 text-sm border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/20"
            >
              <option value="gpt-4o">GPT-4o</option>
              <option value="gpt-4o-mini">GPT-4o-mini</option>
              <option value="claude-sonnet-4-6">Claude Sonnet 4</option>
              <option value="claude-fable-5">Claude Fable 5</option>
              <option value="qwen3.6-plus">通义千问 3.6 Plus</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">API Key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
              className="w-full px-3 py-2 text-sm border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/20"
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Base URL</label>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://api.openai.com/v1"
              className="w-full px-3 py-2 text-sm border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/20"
            />
          </div>
        </div>
      )}

      {/* Tools */}
      {tab === "tools" && (
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-2">已启用工具</label>
            <div className="grid grid-cols-2 gap-2">
              {availableTools.map((tool) => (
                <label key={tool} className="flex items-center gap-2 p-2 border rounded-lg text-sm cursor-pointer hover:bg-accent">
                  <input
                    type="checkbox"
                    checked={toolsEnabled.includes(tool)}
                    onChange={(e) => {
                      if (e.target.checked) setToolsEnabled([...toolsEnabled, tool]);
                      else setToolsEnabled(toolsEnabled.filter((t) => t !== tool));
                    }}
                    className="rounded"
                  />
                  {tool}
                </label>
              ))}
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">
              最大并发 SubAgent: {maxSubagents}
            </label>
            <input
              type="range"
              min={1}
              max={8}
              value={maxSubagents}
              onChange={(e) => setMaxSubagents(parseInt(e.target.value))}
              className="w-full"
            />
          </div>
        </div>
      )}

      {/* MCP */}
      {tab === "mcp" && (
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">MCP 配置 (JSON)</label>
            <textarea
              rows={10}
              placeholder='{"mcpServers": {...}}'
              className="w-full px-3 py-2 text-xs font-mono border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary/20"
            />
          </div>
        </div>
      )}

      {/* 保存 */}
      <div className="mt-8 flex items-center gap-3">
        <button
          onClick={saveConfig}
          disabled={loading}
          className="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm hover:bg-primary/90 disabled:opacity-50"
        >
          {saved ? <CheckCircle className="w-4 h-4" /> : <Save className="w-4 h-4" />}
          {saved ? "已保存" : loading ? "保存中..." : "保存设置"}
        </button>
      </div>
    </div>
  );
}
