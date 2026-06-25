"use client";

import { useState, useEffect } from "react";
import { monitoringAPI } from "@/lib/api-client";
import { TokenUsageStats } from "@/lib/types";
import { formatTokens, formatCost } from "@/lib/utils";

export default function AdminPage() {
  const [stats, setStats] = useState<TokenUsageStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadStats();
  }, []);

  async function loadStats() {
    try {
      const { data } = await monitoringAPI.getTokenUsage();
      setStats(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="max-w-4xl mx-auto p-6">
      <h2 className="text-lg font-semibold mb-6">管理面板</h2>

      {loading ? (
        <div className="text-center py-12 text-muted-foreground">加载中...</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Token 统计 */}
          <div className="border rounded-lg p-4 bg-card">
            <h3 className="text-sm font-medium mb-3">全局 Token 使用</h3>
            {stats ? (
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">总 Tokens</span>
                  <span className="font-bold">{formatTokens(stats.total_tokens)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">输入 Tokens</span>
                  <span>{formatTokens(stats.total_prompt_tokens)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">输出 Tokens</span>
                  <span>{formatTokens(stats.total_completion_tokens)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">总费用</span>
                  <span className="font-bold text-primary">{formatCost(stats.total_cost_usd)}</span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">暂无数据</p>
            )}
          </div>

          {/* 模型分布 */}
          <div className="border rounded-lg p-4 bg-card">
            <h3 className="text-sm font-medium mb-3">模型分布</h3>
            {stats?.by_model ? (
              <div className="space-y-2">
                {Object.entries(stats.by_model).map(([model, usage]) => (
                  <div key={model} className="flex items-center gap-2 text-sm">
                    <span className="flex-1 text-muted-foreground">{model}</span>
                    <span className="font-medium">{formatTokens(usage.total_tokens)}</span>
                    <span className="text-muted-foreground">{formatCost(usage.cost_usd)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">暂无数据</p>
            )}
          </div>

          {/* 系统状态 */}
          <div className="border rounded-lg p-4 bg-card">
            <h3 className="text-sm font-medium mb-3">系统状态</h3>
            <div className="space-y-2 text-sm">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 bg-green-500 rounded-full" />
                <span>App 服务: 运行中</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 bg-green-500 rounded-full" />
                <span>Harness 服务: 运行中</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 bg-green-500 rounded-full" />
                <span>PostgreSQL: 运行中</span>
              </div>
            </div>
          </div>

          {/* 快速操作 */}
          <div className="border rounded-lg p-4 bg-card">
            <h3 className="text-sm font-medium mb-3">快速操作</h3>
            <div className="space-y-2">
              <button
                onClick={loadStats}
                className="w-full py-2 text-sm border rounded-lg hover:bg-accent transition"
              >
                刷新数据
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
