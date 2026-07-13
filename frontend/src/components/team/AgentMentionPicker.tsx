"use client";

import { useMemo } from "react";
import type { AgentDefinition } from "@/lib/types";

interface AgentMentionPickerProps {
  members: AgentDefinition[];
  query: string;
  selectedIndex?: number;
  onSelect: (agentName: string) => void;
}

export function AgentMentionPicker({
  members,
  query,
  selectedIndex = 0,
  onSelect,
}: AgentMentionPickerProps) {
  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return members.filter(
      (m) =>
        m.name.toLowerCase().includes(q) ||
        (m.display_name || "").toLowerCase().includes(q),
    );
  }, [members, query]);

  if (filtered.length === 0) return null;

  return (
    <div className="absolute bottom-full left-0 mb-1 w-64 max-h-48 overflow-auto bg-white border border-slate-200 rounded-lg shadow-lg z-50">
      {filtered.map((m, idx) => (
        <button
          key={m.name}
          type="button"
          onMouseDown={(e) => {
            e.preventDefault(); // 防止 blur 导致先关闭
            onSelect(m.name);
          }}
          className={`w-full text-left px-3 py-2 text-sm hover:bg-slate-50 flex flex-col ${
            idx === selectedIndex ? "bg-slate-100" : ""
          }`}
        >
          <span className="font-medium text-slate-700">
            @{m.display_name || m.name}
          </span>
          {m.description && (
            <span className="text-xs text-slate-400 truncate">
              {m.description.slice(0, 60)}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}
