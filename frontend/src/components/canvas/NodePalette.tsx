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

export default function NodePalette() {
  function onDragStart(event: DragEvent, type: string) {
    event.dataTransfer.setData("application/reactflow-type", type);
    event.dataTransfer.effectAllowed = "move";
  }

  return (
    <aside className="w-48 border-l bg-white flex-shrink-0 p-3">
      <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">节点类型</h3>
      <div className="space-y-2">
        {NODE_TYPES.map((item) => (
          <div
            key={item.type}
            draggable
            onDragStart={(e) => onDragStart(e, item.type)}
            className="flex items-center gap-2.5 p-3 bg-white border border-slate-200 rounded-xl cursor-grab active:cursor-grabbing hover:shadow-md hover:border-slate-300 transition-all duration-150 active:scale-[0.98]"
          >
            <Grip className="w-3 h-3 text-slate-300" />
            <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${item.bg}`}>
              <item.icon className={`w-4 h-4 ${item.color}`} />
            </div>
            <div>
              <p className="text-xs font-medium text-slate-800">{item.label}</p>
              <p className="text-[10px] text-slate-400">{item.description}</p>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-4 p-3 bg-slate-50 rounded-xl border border-slate-100">
        <div className="flex items-start gap-2">
          <Info className="w-3.5 h-3.5 text-slate-400 mt-0.5 flex-shrink-0" />
          <p className="text-[11px] text-slate-500 leading-relaxed">
            拖拽节点到画布。从 Lead Agent 连线到 SubAgent 建立执行依赖。
          </p>
        </div>
      </div>
    </aside>
  );
}
