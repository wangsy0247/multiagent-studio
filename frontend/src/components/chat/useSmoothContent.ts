"use client";

import { useEffect, useRef, useState } from "react";

// 对齐 DeerFlow useSmoothStreamingContent (markdown-content.tsx):
// 流式中大块增量不一次性砸进 Markdown, 而是在 ~300ms 内按每帧 >= 8 字符渐进揭示
const SMOOTH_REVEAL_MIN_DELTA = 80;
const SMOOTH_REVEAL_MIN_CHARS_PER_FRAME = 8;
const SMOOTH_REVEAL_DURATION_MS = 300;

/**
 * 平滑打字机: 数据层节流 (chat-store 50ms flush) 之上再做展示层平滑。
 * - isStreaming 且 target 以当前显示值为前缀且 delta >= 80 字符 → rAF 渐进揭示;
 * - delta 较小 / 非前缀关系 (新消息、重置) / 非流式 → 直接跳变到 target。
 */
export function useSmoothContent(target: string, isStreaming: boolean): string {
  const [display, setDisplay] = useState(target);
  const displayRef = useRef(target);
  const targetRef = useRef(target);

  useEffect(() => {
    targetRef.current = target;

    const current = displayRef.current;
    const delta = target.length - current.length;
    const shouldSmoothReveal =
      isStreaming && delta >= SMOOTH_REVEAL_MIN_DELTA && target.startsWith(current);

    if (!shouldSmoothReveal) {
      if (current !== target) {
        displayRef.current = target;
        setDisplay(target);
      }
      return;
    }

    let cancelled = false;
    let frame: number | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let generation = 0;
    const startedAt = performance.now();
    const startLength = current.length;

    const tick = (now: number, scheduledGeneration: number) => {
      if (cancelled || scheduledGeneration !== generation) return;
      generation += 1;
      if (frame !== null) {
        cancelAnimationFrame(frame);
        frame = null;
      }
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }

      const latestTarget = targetRef.current;
      const latest = displayRef.current;
      if (!latestTarget.startsWith(latest) || latest.length >= latestTarget.length) return;

      // 取 max(时间进度, 每帧下限): 300ms 窗口内揭示完毕, 同时追上持续增长的目标
      const totalDelta = latestTarget.length - startLength;
      const progress = Math.min(1, (now - startedAt) / SMOOTH_REVEAL_DURATION_MS);
      const elapsedLength = startLength + Math.ceil(totalDelta * progress);
      const nextLength = Math.max(
        latest.length + SMOOTH_REVEAL_MIN_CHARS_PER_FRAME,
        elapsedLength,
      );
      const next = latestTarget.slice(0, nextLength);
      displayRef.current = next;
      setDisplay(next);

      if (next.length < latestTarget.length) scheduleTick();
    };

    const scheduleTick = () => {
      const scheduledGeneration = ++generation;
      frame = requestAnimationFrame((now) => tick(now, scheduledGeneration));
      // rAF 在后台标签页会暂停 — setTimeout 兜底保证内容最终揭示
      timer = setTimeout(() => tick(performance.now(), scheduledGeneration), 50);
    };

    scheduleTick();
    return () => {
      cancelled = true;
      generation += 1;
      if (frame !== null) cancelAnimationFrame(frame);
      if (timer !== null) clearTimeout(timer);
    };
  }, [target, isStreaming]);

  return display;
}
