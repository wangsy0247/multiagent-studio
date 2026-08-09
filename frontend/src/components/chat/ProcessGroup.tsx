"use client";

import React from "react";
import { ChevronDown, ChevronRight, Loader2, Settings2 } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import MessageItem from "./MessageItem";

interface ProcessGroupProps {
  messages: ChatMessage[];
  /** 是否为流式进行中的最新一组 (自动展开, 结束后自动收起) */
  isLive: boolean;
}

/** 组内单条: 系统消息/过渡正文渲染为紧凑行, 其余复用 MessageItem (工具/SubAgent/思考卡片) */
function GroupItem({ msg }: { msg: ChatMessage }) {
  if (msg.role === "system") {
    return (
      <div className="flex gap-1.5 py-0.5 text-xs text-slate-500">
        <span className="flex-shrink-0 text-slate-300">•</span>
        <span className="whitespace-pre-wrap break-words">{msg.content}</span>
      </div>
    );
  }
  // 轮内过渡正文: 折叠组内渲染为紧凑引用行, 不再占一个完整气泡
  if (msg.role === "ai" && (msg.msgType === "text" || msg.msgType === "message")) {
    return (
      <div className="flex gap-1.5 py-0.5 text-xs text-slate-500">
        <span className="flex-shrink-0 text-slate-300">💬</span>
        <span className="whitespace-pre-wrap break-words italic">{msg.content}</span>
      </div>
    );
  }
  return <MessageItem message={msg} />;
}

/** 折叠态 header 尾部显示的末步摘要 */
function summarize(msg: ChatMessage | undefined): string {
  if (!msg) return "";
  if (msg.role === "tool") {
    const name = (msg.metadata?.tool_name as string) || "unknown";
    return `${msg.msgType === "tool_call" ? "调用" : "结果"}: ${name}`;
  }
  if (msg.msgType === "thinking") return "思考过程";
  return (msg.content || "").replace(/\s+/g, " ").slice(0, 30);
}

const ProcessGroup = React.memo(function ProcessGroup({ messages, isLive }: ProcessGroupProps) {
  const [expanded, setExpanded] = React.useState(isLive);
  // 历史步骤区 (除最后一步外) 默认折叠, 用户手动展开后保持 (组件内状态)
  const [showHistory, setShowHistory] = React.useState(false);
  const userToggledRef = React.useRef(false);
  const contentRef = React.useRef<HTMLDivElement>(null);

  // 流式中自动展开最新组, 不再是最新组 (出现 AI 正文 / run 结束) 时自动收起;
  // 用户手动展开/收起后尊重用户选择, 不再跟随
  React.useEffect(() => {
    if (!userToggledRef.current) setExpanded(isLive);
  }, [isLive]);

  // 流式进行中: 内容区滚动跟随最新步骤 (用户上翻查看时不强制)
  React.useEffect(() => {
    if (!isLive || !expanded) return;
    const el = contentRef.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages.length, isLive, expanded]);

  const toggle = () => {
    userToggledRef.current = true;
    setExpanded((e) => !e);
  };

  // 两段式 ("只展示最新一步"): 历史步骤默认折叠为 "已完成 N 步", 最后一步常驻展示
  const latest = messages[messages.length - 1];
  const history = messages.slice(0, -1);

  return (
    <div className="pl-11 animate-fade-in">
      <div className="max-w-[80%]">
        <button
          onClick={toggle}
          className="flex items-center gap-1.5 py-1 text-left text-slate-500 hover:text-slate-700 transition-colors"
        >
          {expanded ? (
            <ChevronDown className="w-3.5 h-3.5 text-slate-400" />
          ) : (
            <ChevronRight className="w-3.5 h-3.5 text-slate-400" />
          )}
          {isLive ? (
            <Loader2 className="w-3.5 h-3.5 text-hermes-500 animate-spin" />
          ) : (
            <Settings2 className="w-3.5 h-3.5 text-slate-400" />
          )}
          <span className="text-xs font-medium">
            {isLive ? "执行中" : "执行过程"} · {messages.length} 步
          </span>
          {!expanded && (
            <span className="text-[10px] text-slate-400 truncate max-w-[40%]">
              {summarize(messages[messages.length - 1])}
            </span>
          )}
        </button>
        {expanded && (
          <div ref={contentRef} className="ml-[7px] border-l border-slate-200 pl-3 py-1 space-y-3 max-h-[480px] overflow-y-auto">
            {/* 历史步骤区: 默认折叠为 "已完成 N 步", 点击展开/收起 ("只展示最新" 模式) */}
            {history.length > 0 && (
              <button
                onClick={() => setShowHistory((v) => !v)}
                className="flex items-center gap-1.5 py-0.5 text-xs text-slate-400 hover:text-slate-600 transition-colors"
              >
                {showHistory ? (
                  <ChevronDown className="w-3 h-3" />
                ) : (
                  <ChevronRight className="w-3 h-3" />
                )}
                <span>{showHistory ? "收起" : `已完成 ${history.length} 步`}</span>
              </button>
            )}
            {showHistory &&
              history.map((m) => <GroupItem key={m.id} msg={m} />)}
            {/* 最新步骤区: 常驻展示, 流式期间始终可见当前在做什么 */}
            {latest && <GroupItem msg={latest} />}
          </div>
        )}
      </div>
    </div>
  );
});

export default ProcessGroup;
