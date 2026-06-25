"use client";

import { useRouter } from "next/navigation";
import { LogOut, User, Settings } from "lucide-react";
import { useAuthStore } from "@/lib/auth-store";

export default function TopBar() {
  const { user, logout } = useAuthStore();
  const router = useRouter();

  return (
    <header className="h-12 border-b flex items-center justify-between px-4 bg-card flex-shrink-0">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-muted-foreground">
          {user?.displayName || user?.username || "未登录"}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={() => router.push("/settings")}
          className="p-1.5 rounded-md hover:bg-accent transition"
          title="设置"
        >
          <Settings className="w-4 h-4 text-muted-foreground" />
        </button>
        <button
          onClick={() => {
            logout();
            router.push("/login");
          }}
          className="p-1.5 rounded-md hover:bg-accent transition"
          title="退出登录"
        >
          <LogOut className="w-4 h-4 text-muted-foreground" />
        </button>
      </div>
    </header>
  );
}
