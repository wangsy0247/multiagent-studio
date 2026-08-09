"use client";

import React from "react";
import { Bot, User, Wrench, Network, AlertTriangle, Brain, ChevronDown, ChevronRight, Copy, Check, FileText } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
// 语法高亮配置已抽取到共享模块 (Artifact 预览面板复用)
import { SyntaxHighlighter, oneDark } from "./syntax-highlighter";
import MermaidBlock from "./MermaidBlock";
import { ChatMessage, TokenUsage } from "@/lib/types";
import { cn, formatDateTime, formatFileSize } from "@/lib/utils";
import { useChatStore } from "@/lib/chat-store";
import { resolveOutputsUrl, downloadWithAuth, fetchFileObjectUrl } from "@/lib/api-client";
import ToolCallCard from "./ToolCallCard";
import ArtifactFileList, { parsePresentedFilepaths } from "./ArtifactFileList";
import SubAgentCard from "./SubAgentCard";
import TokenUsageBar from "./TokenUsageBar";
import { useSmoothContent } from "./useSmoothContent";

interface MessageItemProps {
  message: ChatMessage;
  /** 是否为正在流式生长的最后一条 AI 消息 (平滑打字机 + 代码块降级渲染) */
  isLiveStreaming?: boolean;
}

const roleConfig = {
  human: { icon: User, bg: "bg-hermes-500/10", iconColor: "text-hermes-600", label: "你" },
  ai: { icon: Bot, bg: "bg-slate-800/10", iconColor: "text-slate-700", label: "AI" },
  tool: { icon: Wrench, bg: "bg-amber-500/10", iconColor: "text-amber-500", label: "工具" },
  subagent: { icon: Network, bg: "bg-emerald-500/10", iconColor: "text-emerald-500", label: "SubAgent" },
  system: { icon: AlertTriangle, bg: "bg-destructive/10", iconColor: "text-destructive", label: "系统" },
};

// ── Phase 7 辅助: 代码块复制按钮 ──────────────────────────────────
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = React.useState(false);
  const timerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 非安全上下文 (http/局域网 IP) clipboard API 不可用时的降级
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), 1500);
  };

  return (
    <button
      type="button"
      onClick={onClick}
      title={copied ? "已复制" : "复制代码"}
      className="p-1 rounded text-slate-400 hover:text-slate-600 hover:bg-slate-200 transition-colors"
    >
      {copied ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
    </button>
  );
}

// ── Phase 7 辅助: 链接协议白名单 ─────────────────────────────────
// 只放行 http/https/mailto/tel 与相对路径; javascript:/data: 等渲染为禁用纯文本
const ALLOWED_PROTOCOLS = new Set(["http:", "https:", "mailto:", "tel:"]);
function isAllowedHref(href: string): boolean {
  const trimmed = href.trim();
  // 相对路径 / 锚点 / 虚拟产物路径
  if (trimmed.startsWith("/") || trimmed.startsWith("#") || trimmed.startsWith("./") || trimmed.startsWith("../")) {
    return true;
  }
  const schemeMatch = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(trimmed);
  if (!schemeMatch) return true; // 无 scheme 视为相对路径
  return ALLOWED_PROTOCOLS.has(schemeMatch[1].toLowerCase() + ":");
}

// react-markdown children 可能是嵌套节点, 提取纯文本用于 citation 识别
function flattenMarkdownText(node: any): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(flattenMarkdownText).join("");
  if (typeof node === "object" && node.props?.children !== undefined) {
    return flattenMarkdownText(node.props.children);
  }
  return "";
}

// ── Phase 7 辅助: citation 来源聚合 ───────────────────────────────
// 从消息正文正则提取 [citation:标题](url), 按 url 去重, 渲染在消息底部
interface CitationSource {
  title: string;
  url: string;
}
const CITATION_RE = /\[citation:([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
function extractCitationSources(content: string): CitationSource[] {
  const seen = new Set<string>();
  const sources: CitationSource[] = [];
  for (const match of content.matchAll(CITATION_RE)) {
    const url = match[2];
    if (seen.has(url)) continue;
    seen.add(url);
    sources.push({ title: match[1], url });
  }
  return sources;
}

function CitationSources({ content }: { content: string }) {
  const sources = React.useMemo(() => extractCitationSources(content), [content]);
  if (sources.length === 0) return null;
  return (
    <div className="mt-3 pt-2 border-t border-slate-100">
      <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-1.5">来源</div>
      <ol className="space-y-1 list-none pl-0">
        {sources.map((s, i) => (
          <li key={s.url} className="text-xs">
            <a
              href={s.url}
              target="_blank"
              rel="noopener noreferrer"
              title={s.url}
              className="text-hermes-600 hover:text-hermes-700 hover:underline underline-offset-2 break-all"
            >
              [{i + 1}] {s.title}
            </a>
          </li>
        ))}
      </ol>
    </div>
  );
}

// Custom renderers for every Markdown element — ensures pixel-perfect alignment
// (export 供 ArtifactPreviewPanel 的 markdown 预览复用)

/** outputs 产物图片: 带鉴权拉 blob 转 objectURL (裸 src 不带 JWT 会 401) */
function OutputsImage({ src, alt }: { src: string; alt?: string }) {
  const [objUrl, setObjUrl] = React.useState<string | null>(null);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
    let cancelled = false;
    let created: string | null = null;
    fetchFileObjectUrl(src)
      .then((u) => {
        if (cancelled) {
          URL.revokeObjectURL(u);
          return;
        }
        created = u;
        setObjUrl(u);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [src]);

  if (failed) {
    return (
      <span className="inline-block my-2 px-3 py-2 rounded-lg bg-slate-100 text-xs text-slate-400">
        图片加载失败{alt ? `: ${alt}` : ""}
      </span>
    );
  }
  if (!objUrl) {
    return <span className="inline-block my-2 h-16 w-full max-w-sm rounded-lg bg-slate-100 animate-pulse" />;
  }
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={objUrl} alt={alt} className="my-2 rounded-lg max-w-full" />;
}

export const markdownComponents: Record<string, React.FC<any>> = {
  // Code blocks with syntax highlighting
  code({ className, children, ...props }: any) {
    const match = /language-(\w+)/.exec(className || "");
    const codeString = String(children).replace(/\n$/, "");
    if (match) {
      // Phase 7: mermaid 代码块渲染为图表 (仅完整渲染分支; 流式走 streamingCode 降级)
      if (match[1] === "mermaid") {
        return <MermaidBlock chart={codeString} />;
      }
      return (
        <div className="my-3 -mx-0 overflow-hidden rounded-lg border border-slate-200">
          <div className="flex items-center justify-between px-4 py-1.5 bg-slate-100 border-b border-slate-200">
            <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wider">{match[1]}</span>
            <CopyButton text={codeString} />
          </div>
          <SyntaxHighlighter
            style={oneDark}
            language={match[1]}
            PreTag="div"
            customStyle={{ margin: 0, borderRadius: 0, fontSize: "13px", padding: "16px" }}
          >
            {codeString}
          </SyntaxHighlighter>
        </div>
      );
    }
    return (
      <code className="px-1.5 py-0.5 bg-slate-100 text-slate-800 rounded text-[0.88em] font-mono" {...props}>
        {children}
      </code>
    );
  },

  // Tables — full-width, striped, bordered
  table({ children }: any) {
    return (
      <div className="my-3 overflow-x-auto rounded-lg border border-slate-200">
        <table className="min-w-full text-left text-sm">{children}</table>
      </div>
    );
  },
  thead({ children }: any) {
    return <thead className="bg-slate-50 border-b border-slate-200">{children}</thead>;
  },
  th({ children }: any) {
    return <th className="px-3 py-2 text-sm font-semibold text-slate-600 border-r border-slate-100 last:border-r-0">{children}</th>;
  },
  td({ children }: any) {
    return <td className="px-3 py-2 text-sm text-slate-700 border-r border-slate-50 last:border-r-0 border-b border-slate-50">{children}</td>;
  },
  tr({ children }: any) {
    return <tr className="even:bg-slate-50/50">{children}</tr>;
  },

  // Blockquote
  blockquote({ children }: any) {
    return (
      <blockquote className="my-3 pl-4 border-l-[3px] border-hermes-400 text-slate-600 italic">
        {children}
      </blockquote>
    );
  },

  // Headings
  h1({ children }: any) {
    return <h1 className="text-lg font-bold font-display text-slate-900 mt-4 mb-2 first:mt-0">{children}</h1>;
  },
  h2({ children }: any) {
    return <h2 className="text-base font-bold font-display text-slate-900 mt-3 mb-1.5 first:mt-0 pb-1 border-b border-slate-100">{children}</h2>;
  },
  h3({ children }: any) {
    return <h3 className="text-sm font-semibold font-display text-slate-800 mt-3 mb-1 first:mt-0">{children}</h3>;
  },
  h4({ children }: any) {
    return <h4 className="text-sm font-medium text-slate-700 mt-2 mb-1 first:mt-0">{children}</h4>;
  },

  // Lists
  ul({ children }: any) {
    return <ul className="my-2 pl-5 space-y-0.5 list-disc text-slate-700">{children}</ul>;
  },
  ol({ children }: any) {
    return <ol className="my-2 pl-5 space-y-0.5 list-decimal text-slate-700">{children}</ol>;
  },
  li({ children }: any) {
    return <li className="text-base leading-relaxed pl-1">{children}</li>;
  },

  // Paragraph
  p({ children }: any) {
    return <p className="text-base text-slate-700 leading-relaxed my-1.5 first:mt-0 last:mb-0">{children}</p>;
  },

  // Links — /mnt/user-data/outputs/ 虚拟路径映射为下载端点 URL (产物出口闭环)
  // Phase 7: [citation:标题](url) 渲染为徽章; 协议白名单外链接禁用
  a({ children, href }: any) {
    const rawHref = typeof href === "string" ? href : "";
    const text = flattenMarkdownText(children);

    // citation 徽章 (小胶囊 + 上标感, 区别于普通链接; title 悬浮显示 URL)
    if (/^https?:\/\//i.test(rawHref) && text.startsWith("citation:")) {
      return (
        <a
          href={rawHref}
          target="_blank"
          rel="noopener noreferrer"
          title={rawHref}
          className="inline-flex items-center mx-0.5 px-1.5 py-px rounded-full border border-hermes-300 bg-hermes-50 text-hermes-700 text-[11px] leading-4 no-underline align-super hover:bg-hermes-100 transition-colors"
        >
          {text.slice("citation:".length)}
        </a>
      );
    }

    // 协议白名单: javascript:/data: 等渲染为禁用纯文本, 不生成可点链接
    if (rawHref && !isAllowedHref(rawHref)) {
      return (
        <span className="text-slate-400 line-through cursor-not-allowed" title={`已拦截不安全链接: ${rawHref.slice(0, 40)}`}>
          {children}
        </span>
      );
    }

    const mapped = rawHref
      ? resolveOutputsUrl(useChatStore.getState().activeThreadId, rawHref)
      : null;
    // outputs 产物链接: 带鉴权下载 (裸 href 不带 JWT 会 401)
    if (mapped) {
      const name = rawHref.split("/").filter(Boolean).pop() || "download";
      return (
        <a
          href={mapped}
          onClick={(e) => {
            e.preventDefault();
            downloadWithAuth(mapped, name);
          }}
          className="text-hermes-600 hover:text-hermes-700 underline underline-offset-2 cursor-pointer"
        >
          {children}
        </a>
      );
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className="text-hermes-600 hover:text-hermes-700 underline underline-offset-2">
        {children}
      </a>
    );
  },

  // Horizontal rule
  hr() {
    return <hr className="my-4 border-slate-200" />;
  },

  // Strong / emphasis
  strong({ children }: any) {
    return <strong className="font-semibold text-slate-900">{children}</strong>;
  },
  em({ children }: any) {
    return <em className="italic">{children}</em>;
  },

  // Images — /mnt/user-data/outputs/ 虚拟路径映射为产物端点 URL (带鉴权 blob)
  img({ src, alt }: any) {
    const mapped =
      typeof src === "string"
        ? resolveOutputsUrl(useChatStore.getState().activeThreadId, src)
        : null;
    if (mapped) return <OutputsImage src={mapped} alt={alt} />;
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={src} alt={alt} className="my-2 rounded-lg max-w-full" />;
  },
};

// ── 流式渲染分级 (Phase 1) ───────────────────────────────────────
// 流式生长中的那条 AI 消息: 代码块降级为纯 <pre><code>, 不做语法高亮
// (高亮是流式期间的主要渲染开销); isLiveStreaming 结束后 React 重渲染
// 自然切回完整 markdownComponents, 恢复高亮。
function streamingCode({ className, children, ...props }: any) {
  const match = /language-(\w+)/.exec(className || "");
  const codeString = String(children).replace(/\n$/, "");
  if (match) {
    return (
      <div className="my-3 -mx-0 overflow-hidden rounded-lg border border-slate-200">
        <div className="flex items-center justify-between px-4 py-1.5 bg-slate-100 border-b border-slate-200">
          <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wider">{match[1]}</span>
        </div>
        <pre className="m-0 p-4 overflow-x-auto text-[13px] leading-relaxed bg-[#282c34] text-slate-100">
          <code>{codeString}</code>
        </pre>
      </div>
    );
  }
  return (
    <code className="px-1.5 py-0.5 bg-slate-100 text-slate-800 rounded text-[0.88em] font-mono" {...props}>
      {children}
    </code>
  );
}

const streamingMarkdownComponents: Record<string, React.FC<any>> = {
  ...markdownComponents,
  code: streamingCode,
};

// ── Phase 7: human 消息附件 chip ──────────────────────────────────
// 附件结构化信息来自 message.metadata.files (乐观消息由 ChatPanel 写入,
// 历史消息由后端 extra_metadata.files 还原), 元素形如 {filename, size, path, mime_type}
interface HumanAttachment {
  filename: string;
  size?: number;
}

function extractHumanAttachments(metadata: Record<string, unknown> | undefined): HumanAttachment[] {
  const files = metadata?.files;
  if (!Array.isArray(files)) return [];
  return files
    .filter((f): f is Record<string, unknown> => typeof f === "object" && f !== null)
    .map((f) => ({
      filename: String(f.filename || f.original_name || "file"),
      size: typeof f.size === "number" ? f.size : undefined,
    }));
}

function HumanAttachmentChips({ files }: { files: HumanAttachment[] }) {
  if (files.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5 mt-2">
      {files.map((f, i) => (
        <span
          key={`${f.filename}-${i}`}
          className="flex items-center gap-1.5 px-2 py-0.5 rounded-md text-[11px] bg-white/10 border border-white/20 text-white/90"
        >
          <FileText className="w-3 h-3 flex-shrink-0" />
          <span className="max-w-[160px] truncate">{f.filename}</span>
          {f.size !== undefined && (
            <span className="text-white/50">{formatFileSize(f.size)}</span>
          )}
        </span>
      ))}
    </div>
  );
}

// ── 思考过程卡片（折叠）───────────────────────────────────────────
// Reasoning 卡片: 流式中展开 + "思考中... Ns" 计时;
// 思考结束 1s 后自动收起一次 (hasAutoClosedRef 防重复干预);
// 历史消息默认收起, 用户手动展开后不再干预
const ThinkingCard = React.memo(function ThinkingCard({ message }: { message: ChatMessage }) {
  // 仅"当前正在生长的 thinking 气泡"算思考中 — 历史 thinking 无 thinkingEndAt,
  // 不能用全局 isStreaming 判断, 否则新一轮流式开始时历史卡片会误展开
  const isThinking = useChatStore(
    (s) => s.isStreaming && s._streamingThinkingId === message.id,
  );
  const [expanded, setExpanded] = React.useState(isThinking);
  const hasAutoClosedRef = React.useRef(false);
  const [elapsedSec, setElapsedSec] = React.useState(0);

  // 流式中每秒刷新已用时
  React.useEffect(() => {
    if (!isThinking || !message.thinkingStartAt) return;
    const startAt = message.thinkingStartAt;
    const update = () => setElapsedSec(Math.floor((Date.now() - startAt) / 1000));
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [isThinking, message.thinkingStartAt]);

  // 思考结束 1s 后自动收起一次 (仅当卡片仍处于展开态; 历史消息默认收起不触发)
  React.useEffect(() => {
    if (!message.thinkingEndAt || !expanded || hasAutoClosedRef.current) return;
    const timer = setTimeout(() => {
      hasAutoClosedRef.current = true;
      setExpanded(false);
    }, 1000);
    return () => clearTimeout(timer);
  }, [message.thinkingEndAt, expanded]);

  const toggle = () => {
    // 思考结束后用户手动操作过 → 不再自动干预
    if (message.thinkingEndAt) hasAutoClosedRef.current = true;
    setExpanded((e) => !e);
  };

  const durationSec =
    message.thinkingStartAt && message.thinkingEndAt
      ? Math.max(0, Math.round((message.thinkingEndAt - message.thinkingStartAt) / 1000))
      : null;
  const title = isThinking
    ? `思考中... ${elapsedSec}s`
    : durationSec !== null
    ? `思考了 ${durationSec} 秒`
    : "思考过程";

  return (
    <div className="flex gap-3 animate-fade-in-up">
      <div className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 shadow-sm bg-purple-500/10">
        <Brain className="w-4 h-4 text-purple-500" />
      </div>
      <div className="max-w-[80%] min-w-[200px] rounded-xl border border-purple-200 bg-purple-50/50 overflow-hidden">
        <button
          onClick={toggle}
          className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-purple-100/50 transition-colors"
        >
          {expanded ? (
            <ChevronDown className="w-3.5 h-3.5 text-purple-400" />
          ) : (
            <ChevronRight className="w-3.5 h-3.5 text-purple-400" />
          )}
          <Brain className="w-3.5 h-3.5 text-purple-400" />
          <span className="text-sm font-medium text-purple-600">{title}</span>
        </button>
        {expanded && (
          <div className="px-4 py-2 text-base text-slate-600 leading-relaxed whitespace-pre-wrap border-t border-purple-100">
            {message.content}
          </div>
        )}
      </div>
    </div>
  );
});

const MessageItem = React.memo(function MessageItem({ message, isLiveStreaming = false }: MessageItemProps) {
  const config = roleConfig[message.role] || roleConfig.system;
  const Icon = config.icon;
  // 平滑打字机: 仅流式生长的 AI 正文渐进揭示, 其余消息直接跳变 (hook 内部处理);
  // 必须在提前返回之前调用, 保证 hook 数量稳定
  const smoothContent = useSmoothContent(message.content, isLiveStreaming);

  // ── SubAgent 内部事件 → 不渲染 (只存在于 subConversations 中) ──
  if (
    message.msgType === "subagent_tool_call" ||
    message.msgType === "subagent_tool_result" ||
    message.msgType === "subagent_thinking"
  ) {
    return null;
  }

  if (message.msgType === "tool_call" || message.msgType === "tool_result") {
    // present_files → 产物文件卡片;
    // 历史消息 tool_args 缺失/截断导致解析不出 filepaths 时降级为普通工具卡片
    if (
      message.msgType === "tool_call" &&
      message.metadata?.tool_name === "present_files" &&
      parsePresentedFilepaths(message.metadata?.tool_args).length > 0
    ) {
      return <ArtifactFileList message={message} />;
    }
    return <ToolCallCard message={message} />;
  }

  if (message.msgType === "subagent_start" || message.msgType === "subagent_progress" || message.msgType === "subagent_end") {
    return <SubAgentCard message={message} />;
  }

  // 思考过程 — 折叠显示
  if (message.msgType === "thinking") {
    return <ThinkingCard message={message} />;
  }

  const isAi = message.role === "ai";
  const isHuman = message.role === "human";
  const isSystem = message.role === "system";

  return (
    <div className={cn("flex gap-3 animate-fade-in-up", isHuman && "flex-row-reverse")}>
      <div className={cn(
        "w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 shadow-sm",
        config.bg
      )}>
        <Icon className={cn("w-4 h-4", config.iconColor)} />
      </div>

      <div className={cn(
        "max-w-[80%] rounded-xl px-4 py-3 text-base shadow-sm",
        isHuman
          ? "bg-slate-900 text-white rounded-br-md"
          : isSystem
          ? "bg-red-50 border border-red-200 text-red-700"
          : "bg-white border border-slate-200 rounded-bl-md"
      )}>
        <div className="flex items-center gap-2 mb-2">
          <span className={cn(
            "text-[10px] font-semibold",
            isHuman ? "text-hermes-300" : "text-slate-500"
          )}>
            {config.label}
          </span>
          <span className={cn(
            "text-[9px]",
            isHuman ? "text-hermes-300/70" : "text-slate-400"
          )}>
            {formatDateTime(message.createdAt)}
          </span>
          {message.tokenCount > 0 && !isHuman && (
            <span className="text-[9px] text-slate-400">{message.tokenCount} tokens</span>
          )}
        </div>

        <div className="break-words">
          {isAi ? (
            <>
              <ReactMarkdown
                remarkPlugins={[remarkGfm, remarkMath]}
                rehypePlugins={[rehypeKatex]}
                components={isLiveStreaming ? streamingMarkdownComponents : markdownComponents}
              >
                {smoothContent}
              </ReactMarkdown>
              {/* Phase 7: citation 来源聚合列表 (正文正则提取, 按 url 去重) */}
              <CitationSources content={message.content} />
            </>
          ) : (
            <span className={cn(
              "whitespace-pre-wrap",
              isHuman ? "text-white/90" : "text-slate-700"
            )}>
              {message.content}
            </span>
          )}
        </div>

        {/* Phase 7: human 消息附件 chip 列表 (替代原 [Attached N file(s)] 纯文本) */}
        {isHuman && <HumanAttachmentChips files={extractHumanAttachments(message.metadata)} />}

        {message.metadata?.tokens ? <TokenUsageBar tokens={message.metadata.tokens as TokenUsage} /> : null}
      </div>
    </div>
  );
});

export default MessageItem;
