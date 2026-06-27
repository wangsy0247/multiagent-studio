import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDateTime(isoString: string | null | undefined): string {
  if (!isoString) return "";
  // 数据库存储的是 UTC 时间但无时区标记 → 追加 'Z' 明确标识
  const normalized = isoString.endsWith("Z") || isoString.includes("+") ? isoString : isoString + "Z";
  const date = new Date(normalized);
  if (isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export function formatCost(usd: number): string {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toExponential(2)}`;
}

export function formatDuration(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)}m`;
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)}s`;
  return `${ms}ms`;
}

export function generateId(): string {
  // crypto.randomUUID() requires secure context (HTTPS) — not always available in dev.
  // Fallback: crypto.getRandomValues() works everywhere, including HTTP localhost.
  try {
    return crypto.randomUUID();
  } catch {
    // RFC 4122 v4 UUID via crypto.getRandomValues()
    return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
      (parseInt(c) ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (parseInt(c) / 4)))).toString(16)
    );
  }
}
