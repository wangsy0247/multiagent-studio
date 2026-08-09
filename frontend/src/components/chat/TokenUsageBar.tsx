"use client";

import React from "react";
import { TokenUsage } from "@/lib/types";
import { formatTokens } from "@/lib/utils";

interface TokenUsageBarProps {
  tokens: TokenUsage;
}

const TokenUsageBar = React.memo(function TokenUsageBar({ tokens }: TokenUsageBarProps) {
  if (!tokens || tokens.total_tokens === 0) return null;

  const cacheHit = tokens.cache_hit_tokens || 0;

  return (
    <div className="mt-2 pt-1.5 border-t border-slate-200/60 text-[10px] text-slate-400 tabular-nums">
      输入 {formatTokens(tokens.prompt_tokens)} · 输出 {formatTokens(tokens.completion_tokens)}
      {cacheHit > 0 && <> · 缓存命中 {formatTokens(cacheHit)}</>}
    </div>
  );
});

export default TokenUsageBar;
