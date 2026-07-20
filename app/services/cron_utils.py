"""
cron 调度纯函数 — 表达式校验、时区换算、jitter 防惊群、misfire 策略

约定:
- 函数边界上的 aware datetime 一律为 UTC
- DB 存 naive UTC（项目统一风格），入库前 to_naive_utc()，出库后 to_aware_utc()
- jitter 用任务 id 的确定性哈希，同一任务每次偏移相同（参考 Claude Code）
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter

DEFAULT_TIMEZONE = "Asia/Shanghai"

# misfire 宽限窗口 = clamp(半个调度周期, MIN, MAX)，超窗 fast-forward 不补跑
MIN_GRACE_SECONDS = 120.0
MAX_GRACE_SECONDS = 7200.0
# 一次性任务宽限窗口（hermes 同款）
ONESHOT_GRACE_SECONDS = 120.0
# jitter: recurring 最多推迟 min(周期*10%, 15min)；落在整/半点的一次性任务最多提前 90s
MAX_JITTER_RATIO = 0.10
MAX_JITTER_SECONDS = 900
ONESHOT_EARLY_MAX_SECONDS = 90


# ── 时间转换 ─────────────────────────────────────────────────


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_naive_utc(dt: datetime) -> datetime:
    """aware → naive UTC（入库用）"""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def to_aware_utc(dt: datetime) -> datetime:
    """naive（视为 UTC）→ aware UTC（出库用）"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── 校验 ─────────────────────────────────────────────────────


def validate_cron_expr(expr: str) -> Optional[str]:
    """合法返回 None，非法返回错误信息"""
    fields = expr.strip().split()
    if len(fields) != 5:
        return "cron 表达式必须是 5 个字段: 分 时 日 月 星期"
    try:
        croniter(expr)
    except (ValueError, KeyError):
        return f"非法 cron 表达式: {expr}"
    return None


def validate_timezone_name(tz_name: str) -> Optional[str]:
    try:
        ZoneInfo(tz_name)
    except (KeyError, ValueError):
        return f"非法时区: {tz_name}"
    return None


# ── 相对时长（delay）解析 ─────────────────────────────────────
# Agent 创建相对时间任务（"10 分钟后提醒我"）时无需知道当前时间：
# 传 delay 字符串，由服务器基于自己的时钟换算绝对时间
_DELAY_SEGMENT = re.compile(r"(\d+)\s*([smhd])")
MAX_DELAY_DAYS = 365
_DELAY_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def parse_delay(text: str) -> tuple[Optional[timedelta], Optional[str]]:
    """解析相对时长字符串 → (timedelta, None) 或 (None, 错误信息)

    支持: "30s"、"10m"、"2h"、"1d" 及组合 "1h30m"、"2d12h"（大小写不敏感）。
    """
    text = (text or "").strip().lower()
    if not text:
        return None, "delay 不能为空（示例: 30s, 10m, 2h, 1d, 1h30m）"
    total = timedelta(0)
    pos = 0
    for match in _DELAY_SEGMENT.finditer(text):
        if match.start() != pos:
            return None, f"无法解析的相对时长: {text}（示例: 30s, 10m, 2h, 1d, 1h30m）"
        value, unit = int(match.group(1)), match.group(2)
        total += value * _DELAY_UNITS[unit]
        pos = match.end()
    if pos != len(text) or total <= timedelta(0):
        return None, f"无法解析的相对时长: {text}（示例: 30s, 10m, 2h, 1d, 1h30m）"
    if total > timedelta(days=MAX_DELAY_DAYS):
        return None, f"相对时长不能超过 {MAX_DELAY_DAYS} 天"
    return total, None


# ── jitter ───────────────────────────────────────────────────


def _stable_hash(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def jitter_delay_seconds(task_id: str, period_seconds: float) -> int:
    """recurring 任务的确定性触发延迟: hash(task_id) % min(周期*10%, 15min)"""
    cap = min(period_seconds * MAX_JITTER_RATIO, float(MAX_JITTER_SECONDS))
    if cap < 1:
        return 0
    return _stable_hash(task_id) % int(cap)


def oneshot_early_seconds(task_id: str, run_at_utc: datetime) -> int:
    """一次性任务落在 :00/:30 整点时最多提前 90s（防惊群），其余时刻不提前"""
    if run_at_utc.minute in (0, 30) and run_at_utc.second == 0:
        return _stable_hash(f"{task_id}:early") % (ONESHOT_EARLY_MAX_SECONDS + 1)
    return 0


# ── 下次触发时间计算 ──────────────────────────────────────────


def period_seconds(expr: str, tz_name: str, base_utc: datetime) -> float:
    """估算调度周期：连续两次触发的时间差"""
    tz = ZoneInfo(tz_name)
    it = croniter(expr, base_utc.astimezone(tz))
    first = it.get_next(datetime)
    second = it.get_next(datetime)
    return (second - first).total_seconds()


def compute_next_run(expr: str, task_id: str, tz_name: str, base_utc: datetime) -> datetime:
    """从 base_utc 之后的下一次触发时间（aware UTC，含 jitter）"""
    tz = ZoneInfo(tz_name)
    nxt = croniter(expr, base_utc.astimezone(tz)).get_next(datetime)
    delay = jitter_delay_seconds(task_id, period_seconds(expr, tz_name, base_utc))
    return nxt.astimezone(timezone.utc) + timedelta(seconds=delay)


def compute_oneshot_next(run_at: datetime, tz_name: str, task_id: str) -> datetime:
    """解析一次性任务触发时间（aware UTC，含提前 jitter）

    run_at 为 naive 时按 tz_name 解释；aware 时直接换算。
    """
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=ZoneInfo(tz_name))
    run_at_utc = run_at.astimezone(timezone.utc).replace(microsecond=0)
    return run_at_utc - timedelta(seconds=oneshot_early_seconds(task_id, run_at_utc))


def preview_run_times(expr: str, tz_name: str, count: int = 5) -> list[datetime]:
    """未来 count 次触发时间（任务本地时区，不含 jitter，供前端预览）"""
    tz = ZoneInfo(tz_name)
    it = croniter(expr, utcnow().astimezone(tz))
    return [it.get_next(datetime) for _ in range(count)]


# ── misfire / 触发决策 ────────────────────────────────────────


@dataclass
class FirePlan:
    """tick 对单个到期任务的决策"""

    action: str  # "fire" 执行 | "skip" 跳过本次(misfire/超宽限) | "disable" 过期禁用
    next_run_at: Optional[datetime]  # aware UTC；写库前需 to_naive_utc
    enabled: bool
    last_status: Optional[str] = None  # 需要同步更新的 last_status（skip/disable 时）


def resolve_fire_plan(
    *,
    recurring: bool,
    cron_expr: Optional[str],
    tz_name: str,
    task_id: str,
    next_run_at: Optional[datetime],
    expires_at: Optional[datetime],
    now: datetime,
) -> FirePlan:
    """决定本次 tick 对到期任务的处理方式（now/next_run_at/expires_at 均为 aware UTC）"""
    if expires_at is not None and expires_at <= now:
        return FirePlan("disable", next_run_at, enabled=False, last_status="expired")

    if recurring and not cron_expr:
        # 数据异常：recurring 但没有表达式 → 禁用而不是静默消失
        return FirePlan("disable", next_run_at, enabled=False, last_status="error")

    if next_run_at is None:
        return FirePlan("disable", None, enabled=False, last_status="error")

    lateness = (now - next_run_at).total_seconds()

    if not recurring:
        if lateness <= ONESHOT_GRACE_SECONDS:
            return FirePlan("fire", next_run_at, enabled=False)
        # 一次性任务超宽限 → 标记跳过并禁用，不补跑
        return FirePlan("skip", next_run_at, enabled=False, last_status="skipped")

    period = period_seconds(cron_expr, tz_name, next_run_at)
    grace = min(max(period / 2, MIN_GRACE_SECONDS), MAX_GRACE_SECONDS)

    if lateness <= grace:
        # 正常触发；以本次触发时间为 base 推进，保证节奏不漂移
        return FirePlan(
            "fire",
            compute_next_run(cron_expr, task_id, tz_name, base_utc=next_run_at),
            enabled=True,
        )

    # misfire：停机过久，fast-forward 到未来时间点，不补跑（避免重启风暴）
    return FirePlan(
        "skip",
        compute_next_run(cron_expr, task_id, tz_name, base_utc=now),
        enabled=True,
        last_status="skipped",
    )
