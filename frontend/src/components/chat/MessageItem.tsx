"use client";

import React from "react";
import { Bot, User, Wrench, Network, AlertTriangle, Brain, ChevronDown, ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
// 按需加载语言 — 避免完整 Prism (300+ 语言) 拖慢编译
import SyntaxHighlighter from "react-syntax-highlighter/dist/esm/prism-light";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import java from "react-syntax-highlighter/dist/esm/languages/prism/java";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import c from "react-syntax-highlighter/dist/esm/languages/prism/c";
import cpp from "react-syntax-highlighter/dist/esm/languages/prism/cpp";

SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("js", javascript);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("ts", typescript);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("shell", bash);
SyntaxHighlighter.registerLanguage("sh", bash);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("markdown", markdown);
SyntaxHighlighter.registerLanguage("md", markdown);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("yml", yaml);
SyntaxHighlighter.registerLanguage("java", java);
SyntaxHighlighter.registerLanguage("go", go);
SyntaxHighlighter.registerLanguage("rust", rust);
SyntaxHighlighter.registerLanguage("c", c);
SyntaxHighlighter.registerLanguage("cpp", cpp);
import { ChatMessage, TokenUsage } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";
import ToolCallCard from "./ToolCallCard";
import SubAgentCard from "./SubAgentCard";
import TokenUsageBar from "./TokenUsageBar";

interface MessageItemProps {
  message: ChatMessage;
}

const roleConfig = {
  human: { icon: User, bg: "bg-blue-500/10", iconColor: "text-blue-500", label: "你" },
  ai: { icon: Bot, bg: "bg-slate-800/10", iconColor: "text-slate-700", label: "AI" },
  tool: { icon: Wrench, bg: "bg-amber-500/10", iconColor: "text-amber-500", label: "工具" },
  subagent: { icon: Network, bg: "bg-emerald-500/10", iconColor: "text-emerald-500", label: "SubAgent" },
  system: { icon: AlertTriangle, bg: "bg-destructive/10", iconColor: "text-destructive", label: "系统" },
};

// Custom renderers for every Markdown element — ensures pixel-perfect alignment
const markdownComponents: Record<string, React.FC<any>> = {
  // Code blocks with syntax highlighting
  code({ className, children, ...props }: any) {
    const match = /language-(\w+)/.exec(className || "");
    const codeString = String(children).replace(/\n$/, "");
    if (match) {
      return (
        <div className="my-3 -mx-0 overflow-hidden rounded-lg border border-slate-200">
          <div className="flex items-center justify-between px-4 py-1.5 bg-slate-100 border-b border-slate-200">
            <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wider">{match[1]}</span>
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
      <blockquote className="my-3 pl-4 border-l-[3px] border-blue-400 text-slate-600 italic">
        {children}
      </blockquote>
    );
  },

  // Headings
  h1({ children }: any) {
    return <h1 className="text-lg font-bold text-slate-900 mt-4 mb-2 first:mt-0">{children}</h1>;
  },
  h2({ children }: any) {
    return <h2 className="text-base font-bold text-slate-900 mt-3 mb-1.5 first:mt-0 pb-1 border-b border-slate-100">{children}</h2>;
  },
  h3({ children }: any) {
    return <h3 className="text-sm font-semibold text-slate-800 mt-3 mb-1 first:mt-0">{children}</h3>;
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

  // Links
  a({ children, href }: any) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:text-blue-800 underline underline-offset-2">
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

  // Images
  img({ src, alt }: any) {
    return <img src={src} alt={alt} className="my-2 rounded-lg max-w-full" />;
  },
};

// ── 思考过程卡片（折叠）───────────────────────────────────────────
const ThinkingCard = React.memo(function ThinkingCard({ message }: { message: ChatMessage }) {
  const [expanded, setExpanded] = React.useState(true);
  return (
    <div className="flex gap-3 animate-fade-in-up">
      <div className="w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 shadow-sm bg-purple-500/10">
        <Brain className="w-4 h-4 text-purple-500" />
      </div>
      <div className="max-w-[80%] min-w-[200px] rounded-xl border border-purple-200 bg-purple-50/50 overflow-hidden">
        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-purple-100/50 transition-colors"
        >
          {expanded ? (
            <ChevronDown className="w-3.5 h-3.5 text-purple-400" />
          ) : (
            <ChevronRight className="w-3.5 h-3.5 text-purple-400" />
          )}
          <Brain className="w-3.5 h-3.5 text-purple-400" />
          <span className="text-sm font-medium text-purple-600">思考过程</span>
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

const MessageItem = React.memo(function MessageItem({ message }: MessageItemProps) {
  const config = roleConfig[message.role] || roleConfig.system;
  const Icon = config.icon;

  if (message.msgType === "tool_call" || message.msgType === "tool_result") {
    return <ToolCallCard message={message} />;
  }

  if (message.msgType === "subagent_start" || message.msgType === "subagent_end") {
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
            isHuman ? "text-blue-300" : "text-slate-500"
          )}>
            {config.label}
          </span>
          <span className={cn(
            "text-[9px]",
            isHuman ? "text-blue-300/70" : "text-slate-400"
          )}>
            {formatDateTime(message.createdAt)}
          </span>
          {message.tokenCount > 0 && !isHuman && (
            <span className="text-[9px] text-slate-400">{message.tokenCount} tokens</span>
          )}
        </div>

        <div className="break-words">
          {isAi ? (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={markdownComponents}
            >
              {message.content}
            </ReactMarkdown>
          ) : (
            <span className={cn(
              "whitespace-pre-wrap",
              isHuman ? "text-white/90" : "text-slate-700"
            )}>
              {message.content}
            </span>
          )}
        </div>

        {message.metadata?.tokens ? <TokenUsageBar tokens={message.metadata.tokens as TokenUsage} /> : null}
      </div>
    </div>
  );
});

export default MessageItem;
