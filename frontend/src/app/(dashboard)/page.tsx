"use client";

import { useRouter } from "next/navigation";
import { MessageSquare, BarChart3, ArrowRight } from "lucide-react";

const cards = [
  {
    icon: MessageSquare,
    title: "对话式编排",
    desc: "通过自然语言描述需求，AI 自动生成多 Agent 协作流程",
    color: "bg-blue-50 text-blue-600",
    href: null,
  },
  {
    icon: BarChart3,
    title: "实时监控",
    desc: "追踪 token 消耗、任务状态与 Agent 间消息流转",
    color: "bg-amber-50 text-amber-600",
    href: null,
  },
];

export default function DashboardPage() {
  const router = useRouter();

  return (
    <div className="flex items-center justify-center h-full bg-gradient-to-b from-slate-50 to-white">
      <div className="text-center max-w-lg px-6 animate-fade-in-up">
        {/* Decorative icon */}
        <div className="relative mb-6 mx-auto w-20 h-20">
          <div className="absolute inset-0 bg-slate-900 rounded-2xl rotate-6 opacity-10" />
          <div className="absolute inset-0 bg-slate-900 rounded-2xl -rotate-6 opacity-10" />
          <div className="relative w-20 h-20 rounded-2xl bg-gradient-to-br from-slate-700 to-slate-900 shadow-lg flex items-center justify-center">
            <svg className="w-10 h-10 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456z" />
            </svg>
          </div>
        </div>

        <h2 className="text-2xl font-bold text-slate-900 mb-2">多 Agent 协作工作台</h2>
        <p className="text-muted-foreground text-sm mb-8">
          灵活编排多智能体协作流程，拖拽即用，对话即建
        </p>

        {/* Feature cards */}
        <div className="grid grid-cols-2 gap-3 mb-8">
          {cards.map((card) => (
            <div
              key={card.title}
              className="card-hover bg-white border rounded-xl p-4 text-left cursor-pointer group"
            >
              <div className={`w-9 h-9 rounded-lg ${card.color} flex items-center justify-center mb-3`}>
                <card.icon className="w-5 h-5" />
              </div>
              <h3 className="text-sm font-semibold text-slate-900 mb-1">{card.title}</h3>
              <p className="text-[11px] text-slate-500 leading-relaxed">{card.desc}</p>
            </div>
          ))}
        </div>

        {/* Quick action */}
        <button
          onClick={() => {
            const sidebarBtn = document.querySelector('[class*="新建会话"]') as HTMLElement;
            sidebarBtn?.click();
          }}
          className="inline-flex items-center gap-2 px-5 py-2.5 bg-slate-900 text-white rounded-lg hover:bg-slate-800 transition-all duration-200 text-sm font-medium shadow-sm hover:shadow-md active:scale-[0.98]"
        >
          开始新会话
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
