"use client";

import { TokenUsage } from "@/lib/types";
import { formatTokens, formatCost } from "@/lib/utils";

interface TokenUsageBarProps {
  tokens: TokenUsage;
}

export default function TokenUsageBar({ tokens }: TokenUsageBarProps) {
  const inputPct = tokens.total_tokens > 0
    ? (tokens.prompt_tokens / tokens.total_tokens) * 100
    : 0;

  return (
    <div className="mt-2 pt-2 border-t">
      <div className="flex items-center justify-between text-[10px] text-muted-foreground mb-1">
        <span>Token 消耗</span>
        <span>{formatCost(tokens.cost_usd)}</span>
      </div>
      <div className="h-1.5 rounded-full bg-muted overflow-hidden flex">
        <div
          className="h-full bg-blue-400 transition-all"
          style={{ width: `${inputPct}%` }}
          title={`输入: ${formatTokens(tokens.prompt_tokens)}`}
        />
        <div
          className="h-full bg-green-400 transition-all"
          style={{ width: `${100 - inputPct}%` }}
          title={`输出: ${formatTokens(tokens.completion_tokens)}`}
        />
      </div>
      <div className="flex justify-between text-[9px] text-muted-foreground mt-0.5">
        <span>输入: {formatTokens(tokens.prompt_tokens)}</span>
        <span>输出: {formatTokens(tokens.completion_tokens)}</span>
      </div>
    </div>
  );
}
