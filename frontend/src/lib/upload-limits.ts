// 前端上传预校验常量
// ⚠️ 必须与后端 app/services/file_service.py 保持同步:
//    UPLOAD_MAX_SIZE_MB / ALLOWED_MIME_TYPES / _ALLOWED_EXTENSIONS
// 后端仍是最终权威 (含文本嗅探等兜底), 前端只做快速拦截, 拒绝策略可比后端略严.

export const UPLOAD_MAX_SIZE_MB = 50;
export const UPLOAD_MAX_SIZE_BYTES = UPLOAD_MAX_SIZE_MB * 1024 * 1024;

const ALLOWED_MIME_TYPES = new Set([
  "text/plain", "text/csv", "text/markdown", "text/html",
  "application/json", "application/xml", "application/yaml",
  "application/pdf",
  "image/png", "image/jpeg", "image/gif", "image/svg+xml",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", // xlsx
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", // docx
  "application/zip", "application/gzip",
]);

const ALLOWED_EXTENSIONS = new Set([
  ".txt", ".md", ".markdown", ".csv", ".tsv", ".html", ".htm", ".xml",
  ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".log",
  ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
  ".css", ".scss", ".sass", ".less", ".sql", ".sh", ".bash", ".zsh",
  ".c", ".cpp", ".cc", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php",
  ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
  ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt",
  ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
]);

function getExtension(filename: string): string {
  const idx = filename.lastIndexOf(".");
  return idx >= 0 ? filename.slice(idx).toLowerCase() : "";
}

/**
 * 本地预校验上传文件, 返回拒绝原因; 通过时返回 null.
 * 与后端 _is_allowed_file 同口径: MIME 白名单优先, 其次扩展名白名单.
 */
export function validateUploadFile(file: { name: string; type: string; size: number }): string | null {
  if (file.size > UPLOAD_MAX_SIZE_BYTES) {
    return `文件过大 (最大 ${UPLOAD_MAX_SIZE_MB}MB)`;
  }
  if (file.type && ALLOWED_MIME_TYPES.has(file.type)) {
    return null;
  }
  if (ALLOWED_EXTENSIONS.has(getExtension(file.name))) {
    return null;
  }
  return "不支持的文件类型";
}
