"use client";

import { useTeamStore } from "@/lib/team-store";
import type { AgentDefinition } from "@/lib/types";

interface TeamMessageFeedProps {
  agents: AgentDefinition[];
}

export function TeamMessageFeed({ agents }: TeamMessageFeedProps) {
  const messages = useTeamStore((state) => state.messages);

  const displayNames: Record<string, string> = {};
  agents.forEach((a) => (displayNames[a.name] = a.display_name || a.name));

  if (messages.length === 0) return null;

  return (
    <div className="bg-white border border-slate-200 rounded-lg p-3 max-h-64 overflow-y-auto">
      <h3 className="text-sm font-semibold text-slate-700 mb-2">团队消息</h3>
      <div className="space-y-2">
        {messages.slice(-20).map((msg) => (
          <div key={msg.id} className="text-sm">
            <p className="text-xs text-slate-400">
              {displayNames[msg.from_agent] || msg.from_agent}
              {msg.to_agent
                ? ` → ${displayNames[msg.to_agent] || msg.to_agent}`
                : " → 全员"}
            </p>
            <p className="text-slate-700">{msg.content}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
