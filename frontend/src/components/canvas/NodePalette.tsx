"use client";

import { DragEvent } from "react";
import { Bot, Workflow, Grip } from "lucide-react";

const NODE_TYPES = [
  {
    type: "lead" as const,
    label: "Lead Agent",
    icon: Workflow,
    color: "text-primary",
    bg: "bg-primary/10",
    description: "主编排Agent",
  },
  {
    type: "subagent" as const,
    label: "SubAgent",
    icon: Bot,
    color: "text-emerald-600",
    bg: "bg-emerald-500/10",
    description: "执行子任务",
  },
];

export default function NodePalette() {
  function onDragStart(event: DragEvent, type: string) {
    event.dataTransfer.setData("application/reactflow-type", type);
    event.dataTransfer.effectAllowed = "move";
  }

  return (
    <aside className="w-48 border-l bg-card flex-shrink-0 p-3">
      <h3 className="text-sm font-medium mb-3 text-muted-foreground">节点类型</h3>
      <div className="space-y-2">
        {NODE_TYPES.map((item) => (
          <div
            key={item.type}
            draggable
            onDragStart={(e) => onDragStart(e, item.type)}
            className="flex items-center gap-2 p-3 border rounded-lg cursor-grab active:cursor-grabbing hover:shadow-md transition bg-card"
          >
            <Grip className="w-3 h-3 text-muted-foreground" />
            <div className={`p-1.5 rounded ${item.bg}`}>
              <item.icon className={`w-4 h-4 ${item.color}`} />
            </div>
            <div>
              <p className="text-xs font-medium">{item.label}</p>
              <p className="text-[10px] text-muted-foreground">{item.description}</p>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-6 p-3 bg-muted/50 rounded-lg">
        <p className="text-xs text-muted-foreground">
          拖拽节点到画布中。从 Lead Agent 连线到 SubAgent 建立执行依赖。
        </p>
      </div>
    </aside>
  );
}
