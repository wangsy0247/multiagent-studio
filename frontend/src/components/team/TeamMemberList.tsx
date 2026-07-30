"use client";

import { useTeamStore } from "@/lib/team-store";
import type { AgentDefinition } from "@/lib/types";

interface TeamMemberListProps {
  agents: AgentDefinition[];
}

/** 成员状态标签 — 对齐后端 TeammateStatus 枚举 */
const statusLabels: Record<string, string> = {
  spawning: "启动中",
  idle: "空闲",
  working: "执行中",
  busy: "执行中", // 后端枚举外取值, 语义等同 working
  shutting_down: "关闭中",
  shutdown: "已关闭",
  failed: "失败",
};

export function TeamMemberList({ agents }: TeamMemberListProps) {
  const members = useTeamStore((state) => state.members);

  if (agents.length === 0) {
    return (
      <div className="bg-white border border-slate-200 rounded-lg p-3">
        <h3 className="text-sm font-semibold text-slate-700 mb-2">团队成员</h3>
        <p className="text-xs text-slate-400">无成员</p>
      </div>
    );
  }

  return (
    <div className="bg-white border border-slate-200 rounded-lg p-3">
      <h3 className="text-sm font-semibold text-slate-700 mb-2">团队成员</h3>
      <div className="space-y-2">
        {agents.map((agent) => {
          const runtime = members.find((m) => m.agent_name === agent.name);
          const status = runtime?.status || "idle";
          return (
            <div
              key={agent.name}
              className="flex items-center justify-between text-sm"
            >
              <div className="flex items-center gap-2">
                <span
                  className={`w-2 h-2 rounded-full ${
                    status === "spawning"
                      ? "bg-slate-300 animate-pulse"
                      : status === "idle"
                        ? "bg-slate-300"
                        : status === "working" || status === "busy"
                          ? "bg-hermes-500 animate-pulse"
                          : status === "shutting_down"
                            ? "bg-amber-400 animate-pulse"
                            : status === "shutdown"
                              ? "bg-slate-400"
                              : "bg-red-500"
                  }`}
                />
                <span className="font-medium text-slate-700 truncate max-w-[100px]">
                  {agent.display_name || agent.name}
                </span>
              </div>
              <div className="text-right">
                <p className="text-xs text-slate-500">{statusLabels[status]}</p>
                {runtime?.current_task_title && (
                  <p className="text-xs text-slate-400 truncate max-w-[120px]">
                    {runtime.current_task_title}
                  </p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
