"use client";

/**
 * Artifact 预览面板 (Phase 6) — 右侧栏, 与 SubagentDetailPanel 互斥。
 *
 * 数据源: 从消息内 present_files tool_call 收集产物清单 (与 ArtifactFileList
 * 文件卡片同源, 不依赖 SSE artifacts 事件, 也无需额外请求 listOutputs)。
 *
 * 第一版能力 (spec 分级):
 * - 文本/代码: 只读语法高亮 (复用共享 prism-light 配置)
 * - Markdown: 渲染预览 (复用 MessageItem 的 markdownComponents) + 源码/预览切换
 * - 图片 (png/jpg/jpeg/gif/webp/bmp): <img> 直指 outputs URL
 * - SVG: 后端强制 attachment, 不内联, 显示下载提示
 * - PDF: iframe 内联预览 (后端 inline + Range, 成本仅一个 iframe)
 * - 其他类型: "暂不支持预览" + 下载 fallback
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  Code2,
  Download,
  Eye,
  FileWarning,
  Loader2,
  Paperclip,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { useChatStore } from "@/lib/chat-store";
import { filesAPI, authFetch, fetchFileObjectUrl, downloadWithAuth } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import { parsePresentedFilepaths } from "./ArtifactFileList";
import { markdownComponents } from "./MessageItem";
import { SyntaxHighlighter, oneDark, REGISTERED_LANGUAGES } from "./syntax-highlighter";

// ── 文件类型分级 ────────────────────────────────────────────────────────
type FileKind = "markdown" | "code" | "text" | "image" | "svg" | "pdf" | "unsupported";

// svg 故意不在内: 后端对 image/svg+xml 强制 attachment, 前端不内联 (XSS 风险)
const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp"]);

/** 扩展名 → 已注册的 prism 语言 (未注册语言 prism-light 会告警, 必须映射) */
const CODE_LANGS: Record<string, string> = {
  py: "python", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
  json: "json", sh: "bash", bash: "bash", sql: "sql", css: "css",
  yaml: "yaml", yml: "yaml", java: "java", go: "go", rs: "rust",
  c: "c", h: "c", cpp: "cpp",
};

/** 无高亮纯文本 (html/xml 后端强制下载或按文本内联, 仅展示源码) */
const TEXT_EXTS = new Set(["txt", "log", "csv", "tsv", "ini", "cfg", "conf", "xml", "html", "htm"]);

function classify(path: string): { kind: FileKind; language?: string } {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  if (ext === "svg") return { kind: "svg" };
  if (IMAGE_EXTS.has(ext)) return { kind: "image" };
  if (ext === "md" || ext === "markdown") return { kind: "markdown" };
  if (ext === "pdf") return { kind: "pdf" };
  if (CODE_LANGS[ext]) return { kind: "code", language: CODE_LANGS[ext] };
  if (TEXT_EXTS.has(ext)) return { kind: "text" };
  return { kind: "unsupported" };
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).pop() || path;
}

/** 超大文本截断阈值, 避免一次渲染卡死面板 */
const MAX_TEXT_CHARS = 500_000;

export default function ArtifactPreviewPanel() {
  const selectedArtifactPath = useChatStore((s) => s.selectedArtifactPath);
  const selectArtifact = useChatStore((s) => s.selectArtifact);
  const activeThreadId = useChatStore((s) => s.activeThreadId);
  const messages = useChatStore((s) => s.messages);

  // 产物清单: 消息内 present_files tool_call 收集 (去重保序)
  const artifactPaths = useMemo(() => {
    const out: string[] = [];
    const seen = new Set<string>();
    for (const m of messages) {
      const meta = m.metadata as Record<string, unknown> | undefined;
      if (meta?.tool_name !== "present_files") continue;
      for (const p of parsePresentedFilepaths(meta.tool_args)) {
        if (!seen.has(p)) {
          seen.add(p);
          out.push(p);
        }
      }
    }
    return out;
  }, [messages]);

  // 下拉选项: 选中项不在清单里时兜底插入 (例如清单尚未到达)
  const options = useMemo(() => {
    if (selectedArtifactPath && !artifactPaths.includes(selectedArtifactPath)) {
      return [selectedArtifactPath, ...artifactPaths];
    }
    return artifactPaths;
  }, [artifactPaths, selectedArtifactPath]);

  const { kind, language } = useMemo<{ kind: FileKind; language?: string }>(
    () => (selectedArtifactPath ? classify(selectedArtifactPath) : { kind: "unsupported" }),
    [selectedArtifactPath],
  );

  const needsText = kind === "markdown" || kind === "code" || kind === "text";
  const previewUrl =
    activeThreadId && selectedArtifactPath
      ? filesAPI.outputsUrl(activeThreadId, selectedArtifactPath)
      : null;
  const downloadUrl =
    activeThreadId && selectedArtifactPath
      ? filesAPI.outputsUrl(activeThreadId, selectedArtifactPath, true)
      : null;

  const [content, setContent] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"preview" | "source">("preview");
  // 图片/PDF: 带鉴权拉 blob 转 objectURL (<img>/<iframe> 无法自定义请求头)
  const [mediaUrl, setMediaUrl] = useState<string | null>(null);

  // 切换文件时重置视图模式
  useEffect(() => {
    setViewMode("preview");
  }, [selectedArtifactPath]);

  // 拉取文本内容 (markdown/code/text 分支) — authFetch 携带 JWT, 裸 fetch 会 401
  useEffect(() => {
    if (!needsText || !previewUrl) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    setContent(null);
    setTruncated(false);
    authFetch(previewUrl)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((t) => {
        if (cancelled) return;
        if (t.length > MAX_TEXT_CHARS) {
          setContent(t.slice(0, MAX_TEXT_CHARS));
          setTruncated(true);
        } else {
          setContent(t);
        }
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : "加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [needsText, previewUrl]);

  // 拉取二进制媒体 (image/pdf 分支) — blob → objectURL
  const needsMedia = kind === "image" || kind === "pdf";
  useEffect(() => {
    if (!needsMedia || !previewUrl) return;
    let cancelled = false;
    let objUrl: string | null = null;
    setLoading(true);
    setLoadError(null);
    setMediaUrl(null);
    fetchFileObjectUrl(previewUrl)
      .then((u) => {
        if (cancelled) {
          URL.revokeObjectURL(u);
          return;
        }
        objUrl = u;
        setMediaUrl(u);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : "加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      if (objUrl) URL.revokeObjectURL(objUrl);
    };
  }, [needsMedia, previewUrl]);

  if (!selectedArtifactPath) return null;

  const onClose = () => selectArtifact(null);

  return (
    <aside className="w-[480px] border-l bg-white flex-shrink-0 flex flex-col overflow-hidden">
      {/* ── Header: 文件切换 + 下载 + 关闭 ── */}
      <div className="flex items-center justify-between gap-2 p-3 border-b bg-slate-50/50 flex-shrink-0">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-sky-100 flex items-center justify-center flex-shrink-0">
            <Paperclip className="w-4 h-4 text-sky-600" />
          </div>
          <div className="min-w-0">
            {options.length > 1 ? (
              <select
                value={selectedArtifactPath}
                onChange={(e) => selectArtifact(e.target.value)}
                className="block max-w-[260px] text-sm font-semibold text-slate-900 bg-transparent border-none focus:outline-none cursor-pointer truncate"
              >
                {options.map((p) => (
                  <option key={p} value={p}>
                    {fileName(p)}
                  </option>
                ))}
              </select>
            ) : (
              <h3 className="text-sm font-semibold text-slate-900 truncate">
                {fileName(selectedArtifactPath)}
              </h3>
            )}
            <p className="text-[10px] text-slate-400 truncate" title={selectedArtifactPath}>
              产物 {options.length} 个 · {selectedArtifactPath}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {downloadUrl && (
            <button
              onClick={() => downloadWithAuth(downloadUrl, fileName(selectedArtifactPath))}
              className="p-1.5 rounded-lg hover:bg-slate-200 transition-colors"
              title="下载"
            >
              <Download className="w-4 h-4 text-slate-500" />
            </button>
          )}
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-slate-200 transition-colors"
            title="关闭"
          >
            <X className="w-4 h-4 text-slate-500" />
          </button>
        </div>
      </div>

      {/* ── Markdown 源码/预览切换 ── */}
      {kind === "markdown" && (
        <div className="flex items-center gap-1 px-3 py-1.5 border-b bg-slate-50/30 flex-shrink-0">
          {(
            [
              { mode: "preview" as const, icon: Eye, label: "预览" },
              { mode: "source" as const, icon: Code2, label: "源码" },
            ]
          ).map(({ mode, icon: Icon, label }) => (
            <button
              key={mode}
              onClick={() => setViewMode(mode)}
              className={cn(
                "flex items-center gap-1 px-2.5 py-1 rounded-md text-[11px] font-medium transition-colors",
                viewMode === mode
                  ? "bg-sky-100 text-sky-700"
                  : "text-slate-500 hover:bg-slate-100",
              )}
            >
              <Icon className="w-3 h-3" />
              {label}
            </button>
          ))}
        </div>
      )}

      {/* ── 内容区 ── */}
      <div className="flex-1 overflow-y-auto min-h-0">
        {loading && (
          <div className="flex items-center justify-center h-full text-slate-400">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        )}

        {!loading && loadError && (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-slate-400 p-6">
            <FileWarning className="w-8 h-8" />
            <p className="text-xs">内容加载失败 ({loadError})</p>
            {downloadUrl && <DownloadLink url={downloadUrl} name={fileName(selectedArtifactPath)} />}
          </div>
        )}

        {!loading && !loadError && kind === "image" && mediaUrl && (
          <div className="flex items-center justify-center p-4 min-h-full bg-slate-50">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={mediaUrl}
              alt={fileName(selectedArtifactPath)}
              className="max-w-full h-auto rounded-lg border border-slate-200 bg-white"
            />
          </div>
        )}

        {!loading && !loadError && kind === "svg" && (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-slate-400 p-6 text-center">
            <FileWarning className="w-8 h-8" />
            <p className="text-xs">
              SVG 可能包含脚本, 出于安全考虑不提供内联预览, 请下载后查看。
            </p>
            {downloadUrl && <DownloadLink url={downloadUrl} name={fileName(selectedArtifactPath)} />}
          </div>
        )}

        {!loading && !loadError && kind === "pdf" && mediaUrl && (
          <iframe src={mediaUrl} title={fileName(selectedArtifactPath)} className="w-full h-full border-0" />
        )}

        {!loading && !loadError && kind === "unsupported" && (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-slate-400 p-6 text-center">
            <FileWarning className="w-8 h-8" />
            <p className="text-xs font-medium text-slate-600">{fileName(selectedArtifactPath)}</p>
            <p className="text-xs">该类型暂不支持预览</p>
            {downloadUrl && <DownloadLink url={downloadUrl} name={fileName(selectedArtifactPath)} />}
          </div>
        )}

        {!loading && !loadError && needsText && content !== null && (
          <>
            {truncated && (
              <p className="px-3 py-1.5 text-[10px] text-amber-600 bg-amber-50 border-b border-amber-100">
                文件过大, 仅展示前 {(MAX_TEXT_CHARS / 1000).toFixed(0)}k 字符
              </p>
            )}
            {kind === "markdown" && viewMode === "preview" ? (
              <div className="p-4 text-sm leading-relaxed prose prose-sm max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={markdownComponents}>
                  {content}
                </ReactMarkdown>
              </div>
            ) : kind === "code" && language && REGISTERED_LANGUAGES.has(language) ? (
              <SyntaxHighlighter
                style={oneDark}
                language={language}
                PreTag="div"
                customStyle={{ margin: 0, borderRadius: 0, fontSize: "12px", minHeight: "100%" }}
                showLineNumbers
              >
                {content}
              </SyntaxHighlighter>
            ) : kind === "markdown" ? (
              /* markdown 源码视图 */
              <SyntaxHighlighter
                style={oneDark}
                language="markdown"
                PreTag="div"
                customStyle={{ margin: 0, borderRadius: 0, fontSize: "12px", minHeight: "100%" }}
                showLineNumbers
              >
                {content}
              </SyntaxHighlighter>
            ) : (
              /* 纯文本 / 未注册语言: 无高亮 pre */
              <pre className="p-4 text-xs font-mono text-slate-700 whitespace-pre-wrap break-all">
                {content}
              </pre>
            )}
          </>
        )}
      </div>
    </aside>
  );
}

function DownloadLink({ url, name }: { url: string; name: string }) {
  return (
    <button
      onClick={() => downloadWithAuth(url, name)}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-sky-600 text-white text-xs font-medium hover:bg-sky-700 transition-colors"
    >
      <Download className="w-3.5 h-3.5" />
      下载文件
    </button>
  );
}
