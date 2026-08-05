"use client";

/**
 * Mermaid 图表渲染 (Phase 7) — lead_agent prompt 鼓励模型输出 ```mermaid 代码块,
 * 这里将其渲染为 SVG 图表。
 * - mermaid 体积大且依赖 DOM, 仅在客户端 useEffect 内 dynamic import, 不进主 chunk
 * - 仅用于完整渲染分支; 流式期间 (isLiveStreaming) 由 streamingCode 降级为纯文本,
 *   避免语法未闭合时频繁渲染报错
 * - 渲染失败时降级显示源码, 不破坏消息展示
 */
import React from "react";

// mermaid.render 要求全局唯一 id, 用递增计数器保证 (同一组件重渲染也要换 id)
let mermaidSeq = 0;

interface MermaidBlockProps {
  chart: string;
}

export default function MermaidBlock({ chart }: MermaidBlockProps) {
  const [svg, setSvg] = React.useState<string | null>(null);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
    let cancelled = false;
    setSvg(null);
    setFailed(false);
    const renderId = `mermaid-${Date.now()}-${++mermaidSeq}`;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: "neutral",
          securityLevel: "strict",
        });
        const { svg } = await mermaid.render(renderId, chart);
        if (!cancelled) setSvg(svg);
      } catch {
        // 语法错误等 → 降级为源码展示
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [chart]);

  if (failed) {
    return (
      <div className="my-3 overflow-hidden rounded-lg border border-slate-200">
        <div className="flex items-center justify-between px-4 py-1.5 bg-slate-100 border-b border-slate-200">
          <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wider">mermaid</span>
          <span className="text-[10px] text-slate-400">图表渲染失败, 显示源码</span>
        </div>
        <pre className="m-0 p-4 overflow-x-auto text-[13px] leading-relaxed bg-[#282c34] text-slate-100">
          <code>{chart}</code>
        </pre>
      </div>
    );
  }

  if (!svg) {
    return (
      <div className="my-3 flex items-center justify-center rounded-lg border border-slate-200 bg-slate-50 px-4 py-8">
        <span className="text-xs text-slate-400">图表渲染中...</span>
      </div>
    );
  }

  return (
    <div
      className="my-3 overflow-x-auto rounded-lg border border-slate-200 bg-white p-4 [&_svg]:max-w-full"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
