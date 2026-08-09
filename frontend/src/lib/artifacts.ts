/** present_files 产物清单解析 — 供 chat-store (SSE 自动展开) 与文件卡片/预览面板共用 */

/** 从 tool_args 解析 present_files 的文件清单 (兼容字符串化 JSON) */
export function parsePresentedFilepaths(toolArgs: unknown): string[] {
  let args = toolArgs;
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      return [];
    }
  }
  if (!args || typeof args !== "object") return [];
  const fps = (args as Record<string, unknown>).filepaths;
  if (!Array.isArray(fps)) return [];
  return fps.filter((p): p is string => typeof p === "string" && p.length > 0);
}
