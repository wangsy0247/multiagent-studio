"use client";

import { useState, useEffect } from "react";
import { AlertTriangle, XCircle, ChevronDown, ChevronRight } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";
import { ChatMessage } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";

interface ErrorPanelProps {
  threadId: string;
}

export default function ErrorPanel({ threadId }: ErrorPanelProps) {
  const { messages } = useChatStore();
  const [expandedErrors, setExpandedErrors] = useState<Set<string>>(new Set());

  const errors = messages.filter((m) => m.msgType === "error" || m.role === "system");

  function toggle(id: string) {
    setExpandedErrors((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (errors.length === 0) {
    return (
      <div className="text-center py-6 text-xs text-muted-foreground">
        <XCircle className="w-5 h-5 mx-auto mb-1 text-muted-foreground/40" />
        暂无错误
      </div>
    );
  }

  return (
    <div className="space-y-1">
      {errors.map((msg) => (
        <div key={msg.id} className="border border-red-200 dark:border-red-800 rounded-lg overflow-hidden">
          <button
            onClick={() => toggle(msg.id)}
            className="w-full flex items-center gap-2 px-3 py-2 text-xs hover:bg-red-50 dark:hover:bg-red-950/20 transition"
          >
            {expandedErrors.has(msg.id) ? (
              <ChevronDown className="w-3 h-3" />
            ) : (
              <ChevronRight className="w-3 h-3" />
            )}
            <AlertTriangle className="w-3.5 h-3.5 text-red-500 flex-shrink-0" />
            <span className="font-medium text-red-600 flex-1 text-left truncate">
              {msg.content.slice(0, 80)}
            </span>
            <span className="text-muted-foreground">{formatDateTime(msg.createdAt)}</span>
          </button>

          {expandedErrors.has(msg.id) && (
            <div className="px-3 pb-2 text-xs text-muted-foreground border-t border-red-100 dark:border-red-900 pt-2 bg-red-50/50 dark:bg-red-950/10">
              <p className="whitespace-pre-wrap">{msg.content}</p>
              {msg.metadata && Object.keys(msg.metadata).length > 0 && (
                <pre className="mt-2 p-2 bg-muted rounded text-[10px] overflow-x-auto">
                  {JSON.stringify(msg.metadata, null, 2)}
                </pre>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
