"""Member memory store — L1/L3 两层成员经验记忆 (Phase 4 记忆分层).

分层 (详见 docs/team-mode-refactor-plan.md §3):
- L3 (项目×成员): 项目内领域经验, 按任务相关性检索注入
    ``{base}/users/{uid}/projects/{pid}/memory/members/{agent}.json``
- L1 (成员全局): 跨项目通用经验, 全量注入 (体量小)
    ``{base}/users/{uid}/agents/{agent}/memory.json``

文件结构: ``{"practices": [...], "pitfalls": [...], "domain_notes": [...]}``
每条: ``{text, source_task_id, created_at, reuse_count, fingerprint}``

写入路径: 任务终态 → 程序式提取 (extract_lessons_from_task, 不跑 LLM) → 写 L3;
L3 晋升 L1 由程序计数判定 (跨项目出现 / 单项目复用), LLM 只做泛化改写,
无晋升决定权。成员换项目: L1 带走, L3 隔离, 避免跨项目污染。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config.memory_config import get_memory_config
from harness.config.paths import get_paths
from harness.memory.task_memory import _STOP_WORDS

logger = logging.getLogger(__name__)

# ── 经验类型 → 文件内列表键 ──
_KIND_KEYS: dict[str, str] = {
    "practice": "practices",
    "pitfall": "pitfalls",
    "domain_note": "domain_notes",
}

# ── 注入渲染用的中文标签 ──
_KIND_LABELS: dict[str, str] = {
    "practices": "practice",
    "pitfalls": "pitfall",
    "domain_notes": "note",
}

# ── 语义指纹: Jaccard 相似度 ≥ 此阈值视为同类经验 ──
SIMILARITY_THRESHOLD = 0.6

# ── L3→L1 泛化改写 prompt: LLM 只做泛化, 不得新增事实 ──
_GENERALIZE_PROMPT = """Rewrite the following project-specific experience as a cross-project general lesson.

Requirements:
- Remove project-specific details (project names, concrete file paths, business-specific terms)
- Abstract it into a reusable general practice or lesson
- Do not add any new facts; only generalize the existing content
- Use the same language as the original; one sentence, within 120 characters
- Output only the rewritten text, with no explanation, prefix, or suffix

Experience kind: {kind}
Original experience: {text}"""

# ── 注入时每条经验的文本截断长度 ──
_INJECT_TEXT_MAX = 200

_CJK_RE = re.compile(r"[一-鿿]")
_SPLIT_RE = re.compile(r"[\s,，、。.!！?？:：;；\-—/\\()（）\[\]【】《》\"“”'‘’]+")
_TOKEN_PART_RE = re.compile(r"[一-鿿]+|[a-z0-9_.]+")


# ──────────────────────────────────────────────────────────────────────────────
# 语义指纹 (纯函数, 可测试)
# ──────────────────────────────────────────────────────────────────────────────

def normalize_keywords(text: str) -> set[str]:
    """文本归一化为关键词集合: 小写 / 去标点 / 去停用词.

    英文按词取关键词; 中文等无空格语言取二字 bigram 作为关键词
    (单字 CJK 片段保留非停用词单字), 使 Jaccard 对中文同样有效。
    """
    keywords: set[str] = set()
    for token in _SPLIT_RE.split((text or "").lower()):
        token = token.strip()
        if not token or token in _STOP_WORDS:
            continue
        for part in _TOKEN_PART_RE.findall(token):
            if _CJK_RE.search(part):
                if len(part) == 1:
                    if part not in _STOP_WORDS:
                        keywords.add(part)
                    continue
                for i in range(len(part) - 1):
                    bigram = part[i : i + 2]
                    if bigram not in _STOP_WORDS:
                        keywords.add(bigram)
            else:
                if len(part) < 2 or part in _STOP_WORDS:
                    continue
                # 跳过纯数字
                if part.replace(".", "").isdigit():
                    continue
                keywords.add(part)
    return keywords


def text_fingerprint(text: str) -> str:
    """生成语义指纹: 归一化关键词集合的稳定字符串表示 (排序后空格连接)."""
    return " ".join(sorted(normalize_keywords(text)))


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """两个关键词集合的 Jaccard 相似度."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_similar_experience(fp_a: str, fp_b: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """按语义指纹判定两条经验是否同类 (Jaccard ≥ threshold)."""
    return jaccard_similarity(set(fp_a.split()), set(fp_b.split())) >= threshold


# ──────────────────────────────────────────────────────────────────────────────
# 任务经验提取 (程序式, 不跑 LLM; teammate 完成时与 orchestrator 结算两处复用)
# ──────────────────────────────────────────────────────────────────────────────

def extract_lessons_from_task(task: Any) -> list[tuple[str, str]]:
    """从终态任务程序式提取成员经验, 返回 ``[(kind, text)]``.

    规则 (保持简单, 见 Phase 4 决策):
    - failed 且 failure_reason 非空 → pitfall
    - completed/approved 且 output 非空且任务有 spec.goal → practice
      (文本 = goal + 结论摘要, 截断)
    - 无实质内容不写
    """
    lessons: list[tuple[str, str]] = []
    status = getattr(task.status, "value", task.status) or ""
    title = (getattr(task, "title", "") or "").strip()

    if status == "failed":
        reason = (task.effective_failure_reason() or "").strip()
        if reason:
            text = f'Task "{title}" failed: {reason}' if title else reason
            lessons.append(("pitfall", text[:300]))
    elif status in ("completed", "approved"):
        output = (task.effective_output() or "").strip()
        spec = getattr(task, "spec", None)
        goal = (getattr(spec, "goal", "") or "").strip() if spec else ""
        if output and goal:
            lessons.append(("practice", f"{goal} — {output}"[:300]))
    return lessons


# ──────────────────────────────────────────────────────────────────────────────
# MemberMemoryStore
# ──────────────────────────────────────────────────────────────────────────────

def _empty_data() -> dict[str, list[dict]]:
    return {key: [] for key in _KIND_KEYS.values()}


def _safe_name(name: str) -> str:
    """成员名 → 文件名片段 (防路径注入)."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name or "unknown")


class MemberMemoryStore:
    """L1/L3 成员经验记忆的持久化与检索.

    文件读写带 fcntl 排他锁 (与 TeamTaskStore 同模式), JSON 原子写
    (tmp + os.replace)。``llm`` 仅用于 L3→L1 晋升时的泛化改写,
    为 None 时按 memory_config / 环境变量惰性创建, 创建失败则跳过晋升。
    """

    def __init__(
        self,
        user_id: str = "default",
        *,
        base_dir: str | Path | None = None,
        llm: Any | None = None,
        model_name: str | None = None,
        api_key: str = "",
        base_url: str = "",
    ) -> None:
        self._user_id = user_id
        self._base_dir = Path(base_dir) if base_dir is not None else get_paths().base_dir
        self._llm = llm
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def _l3_path(self, project_id: str, agent: str) -> Path:
        return (
            self._base_dir / "users" / self._user_id / "projects" / project_id
            / "memory" / "members" / f"{_safe_name(agent)}.json"
        )

    def _l1_path(self, agent: str) -> Path:
        return (
            self._base_dir / "users" / self._user_id / "agents"
            / _safe_name(agent) / "memory.json"
        )

    # ------------------------------------------------------------------
    # 文件 IO (带锁 + 原子写)
    # ------------------------------------------------------------------

    def _load_file(self, path: Path) -> dict[str, list[dict]]:
        """读取记忆文件, 不存在/损坏时返回空结构."""
        if not path.exists():
            return _empty_data()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load member memory '%s': %s", path, exc)
            return _empty_data()
        result = _empty_data()
        for key in result:
            if isinstance(data.get(key), list):
                result[key] = data[key]
        return result

    def _save_file(self, path: Path, data: dict[str, list[dict]]) -> None:
        """原子写记忆文件 (tmp + os.replace)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    def _update_file(self, path: Path, mutate) -> None:
        """带排他锁的读-改-写 (fcntl 模式同 TeamTaskStore)."""
        import fcntl

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                data = _empty_data()
                if raw.strip():
                    try:
                        loaded = json.loads(raw)
                        for key in data:
                            if isinstance(loaded.get(key), list):
                                data[key] = loaded[key]
                    except json.JSONDecodeError:
                        logger.warning("Corrupt member memory '%s', resetting", path)
                mutate(data)
                self._save_file(path, data)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # 写入 L3
    # ------------------------------------------------------------------

    async def add_lesson(
        self,
        project_id: str,
        agent: str,
        kind: str,
        text: str,
        source_task_id: str = "",
    ) -> bool:
        """写入一条 L3 经验 (指纹去重 + 容量淘汰), 并自动触发晋升检查.

        - 同 source_task_id → 幂等跳过 (teammate 完成时与 orchestrator
          结算两处写入不重复)
        - 语义指纹同类 → 已有条目 reuse_count+1, 不新增
        - 每类容量 ≤ member_memory_l3_max_items, 超出淘汰最少复用/最旧
        """
        if kind not in _KIND_KEYS:
            raise ValueError(f"unknown lesson kind: {kind!r}")
        text = (text or "").strip()
        if not text:
            return False
        fp = text_fingerprint(text)
        if not fp:
            return False

        key = _KIND_KEYS[kind]
        max_items = get_memory_config().member_memory_l3_max_items
        outcome = ""

        def _mutate(data: dict[str, list[dict]]) -> None:
            nonlocal outcome
            # 1) source_task_id 幂等去重 (跨所有类检查)
            if source_task_id:
                for entries in data.values():
                    if any(e.get("source_task_id") == source_task_id for e in entries):
                        outcome = "dup_task"
                        return
            entries = data[key]
            # 2) 语义指纹去重: 同类经验 → reuse_count+1
            for e in entries:
                if is_similar_experience(e.get("fingerprint", ""), fp):
                    e["reuse_count"] = e.get("reuse_count", 0) + 1
                    outcome = "reused"
                    return
            entries.append({
                "text": text,
                "source_task_id": source_task_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reuse_count": 0,
                "fingerprint": fp,
            })
            # 3) 容量淘汰: 最少复用优先, 其次最旧
            if len(entries) > max_items:
                entries.sort(
                    key=lambda e: (e.get("reuse_count", 0), e.get("created_at", ""))
                )
                del entries[: len(entries) - max_items]
            outcome = "added"

        self._update_file(self._l3_path(project_id, agent), _mutate)
        if outcome in ("added", "reused"):
            await self._maybe_promote(project_id, agent)
        return outcome == "added"

    # ------------------------------------------------------------------
    # L3 → L1 晋升 (程序计数 + LLM 仅泛化改写)
    # ------------------------------------------------------------------

    def _find_projects_with_similar(self, agent: str, fp: str) -> list[str]:
        """扫描该成员所有项目的 L3, 返回存在同指纹经验的项目 id 列表."""
        projects_dir = self._base_dir / "users" / self._user_id / "projects"
        if not projects_dir.is_dir():
            return []
        found: list[str] = []
        fname = f"{_safe_name(agent)}.json"
        for proj in sorted(projects_dir.iterdir()):
            path = proj / "memory" / "members" / fname
            if not path.exists():
                continue
            data = self._load_file(path)
            for entries in data.values():
                if any(is_similar_experience(e.get("fingerprint", ""), fp)
                       for e in entries):
                    found.append(proj.name)
                    break
        return found

    def _l1_has_source(self, l1_data: dict[str, list[dict]], fp: str) -> bool:
        """L1 是否已收录同源经验 (按 source_fingerprint / fingerprint 判定)."""
        for entries in l1_data.values():
            for e in entries:
                if is_similar_experience(e.get("source_fingerprint", ""), fp):
                    return True
                if is_similar_experience(e.get("fingerprint", ""), fp):
                    return True
        return False

    async def _maybe_promote(self, project_id: str, agent: str) -> None:
        """晋升检查: 达标经验的 L3 → L1 (LLM 只做泛化改写).

        达标条件 (阈值走 memory_config):
        - 同一指纹经验在 ≥ member_memory_promote_projects 个项目的 L3 出现, 或
        - 单项目 reuse_count ≥ member_memory_promote_reuse
        LLM 不可用/改写失败 → 跳过本次, 不阻断; 晋升事件写审计日志。
        """
        cfg = get_memory_config()
        path = self._l3_path(project_id, agent)
        data = self._load_file(path)
        l1_data = self._load_file(self._l1_path(agent))
        dirty = False

        for key, entries in data.items():
            kind = next((k for k, v in _KIND_KEYS.items() if v == key), key)
            for e in entries:
                if e.get("promoted"):
                    continue
                fp = e.get("fingerprint", "")
                if not fp:
                    continue
                # ── 程序计数判定 (LLM 无决定权) ──
                if e.get("reuse_count", 0) >= cfg.member_memory_promote_reuse:
                    source_projects = [project_id]
                else:
                    source_projects = self._find_projects_with_similar(agent, fp)
                    if len(source_projects) < cfg.member_memory_promote_projects:
                        continue
                # ── L1 已收录同源经验 → 标记跳过 ──
                if self._l1_has_source(l1_data, fp):
                    e["promoted"] = True
                    dirty = True
                    continue
                # ── LLM 仅做泛化改写 (去项目特定细节, 不得新增事实) ──
                new_text = await self._generalize(kind, e.get("text", ""))
                if not new_text:
                    continue  # LLM 不可用/失败 → 跳过本次
                self._append_l1(agent, key, new_text, source_fp=fp,
                                max_items=cfg.member_memory_l1_max_items)
                l1_data = self._load_file(self._l1_path(agent))
                e["promoted"] = True
                dirty = True
                # ── 审计日志: 指纹 / 来源项目 / 改写前后文本 ──
                logger.info(
                    "Member memory promoted L3→L1: agent=%s kind=%s "
                    "fingerprint=%s source_projects=%s original=%r generalized=%r",
                    agent, kind, fp, source_projects, e.get("text", ""), new_text,
                )
                # ── 标记其他项目的同指纹条目, 避免各项目重复晋升 ──
                for pid in source_projects:
                    if pid != project_id:
                        self._mark_promoted(pid, agent, fp)

        if dirty:
            try:
                self._save_file(path, data)
            except OSError as exc:
                logger.warning("Failed to save promoted flags '%s': %s", path, exc)

    def _mark_promoted(self, project_id: str, agent: str, fp: str) -> None:
        """把指定项目 L3 中同指纹的条目标记为已晋升 (best-effort)."""
        def _mutate(data: dict[str, list[dict]]) -> None:
            for entries in data.values():
                for e in entries:
                    if is_similar_experience(e.get("fingerprint", ""), fp):
                        e["promoted"] = True

        try:
            self._update_file(self._l3_path(project_id, agent), _mutate)
        except OSError as exc:
            logger.warning("Failed to mark promoted in '%s': %s", project_id, exc)

    def _append_l1(
        self, agent: str, key: str, text: str, *, source_fp: str, max_items: int,
    ) -> None:
        """写一条 L1 经验 (source_fingerprint 去重 + 容量淘汰)."""
        fp = text_fingerprint(text)

        def _mutate(data: dict[str, list[dict]]) -> None:
            entries = data[key]
            for e in entries:
                if is_similar_experience(e.get("source_fingerprint", ""), source_fp):
                    return
            entries.append({
                "text": text,
                "source_task_id": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reuse_count": 0,
                "fingerprint": fp,
                "source_fingerprint": source_fp,
            })
            if len(entries) > max_items:
                entries.sort(
                    key=lambda e: (e.get("reuse_count", 0), e.get("created_at", ""))
                )
                del entries[: len(entries) - max_items]

        self._update_file(self._l1_path(agent), _mutate)

    async def _generalize(self, kind: str, text: str) -> str:
        """调 LLM 做泛化改写; 不可用/失败返回空串 (调用方跳过, 不阻断)."""
        llm = self._llm
        if llm is None:
            llm = self._create_llm()
            if llm is None:
                return ""
        try:
            from harness.observability.usage_ledger import usage_context

            with usage_context(source="memory"):
                response = await llm.ainvoke(
                    _GENERALIZE_PROMPT.format(kind=kind, text=text)
                )
            content = getattr(response, "content", response)
            if isinstance(content, list):  # LangChain content blocks
                content = " ".join(
                    str(b.get("text", "") if isinstance(b, dict) else b)
                    for b in content
                )
            return str(content).strip()
        except Exception as exc:
            logger.warning("Member memory generalization failed: %s", exc)
            return ""

    def _create_llm(self) -> Any | None:
        """惰性创建晋升改写用的轻量模型 (无凭证时返回 None)."""
        try:
            from harness.memory.updater import _create_memory_model

            self._llm = _create_memory_model(
                self._model_name, api_key=self._api_key, base_url=self._base_url,
            )
        except Exception as exc:
            logger.warning("Failed to create member memory LLM: %s", exc)
            self._llm = None
        return self._llm

    # ------------------------------------------------------------------
    # 检索注入
    # ------------------------------------------------------------------

    def get_l3_context(
        self, project_id: str, agent: str, task_text: str, top_k: int | None = None,
    ) -> str:
        """按任务文本相关性检索 L3, 渲染为 ``<project_memory>`` 块.

        关键词重叠打分 (复用指纹归一化函数), 返回 top-K 条;
        无任务文本或无相关经验时返回空串 (调用方跳过注入)。
        """
        query_kws = normalize_keywords(task_text or "")
        if not query_kws:
            return ""
        if top_k is None:
            top_k = get_memory_config().member_memory_l3_top_k
        if top_k <= 0:
            return ""

        data = self._load_file(self._l3_path(project_id, agent))
        scored: list[tuple[int, str, dict]] = []
        for key, entries in data.items():
            for e in entries:
                overlap = len(query_kws & set(e.get("fingerprint", "").split()))
                if overlap > 0:
                    scored.append((overlap, key, e))
        if not scored:
            return ""
        scored.sort(key=lambda x: (-x[0], x[2].get("created_at", "")))

        lines = ["<project_memory>", "Relevant experience you have accumulated in this project:"]
        for _, key, e in scored[:top_k]:
            text = (e.get("text", "") or "")[:_INJECT_TEXT_MAX]
            reuse = e.get("reuse_count", 0)
            suffix = f" (reused x{reuse})" if reuse else ""
            lines.append(f"- [{_KIND_LABELS.get(key, key)}] {text}{suffix}")
        lines.append("</project_memory>")
        return "\n".join(lines)

    def get_l1_lessons(self, agent: str) -> list[dict]:
        """返回 L1 全部经验条目 (只读; Phase 5 技能进化候选提取用)."""
        data = self._load_file(self._l1_path(agent))
        return [e for entries in data.values() for e in entries]

    def get_l1_context(self, agent: str) -> str:
        """返回 L1 全量, 渲染为 ``<member_memory>`` 块 (体量小, 每条截断)."""
        data = self._load_file(self._l1_path(agent))
        lines = ["<member_memory>", "General experience you have accumulated across projects:"]
        count = 0
        for key, entries in data.items():
            for e in entries:
                text = (e.get("text", "") or "")[:_INJECT_TEXT_MAX]
                if not text:
                    continue
                lines.append(f"- [{_KIND_LABELS.get(key, key)}] {text}")
                count += 1
        if count == 0:
            return ""
        lines.append("</member_memory>")
        return "\n".join(lines)
