"use client";

import { useMemo } from "react";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import { TokenUsage, TokenUsageStats } from "@/lib/types";
import { formatTokens, formatCost } from "@/lib/utils";

interface TokenChartProps {
  tokenStats: TokenUsageStats | null;
  currentUsage: TokenUsage;
}

export default function TokenChart({ tokenStats, currentUsage }: TokenChartProps) {
  // 从历史数据生成图表数据点
  const chartData = useMemo(() => {
    if (!tokenStats?.by_date) {
      return [
        { name: "当前", prompt: currentUsage.prompt_tokens, completion: currentUsage.completion_tokens },
      ];
    }
    return tokenStats.by_date.map((d) => ({
      name: d.date,
      prompt: currentUsage.prompt_tokens || 0,
      completion: currentUsage.completion_tokens || 0,
    }));
  }, [tokenStats, currentUsage]);

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis dataKey="name" tick={{ fontSize: 10 }} />
          <YAxis tick={{ fontSize: 10 }} tickFormatter={(v: number) => formatTokens(v)} />
          <Tooltip
            contentStyle={{ fontSize: 11, borderRadius: 8 }}
            formatter={(value: number) => [formatTokens(value), ""]}
          />
          <Legend wrapperStyle={{ fontSize: 10 }} />
          <Area
            type="monotone"
            dataKey="prompt"
            stackId="1"
            stroke="#3b82f6"
            fill="#3b82f6"
            fillOpacity={0.3}
            name="输入 Tokens"
          />
          <Area
            type="monotone"
            dataKey="completion"
            stackId="1"
            stroke="#10b981"
            fill="#10b981"
            fillOpacity={0.3}
            name="输出 Tokens"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
