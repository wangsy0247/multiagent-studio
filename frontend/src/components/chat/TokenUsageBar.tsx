"use client";

import React from "react";
import { TokenUsage } from "@/lib/types";
import { formatTokens, formatCost } from "@/lib/utils";

interface TokenUsageBarProps {
  tokens: TokenUsage;
}

const TokenUsageBar = React.memo(function TokenUsageBar({ tokens }: TokenUsageBarProps) {
  if (!tokens || tokens.total_tokens === 0) return null;

  const inputPct = tokens.total_tokens > 0
    ? (tokens.prompt_tokens / tokens.total_tokens) * 100
    : 0;

  return (
    <div className="mt-2.5 pt-2.5 border-t border-slate-200/60">
      <div className="flex items-center justify-between text-[10px] text-slate-500 mb-1.5">
        <span>Token 消耗</span>
        <span className="font-medium">{formatCost(tokens.cost_usd)}</span>
      </div>
      <div className="h-1.5 rounded-full bg-slate-200 overflow-hidden flex">
        <div
          className="h-full bg-blue-400 transition-all duration-300 rounded-full"
          style={{ width: `${Math.max(inputPct, 4)}%` }}
          title={`输入: ${formatTokens(tokens.prompt_tokens)}`}
        />
        <div
          className="h-full bg-emerald-400 transition-all duration-300 rounded-full"
          style={{ width: `${Math.max(100 - inputPct, 4)}%` }}
          title={`输出: ${formatTokens(tokens.completion_tokens)}`}
        />
      </div>
      <div className="flex justify-between text-[9px] text-slate-400 mt-1">
        <span>输入: {formatTokens(tokens.prompt_tokens)}</span>
        <span>输出: {formatTokens(tokens.completion_tokens)}</span>
      </div>
    </div>
  );
});

export default TokenUsageBar;
