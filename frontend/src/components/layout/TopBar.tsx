"use client";

import { useRouter } from "next/navigation";
import { LogOut, User, Settings, ChevronRight } from "lucide-react";
import { useAuthStore } from "@/lib/auth-store";

export default function TopBar() {
  const { user, logout } = useAuthStore();
  const router = useRouter();

  const displayName = user?.displayName || user?.username || "未登录";

  return (
    <header className="h-12 border-b flex items-center justify-between px-4 bg-white flex-shrink-0">
      <div className="flex items-center gap-2">
        <div className="w-7 h-7 rounded-full bg-slate-800 flex items-center justify-center">
          <span className="text-xs font-medium text-white">
            {displayName.charAt(0).toUpperCase()}
          </span>
        </div>
        <div className="flex flex-col leading-none">
          <span className="text-sm font-medium text-slate-900">{displayName}</span>
          {user?.email && (
            <span className="text-[10px] text-slate-400">{user.email}</span>
          )}
        </div>
      </div>

      <div className="flex items-center">
        <button
          onClick={() => router.push("/settings")}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-slate-600 hover:text-slate-900 rounded-md hover:bg-slate-100 transition-colors"
          title="设置"
        >
          <Settings className="w-4 h-4" />
          <span className="hidden sm:inline">设置</span>
        </button>

        <div className="w-px h-5 bg-slate-200 mx-1" />

        <button
          onClick={() => {
            logout();
            router.push("/login");
          }}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-slate-600 hover:text-red-600 rounded-md hover:bg-red-50 transition-colors"
          title="退出登录"
        >
          <LogOut className="w-4 h-4" />
          <span className="hidden sm:inline">退出</span>
        </button>
      </div>
    </header>
  );
}
