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

/** 完整时间格式 "2026-01-02 09:30:00"（执行历史等精确场景用） */
export function formatDateTimeFull(isoString: string | null | undefined): string {
  if (!isoString) return "";
  const normalized = isoString.endsWith("Z") || isoString.includes("+") ? isoString : isoString + "Z";
  const date = new Date(normalized);
  if (isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 相对时间：过去 → "5 分钟前"，未来 → "3 小时后"（下次执行倒计时用） */
export function formatRelativeTime(isoString: string | null | undefined): string {
  if (!isoString) return "";
  const normalized = isoString.endsWith("Z") || isoString.includes("+") ? isoString : isoString + "Z";
  const date = new Date(normalized);
  if (isNaN(date.getTime())) return "";
  const diffMs = date.getTime() - Date.now();
  const future = diffMs > 0;
  const absSec = Math.abs(diffMs) / 1000;

  let text: string;
  if (absSec < 60) return future ? "即将" : "刚刚";
  if (absSec < 3600) text = `${Math.floor(absSec / 60)} 分钟`;
  else if (absSec < 86400) text = `${Math.floor(absSec / 3600)} 小时`;
  else text = `${Math.floor(absSec / 86400)} 天`;
  return future ? `${text}后` : `${text}前`;
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
