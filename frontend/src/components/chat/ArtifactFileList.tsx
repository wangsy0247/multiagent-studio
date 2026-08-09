"use client";

import React from "react";
import {
  Download,
  File,
  FileArchive,
  FileCode,
  FileImage,
  FileSpreadsheet,
  FileText,
  Paperclip,
} from "lucide-react";
import { ChatMessage } from "@/lib/types";
import { filesAPI, downloadWithAuth } from "@/lib/api-client";
import { useChatStore } from "@/lib/chat-store";
import { parsePresentedFilepaths } from "@/lib/artifacts";

/** 按扩展名选图标 (保持简单, 与现有卡片风格一致) */
function fileIcon(path: string) {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  const cls = "w-4 h-4";
  if (["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp"].includes(ext))
    return <FileImage className={cls} />;
  if (["zip", "gz", "tgz", "tar", "7z", "rar", "bz2", "xz"].includes(ext))
    return <FileArchive className={cls} />;
  if (["csv", "tsv", "xlsx", "xls"].includes(ext))
    return <FileSpreadsheet className={cls} />;
  if (
    ["py", "js", "jsx", "ts", "tsx", "json", "sh", "sql", "c", "cpp", "h",
     "java", "go", "rs", "rb", "php", "yaml", "yml", "toml", "xml", "html", "css"].includes(ext)
  )
    return <FileCode className={cls} />;
  if (["md", "markdown", "txt", "log", "ini", "cfg", "conf"].includes(ext))
    return <FileText className={cls} />;
  return <File className={cls} />;
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).pop() || path;
}

interface ArtifactFileListProps {
  message: ChatMessage;
}

/** present_files 工具调用的产物文件卡片 */
const ArtifactFileList = React.memo(function ArtifactFileList({ message }: ArtifactFileListProps) {
  const threadId = useChatStore((s) => s.activeThreadId);
  const selectArtifact = useChatStore((s) => s.selectArtifact);
  const files = parsePresentedFilepaths(message.metadata?.tool_args);

  return (
    <div className="flex gap-3 animate-fade-in-up">
      <div className="w-8 h-8 rounded-full bg-sky-100 flex items-center justify-center flex-shrink-0 shadow-sm">
        <Paperclip className="w-4 h-4 text-sky-600" />
      </div>
      <div className="max-w-[80%] min-w-[240px]">
        <p className="text-xs font-medium text-slate-500 mb-1.5">
          交付文件 ({files.length})
        </p>
        <ul className="flex flex-col gap-1.5">
          {files.map((fp) => {
            const name = fileName(fp);
            const url = threadId ? filesAPI.outputsUrl(threadId, fp, true) : null;
            return (
              <li
                key={fp}
                onClick={() => selectArtifact(fp)}
                className="flex items-center gap-2.5 px-3 py-2 rounded-xl border border-slate-200 bg-white hover:border-sky-300 hover:bg-sky-50/40 transition-colors cursor-pointer"
                title="点击预览"
              >
                <span className="text-slate-500 flex-shrink-0">{fileIcon(fp)}</span>
                <span className="min-w-0 flex-1">
                  <span className="block text-xs font-medium text-slate-700 truncate" title={fp}>
                    {name}
                  </span>
                  <span className="block text-[10px] text-slate-400 truncate">{fp}</span>
                </span>
                {url && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      downloadWithAuth(url, name);
                    }}
                    className="flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] text-sky-600 hover:bg-sky-50 flex-shrink-0"
                    title="下载"
                  >
                    <Download className="w-3.5 h-3.5" />
                    下载
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
});

export default ArtifactFileList;
