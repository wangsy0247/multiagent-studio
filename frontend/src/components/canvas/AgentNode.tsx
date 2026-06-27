"use client";

import { memo } from "react";
import { Handle, Position, NodeProps } from "reactflow";
import { Bot, Workflow, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useCanvasStore } from "@/lib/canvas-store";

const AgentNodeComponent = memo(({ data, id, selected }: NodeProps) => {
  const { removeNode, selectNode } = useCanvasStore();
  const isLead = data.isEntryPoint;
  const config = data.config;
  const status = data.status || "idle";

  const statusColors: Record<string, string> = {
    idle: "bg-slate-300",
    running: "bg-green-500 animate-pulse",
    done: "bg-blue-400",
    error: "bg-red-500",
  };

  return (
    <div
      className={cn(
        "bg-white rounded-xl shadow-sm min-w-[180px] cursor-pointer transition-all duration-150 border-2",
        selected ? "border-blue-500 ring-2 ring-blue-500/20" : "border-slate-200 hover:border-slate-300 hover:shadow-md",
        isLead && "border-slate-400"
      )}
      onClick={() => selectNode(id)}
    >
      {/* Header */}
      <div
        className={cn(
          "flex items-center gap-2 px-3 py-2.5 rounded-t-[10px]",
          isLead ? "bg-slate-100" : "bg-emerald-50"
        )}
      >
        <div className={cn(
          "w-7 h-7 rounded-lg flex items-center justify-center",
          isLead ? "bg-slate-800" : "bg-emerald-500"
        )}>
          {isLead ? (
            <Workflow className="w-3.5 h-3.5 text-white" />
          ) : (
            <Bot className="w-3.5 h-3.5 text-white" />
          )}
        </div>
        <span className="text-sm font-medium flex-1 truncate text-slate-800">
          {config.display_name || config.name || (isLead ? "Lead Agent" : "SubAgent")}
        </span>
        <span className={cn("w-2 h-2 rounded-full", statusColors[status])} />
        <button
          onClick={(e) => {
            e.stopPropagation();
            removeNode(id);
          }}
          className="p-1 rounded-md hover:bg-red-100 text-slate-400 hover:text-red-500 transition-colors"
        >
          <Trash2 className="w-3 h-3" />
        </button>
      </div>

      {/* Content */}
      <div className="px-3 py-2 text-xs space-y-1.5">
        {config.description && (
          <p className="text-slate-500 truncate">{config.description}</p>
        )}
        {config.model && config.model !== "inherit" && (
          <p className="text-slate-400">模型: {config.model}</p>
        )}
        {config.tools && config.tools.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {config.tools.slice(0, 3).map((t: string) => (
              <span key={t} className="px-1.5 py-0.5 bg-slate-100 rounded text-[10px] text-slate-600">
                {t}
              </span>
            ))}
            {config.tools.length > 3 && (
              <span className="text-[10px] text-slate-400">+{config.tools.length - 3}</span>
            )}
          </div>
        )}
      </div>

      {!isLead && <Handle type="target" position={Position.Top} className="!bg-slate-400 !w-2.5 !h-2.5" />}
      <Handle type="source" position={Position.Bottom} className="!bg-slate-400 !w-2.5 !h-2.5" />
    </div>
  );
});

AgentNodeComponent.displayName = "AgentNodeComponent";
export default AgentNodeComponent;
