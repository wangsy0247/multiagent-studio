"""Team 成员私有技能进化 — 候选提取 / 试用期 / 转正审批 (Phase 5 Skill 自进化).

与 ``harness/skills/evolution/`` 下已有的单 agent 进化体系 (review fork /
curator) 不同, 本模块只服务 Team 模式的 member:

- 成员私有技能库: ``{base}/users/{uid}/agents/{agent}/skills/{name}/SKILL.md``
  结构与现有 skill 格式一致 (YAML frontmatter + Markdown 正文)
- 元数据 ``skills_meta.json`` (per agent):
  ``{name: {state, created_at, source, success_uses, fail_uses,
  consecutive_fails, last_used_at, promoted_at, archived_at,
  pending_promotion, promotion_requested}}``
- 生命周期: add_candidate (probation) → record_use 计数 →
  成功 ≥ promote_success_uses 标记 pending_promotion → Lead 审批 promote/archive;
  连续失败 ≥ fail_archive_threshold 或 stale_days 未用 → archived
- 文件操作带 fcntl 排他锁 + 原子写 (tmp + os.replace), 沿用 MemberMemoryStore 模式
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harness.config.paths import get_paths
from harness.skills.validation import validate_skill_name

logger = logging.getLogger(__name__)

# ── 生命周期状态 ──
STATE_PROBATION = "probation"   # 试用期 (注入 prompt 标注"试验性")
STATE_ACTIVE = "active"         # 已转正 (Lead 审批通过)
STATE_ARCHIVED = "archived"     # 已归档 (不再注入)

# ── 默认阈值 (构造时可覆盖) ──
DEFAULT_PROMOTE_SUCCESS_USES = 3   # 试用期成功使用 ≥N → 标记待转正
DEFAULT_FAIL_ARCHIVE_THRESHOLD = 2  # 连续失败 ≥N → 归档
DEFAULT_STALE_DAYS = 30            # 未用 ≥N 天 → 归档

# ── 注入时单个技能正文截断长度 ──
_INJECT_CONTENT_MAX = 2000

# ── 高风险内容关键词 (SKILL.md 命中 → 转正审批标记 high, Lead 需升级用户确认) ──
RISKY_SKILL_KEYWORDS: tuple[str, ...] = (
    "写文件", "写入", "删除", "执行命令", "运行命令", "修改配置",
    "write_file", "file_write", "delete", "rm -", "execute",
    "curl -x post", "curl post", "--data", "git push", "drop ",
)

# ── 程序化经验启发式: 步骤标记 / 工具序列 ──
_STEP_RE = re.compile(
    r"(步骤\s*\d|第一步|第二步|第三步|step\s*\d|^\s*\d+[.、\)])",
    re.IGNORECASE | re.MULTILINE,
)
_TOOL_SEQ_RE = re.compile(r"(→|->|\bfile_read\b.*\bfile_write\b)", re.DOTALL)


def is_procedural_lesson(text: str, reuse_count: int = 0) -> bool:
    """判定一条经验是否"稳定复用的程序化流程" (技能进化候选原料).

    启发式 (命中任一即视为候选, 保持简单):
    - 含步骤标记 (步骤N / 第一步 / step 1 / 1. 2. 编号列表)
    - 含工具序列 (a → b 链式调用)
    - 被复用 ≥2 次 (reuse_count >= 2)
    """
    text = text or ""
    if reuse_count >= 2:
        return True
    return bool(_STEP_RE.search(text) or _TOOL_SEQ_RE.search(text))


def is_risky_skill(content: str) -> bool:
    """按内容关键词判定技能是否含写操作/命令执行等高风险动作."""
    text = (content or "").lower()
    return any(kw in text for kw in RISKY_SKILL_KEYWORDS)


def _safe_name(name: str) -> str:
    """成员名 → 文件名片段 (防路径注入), 与 member_memory 同规则."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name or "unknown")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ──────────────────────────────────────────────────────────────────────────────
# SKILL.md frontmatter 解析 (容错: 解析失败返回 None, 调用方跳过)
# ──────────────────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def parse_skill_md(skill_md: str) -> tuple[str, str] | None:
    """从 SKILL.md 文本解析 (name, description); 不合法返回 None."""
    if not skill_md:
        return None
    m = _FRONTMATTER_RE.match(skill_md.strip())
    if not m:
        return None
    try:
        import yaml
        metadata = yaml.safe_load(m.group(1))
    except Exception:
        return None
    if not isinstance(metadata, dict):
        return None
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    if not name or not description:
        return None
    try:
        name = validate_skill_name(name)
    except ValueError:
        return None
    return name, description


# ──────────────────────────────────────────────────────────────────────────────
# LLM 提炼候选技能 (失败返回 None, 调用方跳过不阻断)
# ──────────────────────────────────────────────────────────────────────────────

SKILL_DISTILL_PROMPT = """Distill the following experience into a reusable skill, output in strict SKILL.md format.

Requirements:
- Start with YAML frontmatter (enclosed by ---), which must contain:
  name: skill name of lowercase letters/digits/hyphens (e.g. api-retry-workflow)
  description: one sentence describing what the skill is for
- After the frontmatter, a Markdown body containing: when to use / steps (numbered list) / caveats
- Do not add facts not present in the experience; only restructure it
- Output only the SKILL.md content itself, no explanation or code-block wrapping

Experience:
{lesson}"""


async def distill_skill_candidate(lesson_text: str, llm: Any) -> str | None:
    """调 LLM 把一条程序化经验提炼为 SKILL.md 文本; 失败返回 None (不阻断)."""
    if not lesson_text or llm is None:
        return None
    try:
        response = await llm.ainvoke(SKILL_DISTILL_PROMPT.format(lesson=lesson_text))
        content = getattr(response, "content", response)
        if isinstance(content, list):  # LangChain content blocks
            content = " ".join(
                str(b.get("text", "") if isinstance(b, dict) else b)
                for b in content
            )
        text = str(content).strip()
        # 容忍 ```markdown 代码块包裹
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        if parse_skill_md(text) is None:
            return None
        return text
    except Exception as exc:
        logger.warning("Skill candidate distillation failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 注入渲染 (纯函数, 可测试)
# ──────────────────────────────────────────────────────────────────────────────

def render_evolved_skills_section(records: list[dict[str, Any]]) -> str:
    """把成员进化技能渲染为 ``<member_evolved_skills>`` prompt 块.

    probation 与 active 都注入; probation 标注"（试验性，谨慎使用）";
    archived 由 list_skills 过滤, 不会出现在 records 中。
    """
    if not records:
        return ""
    lines = ["<member_evolved_skills>", "Your own skills evolved through practice:"]
    has_probation = False
    for rec in records:
        state = rec.get("state", STATE_PROBATION)
        tag = " (experimental, use with caution)" if state == STATE_PROBATION else ""
        if state == STATE_PROBATION:
            has_probation = True
        content = (rec.get("content") or "")[:_INJECT_CONTENT_MAX]
        lines.append(f"### {rec.get('name', '')}{tag}")
        lines.append(content)
        lines.append("")
    if has_probation:
        # 上报约定: member 使用试验性技能后在 task_update 的 result JSON 里回填
        lines.append(
            "After using an experimental skill, report its outcome via the "
            "skill_feedback field in the task_update result JSON: "
            '[{"name": "skill-name", "success": true|false}]'
        )
    lines.append("</member_evolved_skills>")
    return "\n".join(lines)


def render_promotion_request(record: dict[str, Any], content: str) -> str:
    """渲染技能转正审批请求内容 (走 plan_approval 通道, Lead 据此审批)."""
    risky = is_risky_skill(content)
    risk_label = "high (involves writes/command execution, requires user confirmation)" if risky else "low (read-only/query)"
    return (
        "<skill_promotion>\n"
        f"Evolved skill of member '{record.get('agent', '')}' requests promotion:\n"
        f"  Skill name: {record.get('name', '')}\n"
        f"  Usage stats: {record.get('success_uses', 0)} succeeded / "
        f"{record.get('fail_uses', 0)} failed\n"
        f"  Risk flag: {risk_label}\n"
        f"  Created at: {record.get('created_at', '')}\n\n"
        "Full skill content:\n"
        f"{content}\n"
        "</skill_promotion>\n\n"
        "Handle per the skill promotion approval rules: review the content and stats, then approve_plan "
        "(approve -> promote, reject -> archive); for high-risk skills, ask_clarification the user first."
    )


async def send_promotion_approval_request(
    message_bus: Any,
    *,
    from_agent: str,
    lead_name: str,
    record: dict[str, Any],
    skill_content: str,
) -> str | None:
    """发送技能转正审批请求 (复用 plan_approval 通道), 返回 request_id.

    只负责发消息; 协议追踪登记 (req_id → skill 名) 由调用方完成,
    审批结果回来时在 PLAN_APPROVAL_RESPONSE 路由里处理 promote/archive。
    """
    from harness.team.models import TeamMessage, TeamMessageType

    req_id = str(uuid.uuid4())[:8]
    msg = TeamMessage(
        from_agent=from_agent, to_agent=lead_name,
        msg_type=TeamMessageType.PLAN_APPROVAL_REQUEST,
        content=render_promotion_request(record, skill_content),
        request_id=req_id,
    )
    await message_bus.send(msg)
    return req_id


# ──────────────────────────────────────────────────────────────────────────────
# MemberSkillEvolutionStore
# ──────────────────────────────────────────────────────────────────────────────

def _empty_record(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "state": STATE_PROBATION,
        "created_at": _now_iso(),
        "source": "evolved",
        "success_uses": 0,
        "fail_uses": 0,
        "consecutive_fails": 0,
        "last_used_at": None,
        "promoted_at": None,
        "archived_at": None,
        "pending_promotion": False,     # 达标待 Lead 审批
        "promotion_requested": False,   # 已发起过审批请求 (防重复发送)
    }


class MemberSkillEvolutionStore:
    """Team 成员私有进化技能的持久化与生命周期管理.

    目录结构::

        {base}/users/{uid}/agents/{agent}/skills/
            skills_meta.json          # 元数据 (状态/计数/时间戳)
            {name}/SKILL.md           # 每个技能一个目录 (可含资源文件)

    所有方法 best-effort: 文件损坏/IO 失败不抛异常, 返回空值或 False。
    """

    def __init__(
        self,
        user_id: str = "default",
        *,
        base_dir: str | Path | None = None,
        promote_success_uses: int = DEFAULT_PROMOTE_SUCCESS_USES,
        fail_archive_threshold: int = DEFAULT_FAIL_ARCHIVE_THRESHOLD,
        stale_days: int = DEFAULT_STALE_DAYS,
    ) -> None:
        self._user_id = user_id
        self._base_dir = Path(base_dir) if base_dir is not None else get_paths().base_dir
        self._promote_success_uses = promote_success_uses
        self._fail_archive_threshold = fail_archive_threshold
        self._stale_days = stale_days

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------

    def _skills_root(self, agent: str) -> Path:
        return (
            self._base_dir / "users" / self._user_id / "agents"
            / _safe_name(agent) / "skills"
        )

    def _meta_path(self, agent: str) -> Path:
        return self._skills_root(agent) / "skills_meta.json"

    def _skill_dir(self, agent: str, name: str) -> Path:
        return self._skills_root(agent) / name

    # ------------------------------------------------------------------
    # 元数据 IO (带锁 + 原子写, 模式同 MemberMemoryStore._update_file)
    # ------------------------------------------------------------------

    def _load_meta(self, agent: str) -> dict[str, dict[str, Any]]:
        path = self._meta_path(agent)
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load skills meta '%s': %s", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _save_meta(self, agent: str, data: dict[str, dict[str, Any]]) -> None:
        path = self._meta_path(agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    def _update_meta(self, agent: str, mutate) -> Any:
        """带 fcntl 排他锁的读-改-写; mutate(data) 的返回值透传给调用方."""
        import fcntl

        path = self._meta_path(agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                data: dict[str, dict[str, Any]] = {}
                if raw.strip():
                    try:
                        loaded = json.loads(raw)
                        if isinstance(loaded, dict):
                            data = {str(k): v for k, v in loaded.items()
                                    if isinstance(v, dict)}
                    except json.JSONDecodeError:
                        logger.warning("Corrupt skills meta '%s', resetting", path)
                result = mutate(data)
                self._save_meta(agent, data)
                return result
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # 生命周期 API
    # ------------------------------------------------------------------

    def add_candidate(
        self, agent: str, skill_md: str, meta: dict[str, Any] | None = None,
    ) -> str | None:
        """添加候选技能 (probation), 返回技能名; 解析失败/重名返回 None.

        同名技能已存在 (任意状态) → 不重复添加, 返回 None。
        """
        parsed = parse_skill_md(skill_md)
        if parsed is None:
            logger.debug("add_candidate: unparseable SKILL.md for agent '%s'", agent)
            return None
        name, _description = parsed

        outcome: list[bool] = [False]

        def _mutate(data: dict[str, dict[str, Any]]) -> None:
            if name in data:
                return  # 重名去重 (含已归档的, 避免反复复活)
            rec = _empty_record(name)
            if meta:
                for k, v in meta.items():
                    if k not in ("name", "state"):
                        rec[k] = v
            data[name] = rec
            outcome[0] = True

        self._update_meta(agent, _mutate)
        if not outcome[0]:
            return None

        # ── 写技能目录 (SKILL.md 原子写) ──
        try:
            skill_dir = self._skill_dir(agent, name)
            skill_dir.mkdir(parents=True, exist_ok=True)
            target = skill_dir / "SKILL.md"
            tmp_path = target.with_suffix(".md.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(skill_md)
            os.replace(tmp_path, target)
        except OSError as exc:
            logger.warning(
                "Failed to write candidate skill '%s' for '%s': %s", name, agent, exc,
            )
            # 元数据已写入但文件失败 → 回滚元数据, 保持一致
            self._update_meta(agent, lambda data: data.pop(name, None))
            return None

        logger.info("Skill candidate added: agent=%s name=%s (probation)", agent, name)
        return name

    def record_use(self, agent: str, name: str, success: bool) -> dict[str, Any] | None:
        """记录一次使用结果, 返回更新后的记录 (不存在返回 None).

        - success → success_uses+1, consecutive_fails 清零
        - 失败 → fail_uses+1, consecutive_fails+1;
          连续失败 ≥ fail_archive_threshold → archived
        - probation 且 success_uses ≥ promote_success_uses → pending_promotion
        """
        def _mutate(data: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
            rec = data.get(name)
            if rec is None:
                return None
            if rec.get("state") == STATE_ARCHIVED:
                return rec  # 已归档不再计数
            rec["last_used_at"] = _now_iso()
            if success:
                rec["success_uses"] = int(rec.get("success_uses") or 0) + 1
                rec["consecutive_fails"] = 0
            else:
                rec["fail_uses"] = int(rec.get("fail_uses") or 0) + 1
                rec["consecutive_fails"] = int(rec.get("consecutive_fails") or 0) + 1
            if rec.get("state") == STATE_PROBATION:
                if rec["consecutive_fails"] >= self._fail_archive_threshold:
                    rec["state"] = STATE_ARCHIVED
                    rec["archived_at"] = _now_iso()
                    rec["pending_promotion"] = False
                    logger.info(
                        "Skill '%s' archived (consecutive fails=%d): agent=%s",
                        name, rec["consecutive_fails"], agent,
                    )
                elif rec["success_uses"] >= self._promote_success_uses:
                    rec["pending_promotion"] = True
            return rec

        return self._update_meta(agent, _mutate)

    def promote(self, agent: str, name: str) -> bool:
        """转正 (probation → active); 仅 pending/promotion 期可转正."""
        def _mutate(data: dict[str, dict[str, Any]]) -> bool:
            rec = data.get(name)
            if rec is None or rec.get("state") == STATE_ARCHIVED:
                return False
            rec["state"] = STATE_ACTIVE
            rec["promoted_at"] = _now_iso()
            rec["pending_promotion"] = False
            return True

        ok = bool(self._update_meta(agent, _mutate))
        if ok:
            logger.info("Skill promoted: agent=%s name=%s", agent, name)
        return ok

    def archive(self, agent: str, name: str) -> bool:
        """归档 (任意状态 → archived, 不再注入; 保留文件可恢复)."""
        def _mutate(data: dict[str, dict[str, Any]]) -> bool:
            rec = data.get(name)
            if rec is None or rec.get("state") == STATE_ARCHIVED:
                return False
            rec["state"] = STATE_ARCHIVED
            rec["archived_at"] = _now_iso()
            rec["pending_promotion"] = False
            return True

        ok = bool(self._update_meta(agent, _mutate))
        if ok:
            logger.info("Skill archived: agent=%s name=%s", agent, name)
        return ok

    def sweep_stale(self, agent: str, now: datetime | None = None) -> int:
        """长期未用归档: last_used_at (或 created_at) 超过 stale_days → archived.

        返回本次归档数量。
        """
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self._stale_days)

        def _mutate(data: dict[str, dict[str, Any]]) -> int:
            count = 0
            for rec in data.values():
                if rec.get("state") == STATE_ARCHIVED:
                    continue
                anchor = (_parse_iso(rec.get("last_used_at"))
                          or _parse_iso(rec.get("created_at")) or now)
                if anchor <= cutoff:
                    rec["state"] = STATE_ARCHIVED
                    rec["archived_at"] = _now_iso()
                    rec["pending_promotion"] = False
                    count += 1
            return count

        count = int(self._update_meta(agent, _mutate))
        if count:
            logger.info("Stale skills archived: agent=%s count=%d", agent, count)
        return count

    def mark_promotion_requested(self, agent: str, name: str) -> None:
        """标记该技能已发起过转正审批 (防同一 run 内重复发送)."""
        def _mutate(data: dict[str, dict[str, Any]]) -> None:
            rec = data.get(name)
            if rec is not None:
                rec["promotion_requested"] = True

        self._update_meta(agent, _mutate)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_meta(self, agent: str, name: str) -> dict[str, Any] | None:
        """读取单个技能的元数据记录 (不存在返回 None)."""
        return self._load_meta(agent).get(name)

    def read_skill_content(self, agent: str, name: str) -> str:
        """读取技能 SKILL.md 全文 (不存在返回空串)."""
        path = self._skill_dir(agent, name) / "SKILL.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def list_skills(self, agent: str) -> list[dict[str, Any]]:
        """列出可注入的技能 (probation + active), 含正文; archived 不返回.

        每条: {name, description, state, content, success_uses, fail_uses}。
        元数据存在但 SKILL.md 文件缺失的条目跳过 (视为不可用)。
        """
        records: list[dict[str, Any]] = []
        for name, rec in sorted(self._load_meta(agent).items()):
            if rec.get("state") not in (STATE_PROBATION, STATE_ACTIVE):
                continue
            content = self.read_skill_content(agent, name)
            if not content:
                logger.warning(
                    "Skill '%s' meta exists but SKILL.md missing (agent=%s) — skipped",
                    name, agent,
                )
                continue
            description = ""
            parsed = parse_skill_md(content)
            if parsed is not None:
                description = parsed[1]
            records.append({
                "name": name,
                "description": description,
                "state": rec.get("state", STATE_PROBATION),
                "content": content,
                "success_uses": rec.get("success_uses", 0),
                "fail_uses": rec.get("fail_uses", 0),
            })
        return records

    def pending_promotions(self, agent: str) -> list[dict[str, Any]]:
        """列出达标待转正且尚未发起审批的技能 (probation + pending_promotion)."""
        result: list[dict[str, Any]] = []
        for name, rec in sorted(self._load_meta(agent).items()):
            if (rec.get("state") == STATE_PROBATION
                    and rec.get("pending_promotion")
                    and not rec.get("promotion_requested")):
                rec = dict(rec)
                rec.setdefault("name", name)
                rec["agent"] = agent
                result.append(rec)
        return result
