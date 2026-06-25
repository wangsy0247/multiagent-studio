"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import { PanelLeftClose, PanelLeft, MessageCircle, GitGraph, BarChart3 } from "lucide-react";
import { threadsAPI } from "@/lib/api-client";
import { ThreadDetail } from "@/lib/types";
import { cn } from "@/lib/utils";
import AgentCanvas from "@/components/canvas/AgentCanvas";
import ChatPanel from "@/components/chat/ChatPanel";
import MonitoringPanel from "@/components/monitoring/MonitoringPanel";

type TabType = "graph" | "chat" | "monitor";

export default function WorkspacePage() {
  const { thread_id } = useParams<{ thread_id: string }>();
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [tab, setTab] = useState<TabType>("chat");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [rightPanelOpen, setRightPanelOpen] = useState(false);

  useEffect(() => {
    loadThread();
  }, [thread_id]);

  async function loadThread() {
    setLoading(true);
    setError(null);
    try {
      const { data } = await threadsAPI.get(thread_id);
      setThread(data);
    } catch (err: any) {
      setError(err.response?.data?.detail || "加载会话失败");
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-3 border-primary border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-muted-foreground">加载会话...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-center">
          <p className="text-destructive mb-2">{error}</p>
          <button
            onClick={loadThread}
            className="px-4 py-2 text-sm bg-primary text-primary-foreground rounded-lg"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full">
      {/* 主面板 */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Tab 栏 */}
        <div className="flex items-center border-b px-4 h-10 bg-card flex-shrink-0">
          <div className="flex items-center gap-1">
            {[
              { id: "chat" as TabType, icon: MessageCircle, label: "对话" },
              { id: "graph" as TabType, icon: GitGraph, label: "画布" },
              { id: "monitor" as TabType, icon: BarChart3, label: "监控" },
            ].map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={cn(
                  "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition",
                  tab === t.id
                    ? "bg-primary/10 text-primary font-medium"
                    : "text-muted-foreground hover:text-foreground hover:bg-accent"
                )}
              >
                <t.icon className="w-3.5 h-3.5" />
                {t.label}
              </button>
            ))}
          </div>
          <div className="flex-1" />
          <span className="text-xs text-muted-foreground truncate max-w-[200px]">
            {thread?.title || "新会话"}
          </span>
        </div>

        {/* 内容区 */}
        <div className="flex-1 overflow-hidden">
          {tab === "graph" && (
            <AgentCanvas threadId={thread_id} initialGraph={thread?.executionGraph || null} />
          )}
          {tab === "chat" && (
            <ChatPanel threadId={thread_id} threadTitle={thread?.title || ""} />
          )}
          {tab === "monitor" && (
            <MonitoringPanel threadId={thread_id} />
          )}
        </div>
      </div>

      {/* 右侧配置面板 (仅在画布模式显示) */}
      {tab === "graph" && (
        <>
          <button
            onClick={() => setRightPanelOpen(!rightPanelOpen)}
            className="absolute right-0 top-1/2 -translate-y-1/2 p-1 bg-card border rounded-l-md shadow-sm hover:bg-accent z-10"
          >
            {rightPanelOpen ? <PanelLeftClose className="w-4 h-4" /> : <PanelLeft className="w-4 h-4" />}
          </button>
          {rightPanelOpen && (
            <aside className="w-80 border-l bg-card overflow-y-auto p-4">
              <p className="text-sm text-muted-foreground">点击画布中的节点进行配置</p>
            </aside>
          )}
        </>
      )}
    </div>
  );
}
