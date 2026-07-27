"use client";

import { useState, useEffect } from "react";
import { Zap, ArrowDown, ArrowUp, DollarSign, Activity } from "lucide-react";
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
    <div className="h-full overflow-y-auto p-5 space-y-5 bg-slate-50/50">
      <div className="flex items-center gap-2 mb-1">
        <Activity className="w-4 h-4 text-slate-500" />
        <h2 className="text-sm font-semibold text-slate-800">运行监控</h2>
      </div>

      {/* Real-time Token Stats */}
      <section className="bg-white border border-slate-200 rounded-xl p-4">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-4">实时 Token 消耗</h3>
        <div className="grid grid-cols-3 gap-3 mb-4">
          <div className="p-3.5 bg-hermes-50 border border-hermes-100 rounded-xl">
            <div className="flex items-center gap-1.5 mb-1.5">
              <ArrowDown className="w-3.5 h-3.5 text-hermes-500" />
              <p className="text-[10px] text-hermes-600 font-semibold">输入 Tokens</p>
            </div>
            <p className="text-xl font-bold text-hermes-700 tabular-nums">
              {((cumulativeTokens?.prompt_tokens ?? 0) / 1000).toFixed(1)}K
            </p>
          </div>
          <div className="p-3.5 bg-emerald-50 border border-emerald-100 rounded-xl">
            <div className="flex items-center gap-1.5 mb-1.5">
              <ArrowUp className="w-3.5 h-3.5 text-emerald-500" />
              <p className="text-[10px] text-emerald-600 font-semibold">输出 Tokens</p>
            </div>
            <p className="text-xl font-bold text-emerald-700 tabular-nums">
              {((cumulativeTokens?.completion_tokens ?? 0) / 1000).toFixed(1)}K
            </p>
          </div>
          <div className="p-3.5 bg-amber-50 border border-amber-100 rounded-xl">
            <div className="flex items-center gap-1.5 mb-1.5">
              <DollarSign className="w-3.5 h-3.5 text-amber-500" />
              <p className="text-[10px] text-amber-600 font-semibold">费用</p>
            </div>
            <p className="text-xl font-bold text-amber-700 tabular-nums">
              ${(cumulativeTokens?.cost_usd ?? 0).toFixed(4)}
            </p>
          </div>
        </div>
        <TokenChart tokenStats={tokenStats} currentUsage={cumulativeTokens} />
      </section>

      {/* Error Log */}
      <section className="bg-white border border-slate-200 rounded-xl p-4">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-4">错误日志</h3>
        <ErrorPanel threadId={threadId} />
      </section>

      {/* History Stats */}
      {loading ? (
        <div className="text-center py-6 text-xs text-slate-400">
          <Zap className="w-4 h-4 mx-auto mb-2 animate-pulse" />
          加载历史数据...
        </div>
      ) : tokenStats ? (
        <section className="bg-white border border-slate-200 rounded-xl p-4">
          <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-4">历史统计</h3>
          <div className="space-y-3 text-sm">
            <div className="flex justify-between py-2 border-b border-slate-100">
              <span className="text-slate-500">总 Tokens</span>
              <span className="font-semibold text-slate-800">{((tokenStats?.total_tokens ?? 0) / 1000).toFixed(1)}K</span>
            </div>
            <div className="flex justify-between py-2 border-b border-slate-100">
              <span className="text-slate-500">总费用</span>
              <span className="font-semibold text-slate-800">${(tokenStats?.total_cost_usd ?? 0).toFixed(4)}</span>
            </div>
            {tokenStats?.by_model && Object.entries(tokenStats.by_model).map(([model, usage]) => (
              <div key={model} className="flex justify-between py-2 border-b border-slate-100 last:border-0">
                <span className="text-slate-500">{model}</span>
                <span className="font-medium text-slate-700">{((usage?.total_tokens ?? 0) / 1000).toFixed(1)}K</span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}
