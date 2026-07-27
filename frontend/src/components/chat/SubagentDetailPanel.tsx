"use client";

import React from "react";
import {
  X, Network, Clock, CheckCircle, XCircle, AlertTriangle,
  Cpu, Brain, BarChart3, MessageSquare, Wrench,
} from "lucide-react";
import { SubAgentResultData } from "@/lib/types";
import { cn, formatDuration } from "@/lib/utils";
import { useChatStore } from "@/lib/chat-store";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// ── 状态配置 ──────────────────────────────────────────────────────────
const STATUS_META: Record<string, { icon: typeof CheckCircle; color: string; label: string }> = {
  running:   { icon: Clock,   color: "text-hermes-600",   label: "执行中" },
  success:   { icon: CheckCircle, color: "text-emerald-500", label: "成功" },
  error:     { icon: XCircle, color: "text-red-500",     label: "失败" },
  cancelled: { icon: AlertTriangle, color: "text-amber-500",  label: "已取消" },
  timed_out: { icon: Clock,   color: "text-orange-500",  label: "超时" },
  max_iterations_reached: { icon: Cpu, color: "text-hermes-600", label: "达到上限" },
};

export default function SubagentDetailPanel() {
  const {
    messages,
    subConversations,
    selectedSubagentId: selectedMessageId,
    selectSubagent,
  } = useChatStore();
  const onClose = () => selectSubagent(null);

  if (!selectedMessageId) return null;

  // 找到被选中的 subagent 消息
  const targetMsg = messages.find((m) => m.id === selectedMessageId);
  if (!targetMsg) return null;

  const result = targetMsg.metadata?.subagent_result as SubAgentResultData | undefined;
  const name = (targetMsg.metadata?.subagent_name as string) || "SubAgent";
  const instruction = targetMsg.metadata?.instruction as string;
  const status = result?.status || "running";
  const statusMeta = STATUS_META[status] || STATUS_META.success;
  const StatusIcon = statusMeta.icon;
  const durationMs = targetMsg.metadata?.duration_ms as number;

  // 实时子会话消息 (来自 SSE subagent_* 事件)
  const liveMessages = name ? (subConversations[name] || []) : [];
  const hasLiveMessages = liveMessages.length > 0;

  // Token 汇总
  const totalTokens = result?.token_usage_records?.reduce(
    (s, r) => s + (r.total_tokens || 0), 0,
  ) ?? 0;
  const totalInput = result?.token_usage_records?.reduce(
    (s, r) => s + (r.input_tokens || 0), 0,
  ) ?? 0;
  const totalOutput = result?.token_usage_records?.reduce(
    (s, r) => s + (r.output_tokens || 0), 0,
  ) ?? 0;

  // 推理步骤 (ai_messages)
  const aiMessages = result?.ai_messages || [];
  const hasReasoning = aiMessages.length > 0;

  // 输出
  const output = result?.output || targetMsg.content || "";
  const hasOutput = output.length > 0 && status === "success";

  return (
    <aside className="w-[420px] border-l bg-white flex-shrink-0 overflow-y-auto flex flex-col">
      {/* ── Header ── */}
      <div className="flex items-center justify-between p-4 border-b bg-slate-50/50 sticky top-0 z-10 backdrop-blur-sm">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-emerald-100 flex items-center justify-center flex-shrink-0">
            <Network className="w-4 h-4 text-emerald-600" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-slate-900 truncate">{name}</h3>
            <div className="flex items-center gap-2 text-[10px]">
              <StatusIcon className={cn("w-3 h-3", statusMeta.color)} />
              <span className={cn("font-medium", statusMeta.color)}>{statusMeta.label}</span>
              {durationMs != null ? (
                <span className="text-slate-400 flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {formatDuration(durationMs)}
                </span>
              ) : null}
            </div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="p-1.5 rounded-lg hover:bg-slate-200 transition-colors flex-shrink-0"
        >
          <X className="w-4 h-4 text-slate-500" />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* ── 任务指令 ── */}
        {instruction && (
          <div>
            <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2 flex items-center gap-1">
              <MessageSquare className="w-3 h-3" /> 任务指令
            </p>
            <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg text-xs text-slate-700 leading-relaxed">
              {instruction}
            </div>
          </div>
        )}

        {/* ── 统计摘要 ── */}
        <div>
          <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2 flex items-center gap-1">
            <BarChart3 className="w-3 h-3" /> 执行统计
          </p>
          <div className="grid grid-cols-2 gap-2">
            <div className="p-2.5 bg-slate-50 border border-slate-200 rounded-lg text-center">
              <p className="text-lg font-bold text-slate-800">
                {result?.iterations ?? "—"}
              </p>
              <p className="text-[10px] text-slate-400">Turns</p>
            </div>
            <div className="p-2.5 bg-slate-50 border border-slate-200 rounded-lg text-center">
              <p className="text-lg font-bold text-slate-800">
                {totalTokens > 0 ? (totalTokens / 1000).toFixed(1) + "k" : "—"}
              </p>
              <p className="text-[10px] text-slate-400">Tokens</p>
            </div>
          </div>
          {totalTokens > 0 && (
            <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-slate-500">
              <span>输入: {totalInput.toLocaleString()}</span>
              <span>输出: {totalOutput.toLocaleString()}</span>
            </div>
          )}
        </div>

        {/* ── 实时会话时间线 (优先展示) ── */}
        {hasLiveMessages && (
          <div>
            <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
              💬 实时会话
            </p>
            <div className="space-y-1.5 max-h-[50vh] overflow-y-auto">
              {liveMessages.map((msg, i) => {
                if (msg.msgType === "thinking") {
                  return (
                    <div key={i} className="flex items-start gap-2 p-2 bg-purple-50 border border-purple-100 rounded-lg">
                      <Brain className="w-3 h-3 text-purple-400 mt-0.5 flex-shrink-0" />
                      <p className="text-[11px] text-slate-600 leading-relaxed whitespace-pre-wrap">{msg.content}</p>
                    </div>
                  );
                }
                if (msg.msgType === "tool_call") {
                  const args = msg.metadata?.tool_args as Record<string, unknown> | undefined;
                  return (
                    <div key={i} className="flex items-start gap-2 p-2 bg-amber-50 border border-amber-100 rounded-lg">
                      <Wrench className="w-3 h-3 text-amber-500 mt-0.5 flex-shrink-0" />
                      <div className="min-w-0">
                        <span className="text-[11px] font-medium text-amber-700">🔧 {msg.content}</span>
                        {args && Object.keys(args).length > 0 && (
                          <pre className="text-[9px] text-amber-600 mt-0.5 font-mono overflow-x-auto">
                            {JSON.stringify(args, null, 1).slice(0, 200)}
                          </pre>
                        )}
                      </div>
                    </div>
                  );
                }
                if (msg.msgType === "tool_result") {
                  return (
                    <div key={i} className="flex items-start gap-2 p-2 bg-green-50 border border-green-100 rounded-lg">
                      <CheckCircle className="w-3 h-3 text-green-400 mt-0.5 flex-shrink-0" />
                      <p className="text-[10px] text-slate-600 leading-relaxed font-mono line-clamp-3">{msg.content}</p>
                    </div>
                  );
                }
                return null;
              })}
            </div>
          </div>
        )}

        {/* ── 输出结果 ── */}
        {hasOutput && (
          <div>
            <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2">
              📤 输出结果
            </p>
            <div className="p-3 bg-white border border-slate-200 rounded-lg text-sm leading-relaxed prose prose-sm max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {output}
              </ReactMarkdown>
            </div>
          </div>
        )}

        {/* ── 错误信息 ── */}
        {result?.error && status !== "success" && (
          <div>
            <p className="text-[10px] font-semibold text-red-400 uppercase tracking-wider mb-2">
              ⚠️ 错误信息
            </p>
            <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-xs text-red-700 font-mono">
              {result.error}
            </div>
          </div>
        )}

        {/* ── 推理过程 (可折叠) ── */}
        {hasReasoning && (
          <details open className="group">
            <summary className="cursor-pointer list-none flex items-center gap-1 text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2 hover:text-slate-500">
              <Brain className="w-3 h-3" />
              推理过程 ({aiMessages.length} 步)
              <span className="text-[9px] text-slate-300 ml-auto group-open:hidden">点击展开</span>
            </summary>
            <div className="space-y-1.5 max-h-96 overflow-y-auto">
              {aiMessages.map((msg, i) => {
                const content =
                  typeof msg.content === "string"
                    ? msg.content
                    : JSON.stringify(msg.content);
                // 检测是否包含 tool_calls
                const hasToolCalls =
                  Boolean(msg.tool_calls) &&
                  Array.isArray(msg.tool_calls) &&
                  (msg.tool_calls as unknown[]).length > 0;

                return (
                  <div
                    key={(msg.id as string) || i}
                    className="p-2.5 bg-slate-50 border border-slate-100 rounded-lg"
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[10px] font-mono text-slate-400">
                        [{i + 1}/{aiMessages.length}]
                      </span>
                      {hasToolCalls && (
                        <span className="text-[9px] px-1.5 py-0.5 bg-amber-50 text-amber-600 rounded border border-amber-200 flex items-center gap-1">
                          <Wrench className="w-2.5 h-2.5" />
                          工具调用
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-slate-600 leading-relaxed whitespace-pre-wrap line-clamp-6">
                      {content}
                    </p>
                    {/* tool_calls 详情 */}
                    {hasToolCalls && (
                      <div className="mt-2 pt-2 border-t border-slate-200">
                        {(msg.tool_calls as unknown[]).map((tc: any, j: number) => (
                          <div
                            key={tc.id || j}
                            className="text-[10px] text-slate-500 font-mono bg-white rounded p-1.5 mt-1"
                          >
                            <span className="text-amber-600 font-medium">{tc.name}</span>
                            {tc.args ? (
                              <pre className="mt-0.5 text-[9px] text-slate-400 overflow-x-auto">
                                {JSON.stringify(tc.args, null, 1).slice(0, 200)}
                              </pre>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </details>
        )}

        {/* ── Token 明细 ── */}
        {result?.token_usage_records && result.token_usage_records.length > 0 && (
          <details className="group">
            <summary className="cursor-pointer list-none flex items-center gap-1 text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-2 hover:text-slate-500">
              🪙 Token 明细 ({result.token_usage_records.length} 次调用)
            </summary>
            <div className="space-y-1">
              {result.token_usage_records.map((r, i) => (
                <div
                  key={r.source_run_id || i}
                  className="flex items-center justify-between text-[10px] p-1.5 bg-slate-50 rounded"
                >
                  <span className="text-slate-500 font-mono">
                    {(r.source_run_id || "").slice(0, 8)}
                  </span>
                  <span className="text-slate-600">
                    in:{r.input_tokens?.toLocaleString()} out:{r.output_tokens?.toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          </details>
        )}

        {/* ── 时序 ── */}
        {(result?.started_at || result?.completed_at) && (
          <div className="text-[10px] text-slate-400 space-y-1 pt-2 border-t border-slate-100">
            {result.started_at && (
              <p>开始: {new Date(result.started_at).toLocaleTimeString()}</p>
            )}
            {result.completed_at && (
              <p>完成: {new Date(result.completed_at).toLocaleTimeString()}</p>
            )}
            {result.started_at && result.completed_at && (
              <p>
                耗时:{" "}
                {(
                  (new Date(result.completed_at).getTime() -
                    new Date(result.started_at).getTime()) /
                  1000
                ).toFixed(1)}
                s
              </p>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
