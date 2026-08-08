"use client";

import { useEffect, useState } from "react";
import {
  Puzzle, Plus, Trash2, Pencil, X, Server, BookOpen, Link2, RefreshCw,
  ChevronDown, ChevronRight,
} from "lucide-react";
import {
  extensionsAPI, McpServerConfig, SkillSummary,
} from "@/lib/api-client";
import { cn } from "@/lib/utils";

type ExtTab = "mcp" | "skills";

interface McpEntry extends McpServerConfig {
  name: string;
}

interface AgentSkillRecord {
  name: string;
  description: string;
  state: string;  // probation | active
  success_uses: number;
  fail_uses: number;
}

interface AgentSkillGroup {
  agent: string;
  display_name: string;
  skills: AgentSkillRecord[];
}

// ── key-value 行编辑器 (env / headers) ─────────────────────────────────
function KvEditor({
  value, onChange, keyPlaceholder, valuePlaceholder,
}: {
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  keyPlaceholder: string;
  valuePlaceholder: string;
}) {
  const rows = Object.entries(value);
  return (
    <div className="space-y-1.5">
      {rows.map(([k, v], i) => (
        <div key={i} className="flex gap-2">
          <input
            value={k}
            onChange={(e) => {
              const entries = rows.map(([ok, ov], j) =>
                j === i ? [e.target.value, ov] : [ok, ov]
              );
              onChange(Object.fromEntries(entries as [string, string][]));
            }}
            placeholder={keyPlaceholder}
            className="flex-1 px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg font-mono input-focus"
          />
          <input
            value={v}
            onChange={(e) => {
              onChange({ ...value, [k]: e.target.value });
            }}
            placeholder={valuePlaceholder}
            className="flex-1 px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg font-mono input-focus"
          />
          <button
            onClick={() => {
              const next = { ...value };
              delete next[k];
              onChange(next);
            }}
            className="p-1.5 text-slate-400 hover:text-red-500"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ))}
      <button
        onClick={() => onChange({ ...value, "": "" })}
        className="text-xs text-hermes-600 hover:text-hermes-700"
      >
        + 添加一行
      </button>
    </div>
  );
}

// ── 开关 ────────────────────────────────────────────────────────────────
function Toggle({ checked, onChange, disabled }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean;
}) {
  return (
    <button
      onClick={() => !disabled && onChange(!checked)}
      className={cn(
        "w-8 h-[18px] rounded-full transition-colors relative shrink-0",
        checked ? "bg-hermes-500" : "bg-slate-300",
        disabled && "opacity-40 cursor-not-allowed"
      )}
    >
      <span className={cn(
        "absolute top-[2px] w-3.5 h-3.5 bg-white rounded-full shadow transition-all",
        checked ? "left-[16px]" : "left-[2px]"
      )} />
    </button>
  );
}

// ── 技能行 (内置/我的共用) ────────────────────────────────────────────
function SkillRow({ s, onToggle, onEdit, onDelete }: {
  s: SkillSummary;
  onToggle: (name: string, enabled: boolean) => void;
  onEdit?: (name: string) => void;
  onDelete?: (name: string) => void;
}) {
  return (
    <div className="flex items-center gap-3 p-4 border border-slate-200 rounded-xl">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-slate-900">{s.name}</span>
          <span className={cn(
            "text-[10px] px-1.5 py-0.5 rounded",
            s.user_id ? "bg-hermes-50 text-hermes-600" : "bg-slate-100 text-slate-500"
          )}>
            {s.user_id ? "我的" : "内置"}
          </span>
        </div>
        <p className="text-xs text-slate-500 mt-1 line-clamp-2">{s.description}</p>
      </div>
      <Toggle checked={s.enabled} onChange={(v) => onToggle(s.name, v)} />
      {onEdit && (
        <button onClick={() => onEdit(s.name)} className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100">
          <Pencil className="w-4 h-4" />
        </button>
      )}
      {onDelete && (
        <button onClick={() => onDelete(s.name)} className="p-1.5 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50">
          <Trash2 className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}

export default function ExtensionsPage() {
  const [tab, setTab] = useState<ExtTab>("mcp");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // ── MCP ──
  const [servers, setServers] = useState<McpEntry[]>([]);
  const [mcpDialog, setMcpDialog] = useState<false | "new" | McpEntry>(false);
  // ── Skills ──
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [agentSkillGroups, setAgentSkillGroups] = useState<AgentSkillGroup[]>([]);
  const [collapsedAgents, setCollapsedAgents] = useState<Set<string>>(new Set());
  const [skillDialog, setSkillDialog] = useState<false | "new" | { name: string }>(false);
  const [urlDialog, setUrlDialog] = useState(false);

  useEffect(() => { loadAll(); }, []);

  async function loadAll() {
    setLoading(true);
    setError("");
    try {
      const [mcpResp, skillResp, agentSkillResp] = await Promise.all([
        extensionsAPI.listMcpServers(),
        extensionsAPI.listSkills(),
        extensionsAPI.listAgentSkills(),
      ]);
      const srv = mcpResp.data.servers || {};
      setServers(
        Object.entries(srv).map(([name, cfg]) => ({
          name, ...(cfg as McpServerConfig),
        }))
      );
      setSkills(skillResp.data.skills || []);
      setAgentSkillGroups(agentSkillResp.data.agents || []);
    } catch (err) {
      setError("加载失败 — 请确认服务已启动");
    } finally {
      setLoading(false);
    }
  }

  async function toggleMcp(name: string, enabled: boolean) {
    setServers((prev) => prev.map((s) => (s.name === name ? { ...s, enabled } : s)));
    try {
      await extensionsAPI.setMcpServerEnabled(name, enabled);
    } catch {
      setServers((prev) => prev.map((s) => (s.name === name ? { ...s, enabled: !enabled } : s)));
    }
  }

  async function deleteMcp(name: string) {
    if (!confirm(`确定删除 MCP 服务「${name}」吗?`)) return;
    try {
      await extensionsAPI.deleteMcpServer(name);
      setServers((prev) => prev.filter((s) => s.name !== name));
    } catch {
      alert("删除失败");
    }
  }

  async function toggleSkill(name: string, enabled: boolean) {
    setSkills((prev) => prev.map((s) => (s.name === name ? { ...s, enabled } : s)));
    try {
      await extensionsAPI.toggleSkill(name, enabled);
    } catch {
      setSkills((prev) => prev.map((s) => (s.name === name ? { ...s, enabled: !enabled } : s)));
    }
  }

  async function deleteSkill(name: string) {
    if (!confirm(`确定删除技能「${name}」吗?`)) return;
    try {
      await extensionsAPI.deleteCustomSkill(name);
      setSkills((prev) => prev.filter((s) => s.name !== name));
    } catch {
      alert("删除失败");
    }
  }

  const tabItems: { id: ExtTab; label: string; icon: typeof Server }[] = [
    { id: "mcp", label: "MCP 服务", icon: Server },
    { id: "skills", label: "技能", icon: BookOpen },
  ];

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-3xl mx-auto p-8">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xl font-bold font-display text-slate-900">扩展</h2>
          <button onClick={loadAll} className="p-2 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100">
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
        <p className="text-xs text-slate-400 mb-6">
          MCP 服务为全局配置, 保存即热生效; 技能默认对所有 Agent 可用, 可在 Agent 编辑页按 Agent 裁剪
        </p>

        {error && (
          <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-600">{error}</div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 border-b border-slate-200 mb-6">
          {tabItems.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={cn(
                "flex items-center gap-2 px-4 py-2.5 text-sm transition-all border-b-2 -mb-px",
                tab === t.id
                  ? "border-slate-900 text-slate-900 font-medium"
                  : "border-transparent text-slate-500 hover:text-slate-700"
              )}
            >
              <t.icon className="w-4 h-4" /> {t.label}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="flex justify-center py-16">
            <div className="w-6 h-6 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
          </div>
        ) : tab === "mcp" ? (
          /* ═══ MCP 服务 ═══ */
          <div className="space-y-3 animate-fade-in">
            {servers.length === 0 && (
              <p className="text-sm text-slate-400 py-8 text-center">暂无 MCP 服务</p>
            )}
            {servers.map((s) => (
              <div key={s.name} className="flex items-center gap-3 p-4 border border-slate-200 rounded-xl">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-slate-900 font-mono">{s.name}</span>
                    <span className="text-[10px] px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded">{s.type}</span>
                  </div>
                  <p className="text-xs text-slate-500 mt-1 truncate">
                    {s.description || (s.type === "stdio" ? `${s.command} ${(s.args || []).join(" ")}` : s.url)}
                  </p>
                </div>
                <Toggle checked={s.enabled} onChange={(v) => toggleMcp(s.name, v)} />
                <button onClick={() => setMcpDialog(s)} className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100">
                  <Pencil className="w-4 h-4" />
                </button>
                <button onClick={() => deleteMcp(s.name)} className="p-1.5 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50">
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
            <button
              onClick={() => setMcpDialog("new")}
              className="w-full flex items-center justify-center gap-2 p-3 border border-dashed border-slate-300 rounded-xl text-sm text-slate-500 hover:border-hermes-400 hover:text-hermes-600 transition-colors"
            >
              <Plus className="w-4 h-4" /> 添加 MCP 服务
            </button>
          </div>
        ) : (
          /* ═══ 技能 ═══ */
          <div className="space-y-5 animate-fade-in">
            {skills.length === 0 && agentSkillGroups.length === 0 && (
              <p className="text-sm text-slate-400 py-8 text-center">暂无技能</p>
            )}

            {/* ── 内置 ── */}
            {skills.filter((s) => !s.user_id).length > 0 && (
              <section>
                <h3 className="text-xs font-medium text-slate-400 mb-2">内置 ({skills.filter((s) => !s.user_id).length})</h3>
                <div className="space-y-3">
                  {skills.filter((s) => !s.user_id).map((s) => (
                    <SkillRow key={`builtin-${s.name}`} s={s}
                      onToggle={toggleSkill} />
                  ))}
                </div>
              </section>
            )}

            {/* ── 我的 ── */}
            {skills.filter((s) => s.user_id).length > 0 && (
              <section>
                <h3 className="text-xs font-medium text-slate-400 mb-2">我的 ({skills.filter((s) => s.user_id).length})</h3>
                <div className="space-y-3">
                  {skills.filter((s) => s.user_id).map((s) => (
                    <SkillRow key={`mine-${s.name}`} s={s}
                      onToggle={toggleSkill}
                      onEdit={(n) => setSkillDialog({ name: n })}
                      onDelete={deleteSkill} />
                  ))}
                </div>
              </section>
            )}

            {/* ── 按 Agent (成员私有进化技能, 只读) ── */}
            {agentSkillGroups.length > 0 && (
              <section>
                <h3 className="text-xs font-medium text-slate-400 mb-2">按 Agent ({agentSkillGroups.length})</h3>
                <div className="space-y-2">
                  {agentSkillGroups.map((g) => {
                    const collapsed = collapsedAgents.has(g.agent);
                    return (
                      <div key={g.agent} className="border border-slate-200 rounded-xl overflow-hidden">
                        <button
                          onClick={() => {
                            const next = new Set(collapsedAgents);
                            if (collapsed) next.delete(g.agent);
                            else next.add(g.agent);
                            setCollapsedAgents(next);
                          }}
                          className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-slate-700 hover:bg-slate-50 transition-colors"
                        >
                          {collapsed ? <ChevronRight className="w-3.5 h-3.5 text-slate-400" /> : <ChevronDown className="w-3.5 h-3.5 text-slate-400" />}
                          <span className="font-medium">{g.display_name}</span>
                          <span className="text-[10px] text-slate-400">({g.skills.length})</span>
                        </button>
                        {!collapsed && (
                          <div className="border-t border-slate-100 divide-y divide-slate-50">
                            {g.skills.map((sk) => (
                              <div key={sk.name} className="px-4 py-2.5 flex items-center gap-3">
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2">
                                    <span className="text-xs font-medium text-slate-800">{sk.name}</span>
                                    <span className={cn(
                                      "text-[10px] px-1.5 py-0.5 rounded",
                                      sk.state === "active"
                                        ? "bg-emerald-50 text-emerald-600"
                                        : "bg-amber-50 text-amber-600"
                                    )}>
                                      {sk.state === "active" ? "已转正" : "试验中"}
                                    </span>
                                  </div>
                                  <p className="text-[11px] text-slate-500 mt-0.5 line-clamp-1">{sk.description}</p>
                                </div>
                                <span className="text-[10px] text-slate-400 shrink-0">成功 {sk.success_uses} 次</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
            )}

            <div className="grid grid-cols-2 gap-3">
              <button
                onClick={() => setSkillDialog("new")}
                className="flex items-center justify-center gap-2 p-3 border border-dashed border-slate-300 rounded-xl text-sm text-slate-500 hover:border-hermes-400 hover:text-hermes-600 transition-colors"
              >
                <Plus className="w-4 h-4" /> 新建技能
              </button>
              <button
                onClick={() => setUrlDialog(true)}
                className="flex items-center justify-center gap-2 p-3 border border-dashed border-slate-300 rounded-xl text-sm text-slate-500 hover:border-hermes-400 hover:text-hermes-600 transition-colors"
              >
                <Link2 className="w-4 h-4" /> 从 URL 安装
              </button>
            </div>
          </div>
        )}

        {/* ── MCP 编辑对话框 ── */}
        {mcpDialog && (
          <McpEditDialog
            initial={mcpDialog === "new" ? null : mcpDialog}
            onClose={() => setMcpDialog(false)}
            onSaved={() => { setMcpDialog(false); loadAll(); }}
          />
        )}

        {/* ── Skill 编辑对话框 ── */}
        {skillDialog && (
          <SkillEditDialog
            name={skillDialog === "new" ? null : skillDialog.name}
            onClose={() => setSkillDialog(false)}
            onSaved={() => { setSkillDialog(false); loadAll(); }}
          />
        )}

        {/* ── URL 安装对话框 ── */}
        {urlDialog && (
          <UrlInstallDialog
            onClose={() => setUrlDialog(false)}
            onSaved={() => { setUrlDialog(false); loadAll(); }}
          />
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════
// 对话框
// ═══════════════════════════════════════════════════════════════════════

function DialogShell({ title, onClose, children, wide }: {
  title: string; onClose: () => void; children: React.ReactNode; wide?: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={onClose}>
      <div
        className={cn("bg-white rounded-2xl shadow-xl p-6 max-h-[85vh] overflow-y-auto", wide ? "w-[640px]" : "w-[480px]")}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-slate-900">{title}</h3>
          <button onClick={onClose} className="p-1 text-slate-400 hover:text-slate-600">
            <X className="w-4 h-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function McpEditDialog({ initial, onClose, onSaved }: {
  initial: McpEntry | null; onClose: () => void; onSaved: () => void;
}) {
  const [name, setName] = useState(initial?.name || "");
  const [type, setType] = useState<"stdio" | "http" | "sse">(initial?.type || "stdio");
  const [command, setCommand] = useState(initial?.command || "");
  const [argsText, setArgsText] = useState((initial?.args || []).join(" "));
  const [env, setEnv] = useState<Record<string, string>>(initial?.env || {});
  const [url, setUrl] = useState(initial?.url || "");
  const [headers, setHeaders] = useState<Record<string, string>>(initial?.headers || {});
  const [description, setDescription] = useState(initial?.description || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function save() {
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(name)) {
      setError("名称只能包含字母、数字、_ 和 -");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const payload: McpServerConfig = {
        enabled: initial?.enabled ?? true,
        type,
        description,
        ...(type === "stdio"
          ? { command, args: argsText.split(/\s+/).filter(Boolean), env }
          : { url, headers }),
      };
      await extensionsAPI.upsertMcpServer(name, payload);
      onSaved();
    } catch (err: any) {
      setError(err.response?.data?.detail || "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell title={initial ? `编辑 MCP 服务: ${initial.name}` : "添加 MCP 服务"} onClose={onClose}>
      <div className="space-y-3">
        {error && <p className="text-xs text-red-500">{error}</p>}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">名称 *</label>
            <input value={name} onChange={(e) => setName(e.target.value)} disabled={!!initial}
              placeholder="my-server"
              className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus disabled:bg-slate-50 font-mono" />
          </div>
          <div>
            <label className="block text-xs font-medium text-slate-600 mb-1">类型</label>
            <select value={type} onChange={(e) => setType(e.target.value as any)}
              className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus bg-white">
              <option value="stdio">stdio (本地进程)</option>
              <option value="http">http (远程服务)</option>
              <option value="sse">sse (远程服务)</option>
            </select>
          </div>
        </div>

        {type === "stdio" ? (
          <>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Command *</label>
              <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus font-mono" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Args (空格分隔)</label>
              <input value={argsText} onChange={(e) => setArgsText(e.target.value)} placeholder="-y @modelcontextprotocol/server-github"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus font-mono" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">
                环境变量 <span className="text-slate-400">(值可用 $VAR 引用 harness/.env)</span>
              </label>
              <KvEditor value={env} onChange={setEnv} keyPlaceholder="GITHUB_TOKEN" valuePlaceholder="$GITHUB_TOKEN" />
            </div>
          </>
        ) : (
          <>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">URL *</label>
              <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/mcp"
                className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus font-mono" />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Headers</label>
              <KvEditor value={headers} onChange={setHeaders} keyPlaceholder="Authorization" valuePlaceholder="Bearer ..." />
            </div>
          </>
        )}

        <div>
          <label className="block text-xs font-medium text-slate-600 mb-1">描述</label>
          <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="这个服务提供什么能力"
            className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus" />
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 rounded-lg">取消</button>
          <button onClick={save} disabled={saving}
            className="px-4 py-2 text-sm bg-slate-900 text-white rounded-lg hover:bg-slate-800 disabled:opacity-50">
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </div>
    </DialogShell>
  );
}

function SkillEditDialog({ name, onClose, onSaved }: {
  name: string | null; onClose: () => void; onSaved: () => void;
}) {
  const [skillName, setSkillName] = useState(name || "");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(!!name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!name) {
      setContent("---\nname: \ndescription: 什么时候使用这个技能\n---\n\n# 操作指令\n\n");
      return;
    }
    (async () => {
      try {
        const { data } = await extensionsAPI.getCustomSkill(name);
        setContent(data.content || "");
      } catch {
        setError("加载技能内容失败");
      } finally {
        setLoading(false);
      }
    })();
  }, [name]);

  async function save() {
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(skillName)) {
      setError("名称只能包含字母、数字、_ 和 -");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await extensionsAPI.writeCustomSkill(skillName, content);
      onSaved();
    } catch (err: any) {
      setError(err.response?.data?.detail || "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <DialogShell title={name ? `编辑技能: ${name}` : "新建技能"} onClose={onClose} wide>
      <div className="space-y-3">
        {error && <p className="text-xs text-red-500">{error}</p>}
        <div>
          <label className="block text-xs font-medium text-slate-600 mb-1">技能名称 *</label>
          <input value={skillName} onChange={(e) => setSkillName(e.target.value)} disabled={!!name}
            placeholder="my-skill"
            className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus disabled:bg-slate-50 font-mono" />
        </div>
        <div>
          <label className="block text-xs font-medium text-slate-600 mb-1">
            SKILL.md 内容 <span className="text-slate-400">(frontmatter 需含 name + description)</span>
          </label>
          {loading ? (
            <div className="flex justify-center py-10">
              <div className="w-5 h-5 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
            </div>
          ) : (
            <textarea value={content} onChange={(e) => setContent(e.target.value)} rows={16}
              className="w-full px-3 py-2 text-xs border border-slate-200 rounded-lg input-focus font-mono resize-y" />
          )}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 rounded-lg">取消</button>
          <button onClick={save} disabled={saving || loading}
            className="px-4 py-2 text-sm bg-slate-900 text-white rounded-lg hover:bg-slate-800 disabled:opacity-50">
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </div>
    </DialogShell>
  );
}

function UrlInstallDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [url, setUrl] = useState("");
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState("");

  async function install() {
    if (!url.startsWith("http://") && !url.startsWith("https://")) {
      setError("请输入 http/https 链接");
      return;
    }
    setInstalling(true);
    setError("");
    try {
      await extensionsAPI.installSkillFromUrl(url.trim());
      onSaved();
    } catch (err: any) {
      setError(err.response?.data?.detail || "安装失败");
    } finally {
      setInstalling(false);
    }
  }

  return (
    <DialogShell title="从 URL 安装技能" onClose={onClose}>
      <div className="space-y-3">
        {error && <p className="text-xs text-red-500">{error}</p>}
        <p className="text-xs text-slate-500">
          输入 SKILL.md 的直链地址 (如 GitHub raw 链接), 服务端会下载、校验并安装为你的私有技能。
        </p>
        <input value={url} onChange={(e) => setUrl(e.target.value)}
          placeholder="https://raw.githubusercontent.com/.../SKILL.md"
          className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg input-focus font-mono" />
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 rounded-lg">取消</button>
          <button onClick={install} disabled={installing}
            className="px-4 py-2 text-sm bg-slate-900 text-white rounded-lg hover:bg-slate-800 disabled:opacity-50">
            {installing ? "安装中..." : "安装"}
          </button>
        </div>
      </div>
    </DialogShell>
  );
}
