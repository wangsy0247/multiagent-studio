"use client";

import { useEffect, createContext, useContext } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuthStore } from "@/lib/auth-store";

const AuthContext = createContext<ReturnType<typeof useAuthStore> | null>(null);

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}

const PUBLIC_PATHS = ["/login", "/register"];

export default function AuthProvider({ children }: { children: React.ReactNode }) {
  const store = useAuthStore();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    // 初始化 — 尝试用本地 token 获取用户信息
    const token = localStorage.getItem("auth-storage");
    if (token) {
      try {
        const { state } = JSON.parse(token);
        if (state?.accessToken) {
          store.fetchMe();
          return;
        }
      } catch {}
    }
    useAuthStore.setState({ isLoading: false });
  }, []);

  useEffect(() => {
    if (store.isLoading) return;

    if (!store.isAuthenticated && !PUBLIC_PATHS.includes(pathname || "")) {
      router.push("/login");
    } else if (store.isAuthenticated && PUBLIC_PATHS.includes(pathname || "")) {
      router.push("/");
    }
  }, [store.isAuthenticated, store.isLoading, pathname, router]);

  // 加载中
  if (store.isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-background">
        <div className="flex flex-col items-center gap-4">
          <div className="w-10 h-10 border-4 border-primary border-t-transparent rounded-full animate-spin" />
          <p className="text-muted-foreground text-sm">加载中...</p>
        </div>
      </div>
    );
  }

  // 公开页面不需要 sidebar
  if (PUBLIC_PATHS.includes(pathname || "")) {
    return <>{children}</>;
  }

  return <AuthContext.Provider value={store}>{children}</AuthContext.Provider>;
}
