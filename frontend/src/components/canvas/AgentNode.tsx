"use client";

import { memo } from "react";
import { Handle, Position, NodeProps } from "reactflow";
import { Bot, Workflow, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useCanvasStore } from "@/lib/canvas-store";

const AgentNodeComponent = memo(({ data, id }: NodeProps) => {
  const { removeNode, selectNode } = useCanvasStore();
  const isLead = data.isEntryPoint;
  const config = data.config;
  const status = data.status || "idle";

  const statusColors: Record<string, string> = {
    idle: "bg-gray-300",
    running: "bg-green-500 animate-pulse",
    done: "bg-blue-400",
    error: "bg-red-500",
  };

  const statusBorder: Record<string, string> = {
    idle: "border-gray-300",
    running: "border-green-500 animate-pulse-glow",
    done: "border-blue-400",
    error: "border-red-500",
  };

  return (
    <div
      className={cn(
        "bg-card border-2 rounded-lg shadow-sm min-w-[180px] cursor-pointer",
        isLead ? "border-primary/50" : "border-emerald-500/50",
        status === "running" && "animate-pulse-glow"
      )}
      onClick={() => selectNode(id)}
    >
      {/* 头部 */}
      <div
        className={cn(
          "flex items-center gap-2 px-3 py-2 rounded-t-md",
          isLead ? "bg-primary/10" : "bg-emerald-500/10"
        )}
      >
        {isLead ? (
          <Workflow className="w-4 h-4 text-primary" />
        ) : (
          <Bot className="w-4 h-4 text-emerald-600" />
        )}
        <span className="text-sm font-medium flex-1 truncate">
          {config.display_name || config.name || (isLead ? "Lead Agent" : "SubAgent")}
        </span>
        <span className={cn("w-2 h-2 rounded-full", statusColors[status])} />
        <button
          onClick={(e) => {
            e.stopPropagation();
            removeNode(id);
          }}
          className="p-0.5 rounded hover:bg-destructive/10 text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="w-3 h-3" />
        </button>
      </div>

      {/* 内容 */}
      <div className="px-3 py-2 text-xs space-y-1">
        {config.model && config.model !== "inherit" && (
          <p className="text-muted-foreground">模型: {config.model}</p>
        )}
        {config.tools && config.tools.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {config.tools.slice(0, 3).map((t: string) => (
              <span key={t} className="px-1.5 py-0.5 bg-muted rounded text-[10px]">
                {t}
              </span>
            ))}
            {config.tools.length > 3 && (
              <span className="text-[10px] text-muted-foreground">+{config.tools.length - 3}</span>
            )}
          </div>
        )}
      </div>

      {/* 端口 */}
      {!isLead && <Handle type="target" position={Position.Top} className="!bg-primary !w-3 !h-3" />}
      <Handle type="source" position={Position.Bottom} className="!bg-primary !w-3 !h-3" />
    </div>
  );
});

AgentNodeComponent.displayName = "AgentNodeComponent";
export default AgentNodeComponent;
