"use client";

import { useState, useEffect, lazy, Suspense } from "react";
import { useParams } from "next/navigation";
import { MessageCircle, BarChart3 } from "lucide-react";
import { threadsAPI } from "@/lib/api-client";
import { ThreadDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

const ChatPanel = lazy(() => import("@/components/chat/ChatPanel"));
const MonitoringPanel = lazy(() => import("@/components/monitoring/MonitoringPanel"));

function PanelFallback() {
  return (
    <div className="flex items-center justify-center h-full bg-slate-50">
      <div className="w-6 h-6 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
    </div>
  );
}

type TabType = "chat" | "monitor";

export default function WorkspacePage() {
  const { thread_id } = useParams<{ thread_id: string }>();
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [tab, setTab] = useState<TabType>("chat");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { loadThread(); }, [thread_id]);

  async function loadThread() {
    setLoading(true); setError(null);
    try {
      const { data } = await threadsAPI.get(thread_id);
      setThread(data);
    } catch (err: any) {
      setError(err.response?.data?.detail || "加载会话失败");
    } finally { setLoading(false); }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full bg-slate-50">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-3 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
          <p className="text-sm text-slate-500">加载会话...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full bg-slate-50">
        <div className="text-center">
          <p className="text-red-600 mb-3">{error}</p>
          <button onClick={loadThread} className="px-5 py-2 text-sm bg-slate-900 text-white rounded-lg hover:bg-slate-800">重试</button>
        </div>
      </div>
    );
  }

  const tabs = [
    { id: "chat" as TabType, icon: MessageCircle, label: "对话" },
    { id: "monitor" as TabType, icon: BarChart3, label: "监控" },
  ];

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center border-b border-slate-200 px-4 h-11 bg-white flex-shrink-0">
        <div className="flex items-center gap-0.5">
          {tabs.map((t) => {
            const isActive = tab === t.id;
            return (
              <button key={t.id} onClick={() => setTab(t.id)}
                className={cn("flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-sm transition-all duration-150",
                  isActive ? "bg-slate-100 text-slate-900 font-medium" : "text-slate-500 hover:text-slate-700 hover:bg-slate-50")}>
                <t.icon className={cn("w-3.5 h-3.5", isActive && "text-slate-700")} />
                {t.label}
              </button>
            );
          })}
        </div>
        <div className="flex-1" />
        <span className="text-xs text-slate-400 truncate max-w-[250px]">{thread?.title || "新会话"}</span>
      </div>
      <div className="flex-1 overflow-hidden">
        <Suspense fallback={<PanelFallback />}>
          {tab === "chat" && <ChatPanel threadId={thread_id} threadTitle={thread?.title || ""} />}
          {tab === "monitor" && <MonitoringPanel threadId={thread_id} />}
        </Suspense>
      </div>
    </div>
  );
}
