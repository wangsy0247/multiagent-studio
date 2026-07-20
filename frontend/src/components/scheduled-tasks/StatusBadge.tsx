import { cn } from "@/lib/utils";

const STATUS_MAP: Record<string, { label: string; cls: string }> = {
  success: { label: "成功", cls: "bg-green-50 text-green-600 border-green-200" },
  error: { label: "失败", cls: "bg-red-50 text-red-600 border-red-200" },
  timeout: { label: "超时", cls: "bg-amber-50 text-amber-600 border-amber-200" },
  skipped: { label: "已跳过", cls: "bg-slate-50 text-slate-500 border-slate-200" },
  expired: { label: "已过期", cls: "bg-slate-50 text-slate-500 border-slate-200" },
  interrupted: { label: "已中断", cls: "bg-slate-50 text-slate-500 border-slate-200" },
  running: { label: "运行中", cls: "bg-blue-50 text-blue-600 border-blue-200" },
};

export default function StatusBadge({ status }: { status: string | null }) {
  if (!status) return <span className="text-slate-300 text-xs">—</span>;
  const s = STATUS_MAP[status] || { label: status, cls: "bg-slate-50 text-slate-500 border-slate-200" };
  return (
    <span className={cn("inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium border", s.cls)}>
      {status === "running" && (
        <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse mr-1" />
      )}
      {s.label}
    </span>
  );
}
