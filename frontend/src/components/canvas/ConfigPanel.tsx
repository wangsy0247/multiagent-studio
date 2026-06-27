"use client";

import { X } from "lucide-react";
import { SubAgentConfig } from "@/lib/types";
import { useCanvasStore } from "@/lib/canvas-store";

interface ConfigPanelProps {
  nodeId: string;
  config: SubAgentConfig;
  isEntryPoint: boolean;
  onClose: () => void;
}

export default function ConfigPanel({ nodeId, config, isEntryPoint, onClose }: ConfigPanelProps) {
  const { updateNodeConfig } = useCanvasStore();

  function update<K extends keyof SubAgentConfig>(key: K, value: SubAgentConfig[K]) {
    updateNodeConfig(nodeId, { [key]: value });
  }

  const tempLabels = ["0 精确", "1 平衡", "2 创意"];

  return (
    <aside className="w-80 border-l bg-white flex-shrink-0 overflow-y-auto">
      <div className="flex items-center justify-between p-4 border-b sticky top-0 bg-white z-10">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">
            {isEntryPoint ? "Lead Agent 配置" : "SubAgent 配置"}
          </h3>
          {isEntryPoint && (
            <p className="text-[11px] text-slate-400 mt-0.5">name 和 role 不可编辑</p>
          )}
        </div>
        <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors">
          <X className="w-4 h-4 text-slate-500" />
        </button>
      </div>

      <div className="p-4 space-y-5">
        {/* Section: Basic */}
        <div>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3">基本信息</p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">name (内部标识)</label>
              <input
                type="text"
                value={config.name}
                onChange={(e) => update("name", e.target.value)}
                disabled={isEntryPoint}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus disabled:bg-slate-50 disabled:text-slate-400"
                placeholder="agent_name"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">display_name (显示名称)</label>
              <input
                type="text"
                value={config.display_name}
                onChange={(e) => update("display_name", e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">description</label>
              <textarea
                value={config.description}
                onChange={(e) => update("description", e.target.value)}
                rows={2}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus resize-none"
              />
            </div>
          </div>
        </div>

        {/* Section: Prompt */}
        <div>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3">提示词</p>
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">system_prompt</label>
            <textarea
              value={config.system_prompt}
              onChange={(e) => update("system_prompt", e.target.value)}
              rows={6}
              className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus resize-none font-mono"
            />
          </div>
        </div>

        {/* Section: Model */}
        <div>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3">模型设置</p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">model</label>
              <select
                value={config.model}
                onChange={(e) => update("model", e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus bg-white"
              >
                <option value="inherit">继承父Agent</option>
                <option value="gpt-4o">gpt-4o</option>
                <option value="gpt-4o-mini">gpt-4o-mini</option>
                <option value="claude-sonnet-4-6">Claude Sonnet 4</option>
                <option value="claude-fable-5">Claude Fable 5</option>
                <option value="qwen3.6-plus">通义千问 3.6 Plus</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                temperature: {config.temperature}
              </label>
              <input
                type="range"
                min="0"
                max="2"
                step="0.1"
                value={config.temperature}
                onChange={(e) => update("temperature", parseFloat(e.target.value))}
                className="w-full accent-slate-700"
              />
              <div className="flex justify-between text-[10px] text-slate-400 mt-0.5">
                {tempLabels.map((l) => <span key={l}>{l}</span>)}
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">max_turns</label>
              <input
                type="number"
                min={1}
                max={50}
                value={config.max_turns}
                onChange={(e) => update("max_turns", parseInt(e.target.value) || 10)}
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus"
              />
            </div>
          </div>
        </div>

        {/* Section: Tools */}
        <div>
          <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-3">工具</p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">tools (逗号分隔)</label>
              <input
                type="text"
                value={config.tools?.join(", ") || ""}
                onChange={(e) =>
                  update(
                    "tools",
                    e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean)
                  )
                }
                placeholder="web_search, python, file_read"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">disallowed_tools (逗号分隔)</label>
              <input
                type="text"
                value={config.disallowed_tools?.join(", ") || ""}
                onChange={(e) =>
                  update(
                    "disallowed_tools",
                    e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean)
                  )
                }
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus"
              />
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}
