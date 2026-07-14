"use client";

import { useState, useEffect } from "react";
import { ChevronDown, Bot } from "lucide-react";
import { agentsAPI } from "@/lib/api-client";
import { AgentDefinition } from "@/lib/types";
import { cn } from "@/lib/utils";

interface AgentSelectorProps {
  value: string;
  onChange: (agentName: string) => void;
  className?: string;
}

export default function AgentSelector({ value, onChange, className }: AgentSelectorProps) {
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    agentsAPI
      .list()
      .then(({ data }) => {
        if (data?.agents) setAgents(data.agents);
      })
      .catch(() => {});
  }, []);

  const selected = agents.find((a) => a.name === value);
  const label = selected?.display_name || selected?.name || value || "default";

  return (
    <div className={cn("relative", className)}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors bg-white min-w-0"
        title={`当前 Agent: ${label}`}
      >
        <Bot className="w-3 h-3 text-slate-400 flex-shrink-0" />
        <span className="text-slate-700 truncate max-w-[80px]">{label}</span>
        <ChevronDown className={cn("w-3 h-3 text-slate-400 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full left-0 mb-1 z-20 w-52 bg-white border border-slate-200 rounded-xl shadow-lg overflow-hidden">
            <div className="p-1.5 border-b border-slate-100">
              <p className="text-[10px] text-slate-400 px-2 uppercase font-medium">选择 Agent</p>
            </div>
            <div className="max-h-48 overflow-y-auto py-1">
              {agents.length === 0 && (
                <p className="text-xs text-slate-400 text-center py-3">暂无可用 Agent</p>
              )}
              {agents.map((agent) => (
                <button
                  key={agent.name}
                  onClick={() => {
                    onChange(agent.name);
                    setOpen(false);
                  }}
                  className={cn(
                    "w-full text-left px-3 py-2 text-xs transition-colors flex items-center gap-2",
                    agent.name === value
                      ? "bg-slate-100 text-slate-900 font-medium"
                      : "text-slate-600 hover:bg-slate-50"
                  )}
                >
                  <Bot className={cn(
                    "w-3 h-3 flex-shrink-0",
                    agent.name === value ? "text-slate-700" : "text-slate-400"
                  )} />
                  <div className="min-w-0">
                    <p className="truncate">{agent.display_name || agent.name}</p>
                    {agent.description && (
                      <p className="text-[10px] text-slate-400 truncate">{agent.description}</p>
                    )}
                  </div>
                  {agent.name === "default" && (
                    <span className="ml-auto text-[9px] px-1.5 py-0.5 bg-slate-200 text-slate-500 rounded-full flex-shrink-0">
                      默认
                    </span>
                  )}
                  {agent.name === value && (
                    <span className="ml-auto w-1.5 h-1.5 rounded-full bg-slate-900 flex-shrink-0" />
                  )}
                </button>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
