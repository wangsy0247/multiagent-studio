"use client";

import { useEffect } from "react";
import Sidebar from "@/components/layout/Sidebar";
import TopBar from "@/components/layout/TopBar";
import { useAuthStore } from "@/lib/auth-store";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const fetchMe = useAuthStore((s) => s.fetchMe);

  // 页面加载时：从 localStorage 恢复 token 和 user，确保 auth 状态同步
  useEffect(() => {
    // Zustand 在页面刷新后会丢失状态，需要从 localStorage 恢复
    const stored = localStorage.getItem("auth-storage");
    if (!stored) return;
    try {
      const { state } = JSON.parse(stored);
      const storedToken = state?.accessToken;
      const storedUser = state?.user;
      if (storedToken && !storedUser) {
        // 有 token 但没有 user → 调用 fetchMe 获取用户信息
        fetchMe().catch(() => {});
      }
    } catch {}
  }, []); // 仅在挂载时执行一次

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar />
        <main className="flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}
