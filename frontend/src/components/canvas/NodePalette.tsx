"use client";

import { DragEvent } from "react";
import { Bot, Workflow, Grip, Info } from "lucide-react";

const NODE_TYPES = [
  {
    type: "lead" as const,
    label: "Lead Agent",
    icon: Workflow,
    color: "text-slate-700",
    bg: "bg-slate-100",
    description: "主编排Agent",
  },
  {
    type: "subagent" as const,
    label: "SubAgent",
    icon: Bot,
    color: "text-emerald-600",
    bg: "bg-emerald-50",
    description: "执行子任务",
  },
];

// ── 预设快捷类型 (拖拽时带上预设名, 在 ConfigPanel 中应用) ──
const PRESET_SHORTCUTS = [
  { key: "researcher", label: "信息检索", icon: "🔍" },
  { key: "coder", label: "代码执行", icon: "💻" },
  { key: "analyst", label: "数据分析", icon: "📊" },
  { key: "writer", label: "文档撰写", icon: "📝" },
  { key: "reviewer", label: "审查专家", icon: "🔎" },
];

export default function NodePalette() {
  function onDragStart(event: DragEvent, type: string, presetKey?: string) {
    event.dataTransfer.setData("application/reactflow-type", type);
    if (presetKey) {
      event.dataTransfer.setData("application/reactflow-preset", presetKey);
    }
    event.dataTransfer.effectAllowed = "move";
  }

  return (
    <aside className="w-52 border-l bg-white flex-shrink-0 p-3 flex flex-col">
      {/* ── 基础节点 ── */}
      <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">
        节点类型
      </h3>
      <div className="space-y-2">
        {NODE_TYPES.map((item) => (
          <div
            key={item.type}
            draggable
            onDragStart={(e) => onDragStart(e, item.type)}
            className="flex items-center gap-2.5 p-2.5 bg-white border border-slate-200 rounded-xl cursor-grab active:cursor-grabbing hover:shadow-md hover:border-slate-300 transition-all duration-150 active:scale-[0.98]"
          >
            <Grip className="w-3 h-3 text-slate-300 flex-shrink-0" />
            <div
              className={`w-7 h-7 rounded-lg flex items-center justify-center ${item.bg}`}
            >
              <item.icon className={`w-3.5 h-3.5 ${item.color}`} />
            </div>
            <div className="min-w-0">
              <p className="text-xs font-medium text-slate-800 truncate">
                {item.label}
              </p>
              <p className="text-[10px] text-slate-400">{item.description}</p>
            </div>
          </div>
        ))}
      </div>

      {/* ── 预设 SubAgent 快捷拖拽 ── */}
      <div className="mt-5">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">
          预设快捷创建
        </h3>
        <div className="space-y-1.5">
          {PRESET_SHORTCUTS.map((preset) => (
            <div
              key={preset.key}
              draggable
              onDragStart={(e) => onDragStart(e, "subagent", preset.key)}
              className="flex items-center gap-2 px-2.5 py-2 bg-slate-50 border border-slate-200 rounded-lg cursor-grab active:cursor-grabbing hover:bg-emerald-50 hover:border-emerald-200 transition-all duration-150 active:scale-[0.98]"
            >
              <span className="text-base leading-none">{preset.icon}</span>
              <span className="text-xs text-slate-700 font-medium">{preset.label}</span>
            </div>
          ))}
        </div>
      </div>

      {/* ── 提示 ── */}
      <div className="mt-auto pt-4">
        <div className="p-3 bg-slate-50 rounded-xl border border-slate-100">
          <div className="flex items-start gap-2">
            <Info className="w-3.5 h-3.5 text-slate-400 mt-0.5 flex-shrink-0" />
            <p className="text-[11px] text-slate-500 leading-relaxed">
              从上方拖拽节点到画布。从 Lead Agent 连线到 SubAgent 建立执行依赖。
            </p>
          </div>
        </div>
      </div>
    </aside>
  );
}
