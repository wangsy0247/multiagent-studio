"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Eye, EyeOff, UserPlus, AlertCircle } from "lucide-react";
import { useAuthStore } from "@/lib/auth-store";

export default function RegisterPage() {
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const { register, isLoading, error, clearError } = useAuthStore();
  const router = useRouter();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    try {
      await register(email, username, password);
      router.push("/");
    } catch {}
  }

  return (
    <div className="min-h-screen flex items-center justify-center relative overflow-hidden bg-gradient-to-br from-slate-50 via-white to-blue-50/50">
      {/* Decorative blurs */}
      <div className="absolute top-0 -right-40 w-96 h-96 bg-blue-100/40 rounded-full blur-3xl" />
      <div className="absolute -bottom-40 -left-40 w-96 h-96 bg-slate-100/40 rounded-full blur-3xl" />

      <div className="w-full max-w-sm relative z-10 animate-fade-in-up">
        {/* Brand header */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br from-blue-500 to-blue-600 shadow-lg mb-4">
            <UserPlus className="w-7 h-7 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-slate-900">创建账号</h1>
          <p className="text-muted-foreground text-sm mt-1.5">注册 MultiAgent Studio</p>
        </div>

        <div className="bg-card border rounded-xl shadow-sm p-8">
          {error && (
            <div className="mb-4 p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive flex items-start gap-2 animate-scale-in">
              <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
              <span className="flex-1">{error}</span>
              <button onClick={clearError} className="text-destructive/70 hover:text-destructive shrink-0">&times;</button>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">邮箱</label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="w-full px-3.5 py-2.5 border border-slate-200 rounded-lg text-sm input-focus"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">用户名</label>
              <input
                type="text"
                required
                minLength={3}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="你的名字"
                className="w-full px-3.5 py-2.5 border border-slate-200 rounded-lg text-sm input-focus"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1.5">密码</label>
              <div className="relative">
                <input
                  type={showPassword ? "text" : "password"}
                  required
                  minLength={6}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="至少 6 位"
                  className="w-full px-3.5 py-2.5 pr-10 border border-slate-200 rounded-lg text-sm input-focus"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                >
                  {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>
            <button
              type="submit"
              disabled={isLoading}
              className="w-full py-2.5 bg-slate-900 text-white rounded-lg hover:bg-slate-800 transition-all duration-200 disabled:opacity-50 font-medium text-sm shadow-sm hover:shadow-md active:scale-[0.98]"
            >
              {isLoading ? "注册中..." : "注册"}
            </button>
          </form>

          <p className="text-center text-sm text-muted-foreground mt-6">
            已有账号？{" "}
            <Link href="/login" className="text-slate-900 font-medium hover:underline">
              登录
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
