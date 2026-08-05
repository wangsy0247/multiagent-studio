"use client";

import { useRef, useEffect, useMemo } from "react";
import { Bot, MessageSquare, GitBranch, CheckCircle2, XCircle } from "lucide-react";
import { ChatMessage } from "@/lib/types";
import MessageItem from "./MessageItem";
import ProcessGroup from "./ProcessGroup";

interface MessageListProps {
  messages: ChatMessage[];
  isStreaming: boolean;
}

type MsgGroup =
  | { kind: "single"; msg: ChatMessage }
  | { kind: "process"; msgs: ChatMessage[] };

/** 是否为"过程消息" (折叠进执行过程组) — 正文/里程碑/错误保持可见 */
function isProcessMessage(msg: ChatMessage): boolean {
  if (msg.msgType === "task_boundary") return false; // 任务分隔条
  if (msg.role === "human") return false;
  if (msg.msgType === "error") return false; // 错误必须可见, 不能藏进折叠组
  // present_files 产物卡片单独归组, 不进折叠 (用户必须能直接看到交付文件)
  if (
    msg.msgType === "tool_call" &&
    (msg.metadata as Record<string, unknown> | undefined)?.tool_name === "present_files"
  )
    return false;
  if (msg.role === "tool" || msg.role === "subagent") return true;
  if (msg.msgType === "thinking") return true;
  if (msg.role === "system") {
    // 降级警告保持可见 (模式切换必须感知), 其余系统消息全部折叠
    const et = (msg.metadata as Record<string, unknown> | undefined)?.event_type;
    return et !== "team_degrade";
  }
  return false; // ai 正文 (text/message/clarification 等)
}

/** 把连续的过程消息聚合成折叠组 (Hermes 式"执行过程"分组) */
function groupMessages(messages: ChatMessage[]): MsgGroup[] {
  const groups: MsgGroup[] = [];
  for (const msg of messages) {
    if (isProcessMessage(msg)) {
      const last = groups[groups.length - 1];
      if (last && last.kind === "process") last.msgs.push(msg);
      else groups.push({ kind: "process", msgs: [msg] });
    } else {
      groups.push({ kind: "single", msg });
    }
  }
  return groups;
}

export default function MessageList({ messages, isStreaming }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // hooks 必须位于提前返回之前 — 空消息 → 有消息时 hook 数量不能变化
  const groups = useMemo(() => groupMessages(messages), [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-gradient-to-b from-slate-50 to-white">
        <div className="text-center max-w-sm px-6 animate-fade-in">
          <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-slate-100 to-slate-200 flex items-center justify-center shadow-sm">
            <Bot className="w-7 h-7 text-slate-600" />
          </div>
          <h3 className="text-base font-semibold text-slate-900 mb-2">开始对话</h3>
          <p className="text-sm text-slate-500 mb-6">
            输入任务指令启动多 Agent 协作
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-white border rounded-xl p-3 text-left">
              <MessageSquare className="w-4 h-4 text-slate-400 mb-2" />
              <p className="text-xs font-medium text-slate-700">直接对话</p>
              <p className="text-[10px] text-slate-400 mt-0.5">使用默认编排启动</p>
            </div>
            <div className="bg-white border rounded-xl p-3 text-left">
              <GitBranch className="w-4 h-4 text-slate-400 mb-2" />
              <p className="text-xs font-medium text-slate-700">画布编排</p>
              <p className="text-[10px] text-slate-400 mt-0.5">先配置 Agent 拓扑</p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
      {groups.map((group, gi) => {
        // ── 过程消息组: Hermes 式折叠 ("执行过程 · N 步") ──
        if (group.kind === "process") {
          const isLive = isStreaming && gi === groups.length - 1;
          return (
            <ProcessGroup
              key={`pg-${group.msgs[0].id}`}
              messages={group.msgs}
              isLive={isLive}
            />
          );
        }
        const msg = group.msg;
        // ── 任务边界: 渲染为分隔条, 显示任务名 + 状态 ──
        if (msg.msgType === "task_boundary") {
          const meta = msg.metadata as Record<string, unknown> | undefined;
          const taskTitle = String(meta?.title || meta?.task_id || "");
          const taskStatus = String(meta?.status || "");
          const isSuccess = taskStatus === "completed" || taskStatus === "approved";
          const isFailed = taskStatus === "failed";
          return (
            <div key={msg.id} className="flex items-center gap-2 py-2">
              <div className="flex-1 h-px bg-slate-200" />
              <div
                className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium ${
                  isSuccess
                    ? "bg-green-50 text-green-700"
                    : isFailed
                    ? "bg-red-50 text-red-600"
                    : "bg-slate-100 text-slate-500"
                }`}
              >
                {isSuccess ? (
                  <CheckCircle2 className="w-3 h-3" />
                ) : isFailed ? (
                  <XCircle className="w-3 h-3" />
                ) : null}
                <span className="max-w-[200px] truncate">{taskTitle}</span>
                <span className="opacity-60">
                  {taskStatus === "completed"
                    ? "已完成"
                    : taskStatus === "approved"
                    ? "已审查"
                    : taskStatus === "failed"
                    ? "失败"
                    : taskStatus}
                </span>
              </div>
              <div className="flex-1 h-px bg-slate-200" />
            </div>
          );
        }
        // ── 普通消息 ──
        // 只有"流式中且为最后一条 AI 生长消息"才开平滑打字机/代码块降级, 避免每条消息都挂 rAF
        const isLiveStreaming =
          isStreaming && gi === groups.length - 1 && msg.role === "ai";
        return <MessageItem key={msg.id} message={msg} isLiveStreaming={isLiveStreaming} />;
      })}

      {isStreaming && (
        <div className="flex items-center gap-3 pl-11 py-2 animate-fade-in">
          <div className="w-7 h-7 rounded-full bg-slate-100 flex items-center justify-center">
            <Bot className="w-3.5 h-3.5 text-slate-500" />
          </div>
          <div className="flex gap-1.5">
            <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
            <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
            <span className="w-2 h-2 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
          </div>
          <span className="text-xs text-slate-400">AI 正在思考...</span>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
