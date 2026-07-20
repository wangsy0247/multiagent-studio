"""cron_utils 纯函数测试"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter

from app.services import cron_utils as cu

UTC = timezone.utc
SH = ZoneInfo("Asia/Shanghai")


def _dt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


class TestValidate:
    def test_valid_cron(self):
        assert cu.validate_cron_expr("0 9 * * *") is None
        assert cu.validate_cron_expr("*/5 * * * 1-5") is None

    def test_invalid_field_count(self):
        assert cu.validate_cron_expr("0 9 * *") is not None
        assert cu.validate_cron_expr("0 9 * * * *") is not None

    def test_invalid_expr(self):
        assert cu.validate_cron_expr("99 9 * * *") is not None
        assert cu.validate_cron_expr("not-a-cron") is not None

    def test_timezone(self):
        assert cu.validate_timezone_name("Asia/Shanghai") is None
        assert cu.validate_timezone_name("Not/AZone") is not None


class TestComputeNextRun:
    def test_timezone_conversion(self):
        """Asia/Shanghai = UTC+8，'每天 9 点'对应本地 09:00（jitter 最多推迟到 09:15）"""
        base = _dt(2026, 1, 1, 0, 0)
        nxt = cu.compute_next_run("0 9 * * *", "task-a", "Asia/Shanghai", base)
        assert nxt > base
        local = nxt.astimezone(SH)
        assert local.hour == 9
        assert 0 <= local.minute < 15  # raw 09:00 + jitter < 900s

    def test_jitter_deterministic(self):
        """同一任务的 jitter 偏移每次计算相同"""
        base = _dt(2026, 1, 1, 0, 0)
        n1 = cu.compute_next_run("0 9 * * *", "task-a", "Asia/Shanghai", base)
        n2 = cu.compute_next_run("0 9 * * *", "task-a", "Asia/Shanghai", base)
        assert n1 == n2

    def test_jitter_bounded(self):
        """jitter 推迟不超过 min(周期*10%, 900s)"""
        base = _dt(2026, 1, 1, 0, 0)
        raw = croniter("0 9 * * *", base.astimezone(SH)).get_next(datetime).astimezone(UTC)
        nxt = cu.compute_next_run("0 9 * * *", "task-a", "Asia/Shanghai", base)
        assert timedelta(0) <= nxt - raw < timedelta(seconds=900)


class TestResolveFirePlan:
    def test_recurring_fire_within_grace(self):
        now = _dt(2026, 1, 1, 9, 0, 30)
        plan = cu.resolve_fire_plan(
            recurring=True, cron_expr="0 9 * * *", tz_name="Asia/Shanghai",
            task_id="t1", next_run_at=_dt(2026, 1, 1, 9, 0), expires_at=None, now=now,
        )
        assert plan.action == "fire"
        assert plan.enabled is True
        assert plan.next_run_at > now  # 已推进到下一周期

    def test_recurring_misfire_fast_forward(self):
        """每小时任务过期 3h（grace=1800s）→ skip 且快进到未来，不补跑"""
        now = _dt(2026, 1, 1, 12, 0)
        plan = cu.resolve_fire_plan(
            recurring=True, cron_expr="0 * * * *", tz_name="UTC",
            task_id="t1", next_run_at=_dt(2026, 1, 1, 9, 0), expires_at=None, now=now,
        )
        assert plan.action == "skip"
        assert plan.last_status == "skipped"
        assert plan.enabled is True
        assert plan.next_run_at > now

    def test_oneshot_within_grace(self):
        now = _dt(2026, 1, 1, 9, 1)
        plan = cu.resolve_fire_plan(
            recurring=False, cron_expr=None, tz_name="UTC",
            task_id="t1", next_run_at=_dt(2026, 1, 1, 9, 0), expires_at=None, now=now,
        )
        assert plan.action == "fire"
        assert plan.enabled is False  # 触发后自动禁用

    def test_oneshot_beyond_grace(self):
        now = _dt(2026, 1, 1, 9, 5)  # 晚 5 分钟，超 120s 宽限
        plan = cu.resolve_fire_plan(
            recurring=False, cron_expr=None, tz_name="UTC",
            task_id="t1", next_run_at=_dt(2026, 1, 1, 9, 0), expires_at=None, now=now,
        )
        assert plan.action == "skip"
        assert plan.enabled is False
        assert plan.last_status == "skipped"

    def test_expired_disables(self):
        now = _dt(2026, 1, 1, 9, 0)
        plan = cu.resolve_fire_plan(
            recurring=True, cron_expr="0 9 * * *", tz_name="UTC",
            task_id="t1", next_run_at=_dt(2026, 1, 1, 9, 0),
            expires_at=_dt(2026, 1, 1, 8, 0), now=now,
        )
        assert plan.action == "disable"
        assert plan.enabled is False
        assert plan.last_status == "expired"

    def test_recurring_without_cron_disables(self):
        """数据异常：recurring 但无表达式 → 禁用而非静默消失"""
        plan = cu.resolve_fire_plan(
            recurring=True, cron_expr=None, tz_name="UTC",
            task_id="t1", next_run_at=_dt(2026, 1, 1, 9, 0), expires_at=None,
            now=_dt(2026, 1, 1, 9, 1),
        )
        assert plan.action == "disable"
        assert plan.last_status == "error"


class TestOneshotNext:
    def test_naive_interpreted_in_timezone(self):
        """naive 时间按任务时区解释；UTC 分钟非 :00/:30 时不提前"""
        naive = datetime(2026, 1, 2, 9, 15)  # Asia/Shanghai 09:15 → UTC 01:15
        nxt = cu.compute_oneshot_next(naive, "Asia/Shanghai", "task-x")
        assert nxt == datetime(2026, 1, 2, 1, 15, tzinfo=UTC)

    def test_early_jitter_only_on_half_hour(self):
        at_half = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
        nxt = cu.compute_oneshot_next(at_half, "UTC", "task-x")
        assert at_half - timedelta(seconds=90) <= nxt <= at_half
        # 同一任务偏移确定
        assert cu.compute_oneshot_next(at_half, "UTC", "task-x") == nxt

        at_quarter = datetime(2026, 1, 2, 9, 15, tzinfo=UTC)
        assert cu.compute_oneshot_next(at_quarter, "UTC", "task-x") == at_quarter


class TestParseDelay:
    def test_basic_units(self):
        assert cu.parse_delay("30s")[0] == timedelta(seconds=30)
        assert cu.parse_delay("10m")[0] == timedelta(minutes=10)
        assert cu.parse_delay("2h")[0] == timedelta(hours=2)
        assert cu.parse_delay("1d")[0] == timedelta(days=1)

    def test_combinations_and_case(self):
        assert cu.parse_delay("1h30m")[0] == timedelta(hours=1, minutes=30)
        assert cu.parse_delay("2D12H")[0] == timedelta(days=2, hours=12)
        assert cu.parse_delay(" 10m ")[0] == timedelta(minutes=10)

    def test_invalid(self):
        for bad in ("", "abc", "10x", "-5m", "0m", "10", "m10", "10m5x"):
            delta, err = cu.parse_delay(bad)
            assert delta is None and err is not None, bad

    def test_max_delay(self):
        assert cu.parse_delay("400d")[1] is not None  # 超 365 天
        assert cu.parse_delay("365d")[0] is not None
