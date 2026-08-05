"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { HelpCircle, Send, X } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { SSEClient } from "@/lib/sse-client";
import { cn } from "@/lib/utils";

interface ClarificationDialogProps {
  threadId: string;
}

export default function ClarificationDialog({ threadId }: ClarificationDialogProps) {
  const { pendingClarification, setPendingClarification, handleSSEEvent, setStreaming, setError, setStopClarificationFn } =
    useChatStore();
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const sseRef = useRef<SSEClient | null>(null);

  // Register stop function so ChatPanel can cancel clarification streaming
  useEffect(() => {
    setStopClarificationFn(() => {
      sseRef.current?.stop();
      setSubmitting(false);
    });
    return () => setStopClarificationFn(null);
  }, [setStopClarificationFn]);

  const handleSubmit = useCallback(async () => {
    if (!answer.trim() || !pendingClarification || submitting) return;
    setSubmitting(true);

    // Display user's answer as a message
    const { addMessage } = useChatStore.getState();
    addMessage({
      role: "human",
      content: answer.trim(),
      msgType: "clarification_answer",
      metadata: { clarification_id: pendingClarification.id },
      tokenCount: 0,
    });

    setPendingClarification(null);
    setAnswer(""); // 清空答案, 避免下一轮澄清预填上次内容
    setStreaming(true);
    setError(null);

    // Start SSE stream to receive resumed execution
    const sse = new SSEClient({
      onEvent: (event) => {
        handleSSEEvent(event);
        if (event.type === "finished" || event.type === "error") {
          setStreaming(false);
          setStopClarificationFn(null);
        }
        // Guard: agent 在流中再次请求澄清 — 恢复可交互状态并保留停止函数,
        // 否则 submitting 卡在 true 且无法取消, 对话框死锁
        if (event.type === "clarification") {
          setStreaming(false);
          setSubmitting(false);
          setStopClarificationFn(() => {
            sseRef.current?.stop();
            setSubmitting(false);
          });
        }
      },
      onStatus: (status) => {
        if (status === "error") {
          setStreaming(false);
          setStopClarificationFn(null);
        }
      },
    });
    sseRef.current = sse;

    try {
      await sse.connect(`/api/execute/${threadId}/respond`, {
        answer: answer.trim(),
      });
    } catch (err: any) {
      console.error("Clarification submit failed:", err);
      setError("回复提交失败，请重试");
    } finally {
      setStreaming(false);
      setSubmitting(false);
      setStopClarificationFn(null);
    }
  }, [answer, pendingClarification, submitting, threadId, handleSSEEvent, setPendingClarification, setStreaming, setError, setStopClarificationFn]);

  const handleDismiss = useCallback(() => {
    setPendingClarification(null);
    setAnswer("");
  }, [setPendingClarification]);

  if (!pendingClarification) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/30 backdrop-blur-sm p-4">
      <div className="bg-card border rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden animate-in slide-in-from-bottom-4 duration-300">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b bg-primary/5">
          <div className="flex items-center gap-2 text-primary">
            <HelpCircle className="w-5 h-5" />
            <h3 className="text-sm font-semibold">需要确认</h3>
          </div>
          <button
            onClick={handleDismiss}
            className="p-1 rounded-md hover:bg-background/80 transition"
            disabled={submitting}
          >
            <X className="w-4 h-4 text-muted-foreground" />
          </button>
        </div>

        {/* Question */}
        <div className="p-5 space-y-3">
          <p className="text-sm leading-relaxed text-foreground">
            {pendingClarification.question}
          </p>

          {pendingClarification.context && (
            <p className="text-xs text-muted-foreground bg-muted/50 rounded-lg p-2">
              {pendingClarification.context}
            </p>
          )}

          {/* Predefined options */}
          {pendingClarification.options && pendingClarification.options.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {pendingClarification.options.map((opt) => (
                <button
                  key={opt}
                  onClick={() => setAnswer(opt)}
                  disabled={submitting}
                  className={cn(
                    "px-3 py-1.5 text-xs rounded-full border transition",
                    answer === opt
                      ? "bg-primary text-primary-foreground border-primary"
                      : "bg-background hover:bg-accent border-border"
                  )}
                >
                  {opt}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Input */}
        <div className="px-5 pb-5 flex gap-2">
          <input
            type="text"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder="输入你的回复..."
            disabled={submitting}
            className="flex-1 px-3 py-2 text-sm border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:opacity-50"
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSubmit()}
            autoFocus
          />
          <button
            onClick={handleSubmit}
            disabled={!answer.trim() || submitting}
            className="p-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition disabled:opacity-50"
          >
            {submitting ? (
              <div className="w-4 h-4 border-2 border-primary-foreground/30 border-t-primary-foreground rounded-full animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
