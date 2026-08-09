"use client";

import { useState, useEffect } from "react";
import { ArrowDown, ArrowUp, Zap, ZapOff } from "lucide-react";
import { monitoringAPI } from "@/lib/api-client";
import { TokenUsageStats } from "@/lib/types";
import { formatTokens, cn } from "@/lib/utils";
import TokenChart from "./TokenChart";
import { useChatStore } from "@/lib/chat-store";

/** 用量来源 source 枚举 → 中文标签 */
const SOURCE_LABELS: Record<string, string> = {
  main: "主对话",
  subagent: "子Agent",
  team_member: "团队成员",
  team_synthesis: "团队汇总",
  title: "标题",
  summary: "摘要",
  memory: "记忆",
};

/** 单张统计卡 */
function StatCard({
  icon: Icon,
  label,
  value,
  hint,
  tone,
}: {
  icon: typeof ArrowDown;
  label: string;
  value: string;
  hint?: string;
  tone: "hermes" | "emerald" | "amber" | "sky";
}) {
  const tones = {
    hermes: "bg-hermes-50 border-hermes-100 text-hermes-600 [&_.num]:text-hermes-700 [&_.ic]:text-hermes-500",
    emerald: "bg-emerald-50 border-emerald-100 text-emerald-600 [&_.num]:text-emerald-700 [&_.ic]:text-emerald-500",
    amber: "bg-amber-50 border-amber-100 text-amber-600 [&_.num]:text-amber-700 [&_.ic]:text-amber-500",
    sky: "bg-sky-50 border-sky-100 text-sky-600 [&_.num]:text-sky-700 [&_.ic]:text-sky-500",
  } as const;
  return (
    <div className={cn("p-3.5 border rounded-xl", tones[tone])}>
      <div className="flex items-center gap-1.5 mb-1.5">
        <Icon className="ic w-3.5 h-3.5" />
        <p className="text-[10px] font-semibold">{label}</p>
      </div>
      <p className="num text-xl font-bold tabular-nums">{value}</p>
      {hint && <p className="text-[10px] opacity-70 mt-0.5 tabular-nums">{hint}</p>}
    </div>
  );
}

/** 横向条形分布列表 (来源 / 模型) */
function DistributionList({ items }: { items: Array<{ label: string; tokens: number }> }) {
  if (items.length === 0) {
    return <p className="text-xs text-slate-400">暂无数据</p>;
  }
  const max = Math.max(...items.map((i) => i.tokens), 1);
  return (
    <div className="space-y-2">
      {items.map((item) => (
        <div key={item.label} className="flex items-center gap-2 text-xs">
          <span className="w-16 flex-shrink-0 text-slate-500 truncate" title={item.label}>
            {item.label}
          </span>
          <div className="flex-1 h-2 rounded-full bg-slate-100 overflow-hidden">
            <div
              className="h-full rounded-full bg-hermes-400 transition-all duration-300"
              style={{ width: `${Math.max((item.tokens / max) * 100, 2)}%` }}
            />
          </div>
          <span className="w-14 flex-shrink-0 text-right font-medium text-slate-700 tabular-nums">
            {formatTokens(item.tokens)}
          </span>
        </div>
      ))}
    </div>
  );
}

interface UsageStatsViewProps {
  /** thread = 当前会话 (需传 threadId); all = 全部会话 */
  scope: "thread" | "all";
  threadId?: string;
}

/**
 * Token 用量统计视图 (统计卡 + 分布 + 趋势) — 会话监控面板与
 * 侧边栏「用量监控」页共用, 数据源 GET /monitoring/token-usage。
 */
export default function UsageStatsView({ scope, threadId }: UsageStatsViewProps) {
  const [stats, setStats] = useState<TokenUsageStats | null>(null);
  const [loading, setLoading] = useState(true);
  const { cumulativeTokens } = useChatStore();
  // SSE token_usage 到达 → 累计值变化 → 触发一次重新拉取
  const cumulativeTotal = cumulativeTokens.total_tokens;

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const { data } = await monitoringAPI.getTokenUsage(
          scope === "thread" && threadId ? { thread_id: threadId } : undefined,
        );
        if (!cancelled) setStats(data);
      } catch (err) {
        console.error("加载统计失败", err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    // 面板可见时每 10s 轮询 (覆盖 memory 等异步旁路延迟落账)
    const timer = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [scope, threadId, cumulativeTotal]);

  const prompt = stats?.prompt_tokens ?? 0;
  const cacheHit = stats?.cache_hit_tokens ?? 0;
  const cacheMiss = stats?.cache_miss_tokens ?? 0;
  const hitRate = prompt > 0 ? (cacheHit / prompt) * 100 : 0;

  const sourceItems = (stats?.by_source || [])
    .map((s) => ({ label: SOURCE_LABELS[s.source] || s.source, tokens: s.total_tokens }))
    .sort((a, b) => b.tokens - a.tokens);
  const modelItems = Object.entries(stats?.by_model || {})
    .map(([model, usage]) => ({ label: model, tokens: usage?.total_tokens ?? 0 }))
    .sort((a, b) => b.tokens - a.tokens);

  return (
    <div className="space-y-5">
      {/* Token 统计卡 */}
      <section className="bg-white border border-slate-200 rounded-xl p-4">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-4">
          {scope === "thread" ? "当前会话 Token 统计" : "全部会话 Token 统计"}
        </h3>
        {loading && !stats ? (
          <div className="text-center py-6 text-xs text-slate-400">加载中...</div>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            <StatCard icon={ArrowDown} label="输入 Tokens" value={formatTokens(prompt)} tone="hermes" />
            <StatCard
              icon={ArrowUp}
              label="输出 Tokens"
              value={formatTokens(stats?.completion_tokens ?? 0)}
              tone="emerald"
            />
            <StatCard
              icon={Zap}
              label="缓存命中"
              value={formatTokens(cacheHit)}
              hint={prompt > 0 ? `命中率 ${hitRate.toFixed(1)}%` : undefined}
              tone="amber"
            />
            <StatCard icon={ZapOff} label="缓存未命中" value={formatTokens(cacheMiss)} tone="sky" />
          </div>
        )}
      </section>

      {/* 分布 */}
      <section className="bg-white border border-slate-200 rounded-xl p-4">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-4">
          {scope === "thread" ? "来源分布" : "模型分布"}
        </h3>
        <DistributionList items={scope === "thread" ? sourceItems : modelItems} />
      </section>

      {/* 趋势 */}
      <section className="bg-white border border-slate-200 rounded-xl p-4">
        <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-4">Token 趋势</h3>
        <TokenChart tokenStats={stats} currentUsage={cumulativeTokens} />
      </section>
    </div>
  );
}
