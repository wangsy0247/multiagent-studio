"use client";

import { useRouter } from "next/navigation";
import Sidebar from "@/components/layout/Sidebar";
import TopBar from "@/components/layout/TopBar";

export default function HomePage() {
  const router = useRouter();

  return (
    <div className="flex h-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col">
        <TopBar />
        <main className="flex-1 flex items-center justify-center bg-muted/20">
          <div className="text-center">
            <h2 className="text-2xl font-semibold text-foreground mb-2">
              多Agent协作工作台
            </h2>
            <p className="text-muted-foreground mb-6">
              从左侧创建或选择一个会话开始
            </p>
            <div className="flex gap-4 justify-center">
              <button
                onClick={() => router.push("/settings")}
                className="px-4 py-2 border rounded-lg text-sm hover:bg-accent transition"
              >
                配置设置
              </button>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
