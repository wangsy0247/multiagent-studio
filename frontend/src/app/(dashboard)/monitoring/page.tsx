"use client";

import UsageStatsView from "@/components/monitoring/UsageStatsView";

export default function MonitoringPage() {
  return (
    <div className="h-full overflow-y-auto bg-slate-50/50">
      <div className="max-w-3xl mx-auto p-8 space-y-5">
        <div>
          <h2 className="text-xl font-bold font-display text-slate-900">用量监控</h2>
          <p className="text-xs text-slate-400 mt-1">
            全部会话的 Token 消耗统计（含标题、摘要、记忆等旁路调用），每 10 秒自动刷新
          </p>
        </div>
        <UsageStatsView scope="all" />
      </div>
    </div>
  );
}
