"use client";

import { useState, useEffect } from "react";
import { monitoringAPI } from "@/lib/api-client";
import { TokenUsageStats } from "@/lib/types";
import TokenChart from "./TokenChart";
import ErrorPanel from "./ErrorPanel";
import { useChatStore } from "@/lib/chat-store";

interface MonitoringPanelProps {
  threadId: string;
}

export default function MonitoringPanel({ threadId }: MonitoringPanelProps) {
  const [tokenStats, setTokenStats] = useState<TokenUsageStats | null>(null);
  const [loading, setLoading] = useState(true);
  const { cumulativeTokens, tokenUsage } = useChatStore();

  useEffect(() => {
    loadStats();
  }, [threadId]);

  async function loadStats() {
    try {
      const { data } = await monitoringAPI.getTokenUsage();
      setTokenStats(data);
    } catch (err) {
      console.error("加载统计失败", err);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="h-full overflow-y-auto p-4 space-y-4">
      <h2 className="text-sm font-semibold">运行监控</h2>

      {/* 实时 Token 消耗 */}
      <section className="border rounded-lg p-4 bg-card">
        <h3 className="text-xs font-medium text-muted-foreground mb-3">实时 Token 消耗</h3>
        <div className="grid grid-cols-3 gap-3 mb-4">
          <div className="p-3 bg-blue-50 dark:bg-blue-950/20 rounded-lg">
            <p className="text-[10px] text-blue-600 font-medium">输入 Tokens</p>
            <p className="text-lg font-bold tabular-nums">
              {((cumulativeTokens?.prompt_tokens ?? 0) / 1000).toFixed(1)}K
            </p>
          </div>
          <div className="p-3 bg-green-50 dark:bg-green-950/20 rounded-lg">
            <p className="text-[10px] text-green-600 font-medium">输出 Tokens</p>
            <p className="text-lg font-bold tabular-nums">
              {((cumulativeTokens?.completion_tokens ?? 0) / 1000).toFixed(1)}K
            </p>
          </div>
          <div className="p-3 bg-amber-50 dark:bg-amber-950/20 rounded-lg">
            <p className="text-[10px] text-amber-600 font-medium">费用</p>
            <p className="text-lg font-bold tabular-nums">
              ${(cumulativeTokens?.cost_usd ?? 0).toFixed(4)}
            </p>
          </div>
        </div>

        {/* 图表 */}
        <TokenChart tokenStats={tokenStats} currentUsage={cumulativeTokens} />
      </section>

      {/* 错误监控 */}
      <section className="border rounded-lg p-4 bg-card">
        <h3 className="text-xs font-medium text-muted-foreground mb-3">错误日志</h3>
        <ErrorPanel threadId={threadId} />
      </section>

      {/* 历史统计 */}
      {loading ? (
        <div className="text-center py-4 text-xs text-muted-foreground">加载历史数据...</div>
      ) : tokenStats ? (
        <section className="border rounded-lg p-4 bg-card">
          <h3 className="text-xs font-medium text-muted-foreground mb-3">历史统计</h3>
          <div className="space-y-2 text-xs">
            <div className="flex justify-between">
              <span className="text-muted-foreground">总 Tokens</span>
              <span className="font-medium">{((tokenStats?.total_tokens ?? 0) / 1000).toFixed(1)}K</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">总费用</span>
              <span className="font-medium">${(tokenStats?.total_cost_usd ?? 0).toFixed(4)}</span>
            </div>
            {tokenStats?.by_model && Object.entries(tokenStats.by_model).map(([model, usage]) => (
              <div key={model} className="flex justify-between">
                <span className="text-muted-foreground">{model}</span>
                <span className="font-medium">{((usage?.total_tokens ?? 0) / 1000).toFixed(1)}K</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
