"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { Plus, MessageSquare, Search, Settings, Trash2, AlertTriangle, Workflow, Bot, FolderKanban, CalendarClock, Puzzle, BarChart3 } from "lucide-react";
import { threadsAPI } from "@/lib/api-client";
import { useChatStore } from "@/lib/chat-store";
import { useUnreadStore } from "@/lib/unread-store";
import { ThreadSummary } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";

export default function Sidebar() {
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const router = useRouter();
  const pathname = usePathname();

  const chatTitle = useChatStore((s) => s.title);
  const chatActiveThreadId = useChatStore((s) => s.activeThreadId);
  const unreadTotal = useUnreadStore((s) => s.total);
  const refreshUnread = useUnreadStore((s) => s.refresh);

  useEffect(() => {
    loadThreads();
  }, []);

  // 定时任务未读红点：60s 轮询
  useEffect(() => {
    refreshUnread();
    const timer = setInterval(refreshUnread, 60_000);
    return () => clearInterval(timer);
  }, [refreshUnread]);

  useEffect(() => {
    if (chatTitle && chatTitle !== "新会话" && chatActiveThreadId) {
      setThreads((prev) =>
        prev.map((t) =>
          t.id === chatActiveThreadId ? { ...t, title: chatTitle } : t
        )
      );
    }
  }, [chatTitle, chatActiveThreadId]);

  useEffect(() => {
    if (!confirmDeleteId) return;
    const handleClick = () => setConfirmDeleteId(null);
    document.addEventListener("click", handleClick, { once: true });
    const timer = setTimeout(() => setConfirmDeleteId(null), 3000);
    return () => {
      document.removeEventListener("click", handleClick);
      clearTimeout(timer);
    };
  }, [confirmDeleteId]);

  async function loadThreads() {
    try {
      const { data } = await threadsAPI.list();
      const mapped: ThreadSummary[] = (data.threads || []).map((t: any) => ({
        ...t,
        updatedAt: t.updated_at || t.updatedAt,
        createdAt: t.created_at || t.createdAt,
        presetType: t.preset_type || t.presetType,
      }));
      setThreads(mapped);
    } catch (err) {
      console.error("加载会话列表失败", err);
    } finally {
      setLoading(false);
    }
  }

  async function createThread() {
    try {
      const { data } = await threadsAPI.create({ title: "新会话" });
      const mapped: ThreadSummary = {
        ...data,
        updatedAt: data.updated_at || data.updatedAt,
        createdAt: data.created_at || data.createdAt,
        presetType: data.preset_type || data.presetType,
      };
      setThreads((prev) => [mapped, ...prev]);
      router.push(`/threads/${data.id}`);
    } catch (err) {
      console.error("创建会话失败", err);
    }
  }

  async function handleDeleteClick(threadId: string, e: React.MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    if (confirmDeleteId === threadId) {
      try {
        await threadsAPI.delete(threadId);
        setThreads((prev) => prev.filter((t) => t.id !== threadId));
        // 删除的是当前打开的会话 → 返回首页, 避免停留在已删除的页面
        if (pathname?.includes(threadId)) {
          router.push("/");
        }
      } catch (err) {
        console.error("删除会话失败", err);
      }
      setConfirmDeleteId(null);
    } else {
      setConfirmDeleteId(threadId);
    }
  }

  const filtered = threads.filter(
    (t) =>
      t.title.toLowerCase().includes(search.toLowerCase()) ||
      t.id.includes(search)
  );

  const statusBadge = (status: string) => {
    const map: Record<string, string> = {
      idle: "bg-slate-300",
      running: "bg-green-500 animate-pulse",
      finished: "bg-slate-400",
      error: "bg-red-500",
    };
    return map[status] || "bg-slate-300";
  };

  return (
    <aside className="w-64 h-screen flex flex-col border-r bg-white">
      {/* Brand header */}
      <div className="p-4 border-b">
        <div className="flex items-center gap-2.5 mb-3">
          <div className="w-8 h-8 rounded-lg bg-slate-900 flex items-center justify-center">
            <Workflow className="w-4 h-4 text-white" />
          </div>
          <h1 className="text-base font-semibold font-display tracking-wide text-slate-900">MultiAgent Studio</h1>
        </div>
        <button
          onClick={createThread}
          className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-slate-900 text-white rounded-lg hover:bg-hermes-600 transition-all duration-200 text-sm font-medium shadow-sm active:scale-[0.98]"
        >
          <Plus className="w-4 h-4" />
          新建会话
        </button>
      </div>

      {/* Navigation links */}
      <div className="px-3 py-2 space-y-0.5">
        <button onClick={() => router.push("/projects")}
          className={cn("w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors",
            pathname.startsWith("/projects") ? "bg-hermes-50 text-hermes-700 font-medium" : "text-slate-600 hover:bg-slate-50")}>
          <FolderKanban className="w-4 h-4 text-slate-400" /> 项目
        </button>
        <button onClick={() => router.push("/agents")}
          className={cn("w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors",
            pathname.startsWith("/agents") ? "bg-hermes-50 text-hermes-700 font-medium" : "text-slate-600 hover:bg-slate-50")}>
          <Bot className="w-4 h-4 text-slate-400" /> Agent
        </button>
        <button onClick={() => router.push("/extensions")}
          className={cn("w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors",
            pathname.startsWith("/extensions") ? "bg-hermes-50 text-hermes-700 font-medium" : "text-slate-600 hover:bg-slate-50")}>
          <Puzzle className="w-4 h-4 text-slate-400" /> 扩展
        </button>
        <button onClick={() => router.push("/scheduled-tasks")}
          className={cn("w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors",
            pathname.startsWith("/scheduled-tasks") ? "bg-hermes-50 text-hermes-700 font-medium" : "text-slate-600 hover:bg-slate-50")}>
          <CalendarClock className="w-4 h-4 text-slate-400" /> 定时任务
          {unreadTotal > 0 && (
            <span className="ml-auto min-w-[18px] h-[18px] px-1 rounded-full bg-red-500 text-white text-[10px] font-medium flex items-center justify-center">
              {unreadTotal > 99 ? "99+" : unreadTotal}
            </span>
          )}
        </button>
        <button onClick={() => router.push("/monitoring")}
          className={cn("w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors",
            pathname.startsWith("/monitoring") ? "bg-hermes-50 text-hermes-700 font-medium" : "text-slate-600 hover:bg-slate-50")}>
          <BarChart3 className="w-4 h-4 text-slate-400" /> 用量监控
        </button>
      </div>

      {/* Search */}
      <div className="px-3 py-2.5">
        <div className="relative">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="搜索会话..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-8 pr-3 py-2 text-xs border border-slate-200 rounded-lg bg-slate-50 input-focus"
          />
        </div>
      </div>

      {/* Thread list */}
      <div className="flex-1 overflow-y-auto px-2">
        {loading ? (
          <div className="flex justify-center py-10">
            <div className="w-5 h-5 border-2 border-slate-400 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-10">
            <MessageSquare className="w-8 h-8 text-slate-300 mx-auto mb-2" />
            <p className="text-muted-foreground text-xs">
              {search ? "无匹配会话" : "暂无会话"}
            </p>
          </div>
        ) : (
          <div className="space-y-0.5">
            {filtered.map((thread) => {
              const isActive = pathname?.includes(thread.id);
              return (
                <div key={thread.id} className="group relative">
                  <button
                    onClick={() => router.push(`/threads/${thread.id}`)}
                    className={cn(
                      "w-full text-left px-3 py-2.5 rounded-lg transition-all duration-150 text-sm",
                      isActive
                        ? "bg-slate-900/5 text-slate-900 font-medium"
                        : "hover:bg-slate-50 text-slate-700"
                    )}
                  >
                    <div className="flex items-center gap-2.5">
                      <MessageSquare className={cn("w-3.5 h-3.5 flex-shrink-0", isActive ? "text-slate-700" : "text-slate-400")} />
                      <span className="truncate flex-1">{thread.title}</span>
                      <span className={cn("w-2 h-2 rounded-full flex-shrink-0", statusBadge(thread.status))} />
                    </div>
                    <p className="text-[10px] text-slate-400 mt-1 ml-6">
                      {formatDateTime(thread.updatedAt)}
                    </p>
                  </button>
                  {confirmDeleteId === thread.id ? (
                    <button
                      onClick={(e) => handleDeleteClick(thread.id, e)}
                      className="absolute right-2 top-2.5 px-2 py-1 rounded text-[10px] font-medium bg-red-500 text-white hover:bg-red-600 transition-all z-10"
                    >
                      <AlertTriangle className="w-3 h-3 inline mr-0.5" />
                      确认?
                    </button>
                  ) : (
                    <button
                      onClick={(e) => handleDeleteClick(thread.id, e)}
                      className="absolute right-2 top-2.5 p-1 rounded-md opacity-0 group-hover:opacity-100 hover:bg-red-50 hover:text-red-500 text-slate-400 transition-all"
                      title="删除会话"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Bottom */}
      <div className="p-3 border-t bg-slate-50/50">
        <button
          onClick={() => router.push("/settings")}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-slate-600 hover:text-slate-900 rounded-lg hover:bg-white transition-colors"
        >
          <Settings className="w-4 h-4" />
          设置
        </button>
      </div>
    </aside>
  );
}
