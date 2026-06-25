"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { Plus, MessageSquare, Search, Archive, Trash2, AlertTriangle } from "lucide-react";
import { threadsAPI } from "@/lib/api-client";
import { useChatStore } from "@/lib/chat-store";
import { ThreadSummary } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";

export default function Sidebar() {
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const router = useRouter();
  const pathname = usePathname();

  // 订阅 chat-store 中的 title 更新
  const chatTitle = useChatStore((s) => s.title);
  const chatActiveThreadId = useChatStore((s) => s.activeThreadId);

  useEffect(() => {
    loadThreads();
  }, []);

  // Sidebar 标题随 SSE title_update 实时更新
  useEffect(() => {
    if (chatTitle && chatTitle !== "新会话" && chatActiveThreadId) {
      setThreads((prev) =>
        prev.map((t) =>
          t.id === chatActiveThreadId ? { ...t, title: chatTitle } : t
        )
      );
    }
  }, [chatTitle, chatActiveThreadId]);

  // 清除删除确认 (点击其他地方或超时)
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
      // snake_case → camelCase 映射
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
      // 二次点击确认删除
      try {
        await threadsAPI.delete(threadId);
        setThreads((prev) => prev.filter((t) => t.id !== threadId));
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
      idle: "bg-gray-200",
      running: "bg-green-500 animate-pulse",
      finished: "bg-blue-400",
      error: "bg-red-500",
    };
    return map[status] || "bg-gray-200";
  };

  return (
    <aside className="w-64 h-screen flex flex-col border-r bg-card">
      {/* 头部 */}
      <div className="p-4 border-b">
        <h1 className="text-lg font-semibold">MultiAgent Studio</h1>
        <button
          onClick={createThread}
          className="mt-3 w-full flex items-center justify-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition text-sm font-medium"
        >
          <Plus className="w-4 h-4" />
          新建会话
        </button>
      </div>

      {/* 搜索 */}
      <div className="px-3 py-2">
        <div className="relative">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="搜索会话..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-9 pr-3 py-2 text-sm border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-primary/20"
          />
        </div>
      </div>

      {/* 会话列表 */}
      <div className="flex-1 overflow-y-auto px-2">
        {loading ? (
          <div className="flex justify-center py-8">
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground text-sm">
            {search ? "无匹配会话" : "暂无会话，点击上方按钮创建"}
          </div>
        ) : (
          <div className="space-y-1">
            {filtered.map((thread) => (
              <div key={thread.id} className="group relative">
                <button
                  onClick={() => router.push(`/threads/${thread.id}`)}
                  className={cn(
                    "w-full text-left px-3 py-2 rounded-md hover:bg-accent transition text-sm",
                    pathname?.includes(thread.id) && "bg-accent"
                  )}
                >
                  <div className="flex items-center gap-2">
                    <MessageSquare className="w-4 h-4 text-muted-foreground flex-shrink-0" />
                    <span className="truncate flex-1">{thread.title}</span>
                    <span className={cn("w-2 h-2 rounded-full flex-shrink-0", statusBadge(thread.status))} />
                  </div>
                  <p className="text-xs text-muted-foreground mt-1 ml-6">
                    {formatDateTime(thread.updatedAt)}
                  </p>
                </button>
                {confirmDeleteId === thread.id ? (
                  <button
                    onClick={(e) => handleDeleteClick(thread.id, e)}
                    className="absolute right-2 top-2 px-2 py-1 rounded text-xs font-medium bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-all z-10"
                  >
                    <AlertTriangle className="w-3 h-3 inline mr-1" />
                    确认?
                  </button>
                ) : (
                  <button
                    onClick={(e) => handleDeleteClick(thread.id, e)}
                    className="absolute right-2 top-2 p-1 rounded opacity-0 group-hover:opacity-100 hover:bg-destructive/10 hover:text-destructive text-muted-foreground transition-all"
                    title="归档会话"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 底部 */}
      <div className="p-3 border-t">
        <button
          onClick={() => router.push("/settings")}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground hover:text-foreground rounded-md hover:bg-accent transition"
        >
          <Archive className="w-4 h-4" />
          设置
        </button>
      </div>
    </aside>
  );
}
