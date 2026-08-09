"use client";

import { useState } from "react";
import { Activity } from "lucide-react";
import { cn } from "@/lib/utils";
import ErrorPanel from "./ErrorPanel";
import UsageStatsView from "./UsageStatsView";

interface MonitoringPanelProps {
  threadId: string;
}

type TabKey = "thread" | "all";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "thread", label: "当前会话" },
  { key: "all", label: "全部会话" },
];

export default function MonitoringPanel({ threadId }: MonitoringPanelProps) {
  const [tab, setTab] = useState<TabKey>("thread");

  return (
    <div className="h-full overflow-y-auto p-5 space-y-5 bg-slate-50/50">
      <div className="flex items-center gap-2 mb-1">
        <Activity className="w-4 h-4 text-slate-500" />
        <h2 className="text-sm font-semibold text-slate-800">运行监控</h2>
      </div>

      {/* 会话范围切换 */}
      <div className="flex gap-1 p-1 bg-slate-100 rounded-lg w-fit">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              "px-3 py-1 rounded-md text-xs font-medium transition-colors",
              tab === t.key
                ? "bg-white text-slate-800 shadow-sm"
                : "text-slate-500 hover:text-slate-700",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      <UsageStatsView scope={tab} threadId={threadId} />

      {/* 错误日志 (会话级, 两个 tab 共用) */}
      <section className="bg-white border border-slate-200 rounded-xl p-4">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-4">错误日志</h3>
        <ErrorPanel threadId={threadId} />
      </section>
    </div>
  );
}
