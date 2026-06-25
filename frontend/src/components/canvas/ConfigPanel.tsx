"use client";

import { useState } from "react";
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

  return (
    <aside className="w-80 border-l bg-card flex-shrink-0 overflow-y-auto">
      <div className="flex items-center justify-between p-4 border-b sticky top-0 bg-card">
        <h3 className="text-sm font-semibold">
          {isEntryPoint ? "Lead Agent 配置" : "SubAgent 配置"}
        </h3>
        <button onClick={onClose} className="p-1 rounded hover:bg-accent">
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="p-4 space-y-4">
        {/* name */}
        <div>
          <label className="block text-xs font-medium mb-1">name (内部标识)</label>
          <input
            type="text"
            value={config.name}
            onChange={(e) => update("name", e.target.value)}
            disabled={isEntryPoint}
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:opacity-50"
            placeholder="agent_name"
          />
        </div>

        {/* display_name */}
        <div>
          <label className="block text-xs font-medium mb-1">display_name (显示名称)</label>
          <input
            type="text"
            value={config.display_name}
            onChange={(e) => update("display_name", e.target.value)}
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20"
          />
        </div>

        {/* description */}
        <div>
          <label className="block text-xs font-medium mb-1">description</label>
          <textarea
            value={config.description}
            onChange={(e) => update("description", e.target.value)}
            rows={2}
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20 resize-none"
          />
        </div>

        {/* system_prompt */}
        <div>
          <label className="block text-xs font-medium mb-1">system_prompt</label>
          <textarea
            value={config.system_prompt}
            onChange={(e) => update("system_prompt", e.target.value)}
            rows={6}
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20 resize-none font-mono"
          />
        </div>

        {/* model */}
        <div>
          <label className="block text-xs font-medium mb-1">model</label>
          <select
            value={config.model}
            onChange={(e) => update("model", e.target.value)}
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20"
          >
            <option value="inherit">继承父Agent</option>
            <option value="gpt-4o">gpt-4o</option>
            <option value="gpt-4o-mini">gpt-4o-mini</option>
            <option value="claude-sonnet-4-6">Claude Sonnet 4</option>
            <option value="claude-fable-5">Claude Fable 5</option>
            <option value="qwen3.6-plus">通义千问 3.6 Plus</option>
          </select>
        </div>

        {/* temperature */}
        <div>
          <label className="block text-xs font-medium mb-1">
            temperature: {config.temperature}
          </label>
          <input
            type="range"
            min="0"
            max="2"
            step="0.1"
            value={config.temperature}
            onChange={(e) => update("temperature", parseFloat(e.target.value))}
            className="w-full"
          />
        </div>

        {/* max_turns */}
        <div>
          <label className="block text-xs font-medium mb-1">max_turns</label>
          <input
            type="number"
            min={1}
            max={50}
            value={config.max_turns}
            onChange={(e) => update("max_turns", parseInt(e.target.value) || 10)}
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20"
          />
        </div>

        {/* tools */}
        <div>
          <label className="block text-xs font-medium mb-1">tools (逗号分隔)</label>
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
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20"
          />
        </div>

        {/* disallowed_tools */}
        <div>
          <label className="block text-xs font-medium mb-1">disallowed_tools (逗号分隔)</label>
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
            className="w-full px-2 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-primary/20"
          />
        </div>
      </div>
    </aside>
  );
}
