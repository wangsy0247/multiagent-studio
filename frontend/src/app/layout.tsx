import type { Metadata } from "next";
import "@/styles/globals.css";
import AuthProvider from "@/components/providers/AuthProvider";

export const metadata: Metadata = {
  title: "MultiAgent Studio — 多Agent协作工作台",
  description: "可视化构建、管理和监控多Agent协作流程",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-background antialiased">
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
