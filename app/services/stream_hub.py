"""
SSE 事件流枢纽 — 按 thread 广播事件给多个消费者 (断线续传, Phase 3)

设计要点:
- 每次运行 (execute / respond) 由后台泵任务 (_pump_run, 见 app/api/execute.py)
  消费 Harness 事件流, 经 publish() 为每条事件分配单调递增序号
  (seq 从 1 开始, 每次运行重置), 写入环形缓冲并广播给所有订阅队列。
- 订阅时携带 last_event_id: 先补发缓冲中 seq > last_event_id 的事件,
  再从订阅队列读实时事件; 缓冲已覆盖不到 (gap) 或运行已结束
  (not_running) 时由调用方回退到状态轮询。
- 泵任务独立于 HTTP 连接运行, 原客户端断开 (页面刷新) 不影响事件生产,
  resume 端点因此能挂接到实时流继续推送。
- 终态后保留缓冲 CLEANUP_DELAY_SEC 秒供迟到的 resume 判定/补发, 随后清理;
  内存有界 (BUFFER_MAX 条/运行)。

背压策略: 订阅队列满 (消费者严重滞后) 时丢弃该消费者 — 队尾塞入结束
哨兵使其流正常收尾, 前端检测到流结束后回退到轮询逻辑。
"""

import asyncio
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# 环形缓冲容量 (每次运行)。token 级流式约 10-30 事件/秒,
# 2000 条约覆盖 1-3 分钟高密度输出; 超出则 resume 判定 gap → 回退轮询。
BUFFER_MAX = 2000
# 单个订阅者队列容量; 消费者落后这么多事件会被丢弃 (见模块 docstring)
QUEUE_MAX = 200
# 终态后缓冲保留时长 (秒)
CLEANUP_DELAY_SEC = 300

# 终态事件 (与 app/api/execute.py 的落库终态一致)
TERMINAL_EVENTS = ("finished", "error", "team_error")

# subscribe() 返回状态
SUB_OK = "ok"
SUB_NOT_RUNNING = "not_running"
SUB_GAP = "gap"  # 缓冲最旧序号 > last_event_id + 1, 中间事件已丢失


class RunStream:
    """一次运行的事件流状态 (序号计数 + 环形缓冲 + 订阅者集合)"""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.seq = 0
        self.buffer: deque[tuple[int, str]] = deque(maxlen=BUFFER_MAX)
        self.subscribers: set[asyncio.Queue] = set()
        self.running = True
        self.pump_task: Optional[asyncio.Task] = None
        self.cleanup_task: Optional[asyncio.Task] = None


class Subscription:
    """subscribe() 成功时的订阅句柄"""

    def __init__(self, status: str, run: Optional[RunStream] = None,
                 missed: Optional[list[tuple[int, str]]] = None,
                 queue: Optional[asyncio.Queue] = None) -> None:
        self.status = status
        self.run = run
        self.missed = missed or []
        self.queue = queue


class StreamHub:
    """按 thread_id 管理活跃运行的事件广播 (进程内单例)"""

    def __init__(self) -> None:
        self._runs: dict[str, RunStream] = {}

    def start_run(self, thread_id: str) -> RunStream:
        """开始一次新运行 — 序号重置, 替换旧状态 (若有)"""
        old = self._runs.get(thread_id)
        if old is not None:
            old.running = False
            if old.cleanup_task is not None:
                old.cleanup_task.cancel()
            self._close_subscribers(old)
        rs = RunStream(thread_id)
        self._runs[thread_id] = rs
        return rs

    def publish(self, rs: RunStream, payload: str) -> int:
        """为事件分配序号, 写入缓冲并广播给所有订阅者。返回分配的序号。"""
        rs.seq += 1
        seq = rs.seq
        rs.buffer.append((seq, payload))
        for q in list(rs.subscribers):
            try:
                q.put_nowait((seq, payload))
            except asyncio.QueueFull:
                # 背压: 消费者严重滞后 → 丢弃该消费者, 塞结束哨兵让其
                # 流收尾 (前端流结束后回退轮询), 不阻塞其他消费者
                rs.subscribers.discard(q)
                self._force_sentinel(q)
                logger.warning(
                    f"[stream_hub] thread={rs.thread_id} 订阅者队列满, 已丢弃"
                )
        return seq

    def end_run(self, rs: RunStream) -> None:
        """运行结束 — 通知所有订阅者 (哨兵), 调度缓冲延迟清理。幂等。"""
        if not rs.running:
            return
        rs.running = False
        self._close_subscribers(rs)
        rs.cleanup_task = asyncio.create_task(self._cleanup_later(rs))

    def subscribe(self, thread_id: str, last_event_id: int = 0) -> Subscription:
        """订阅 thread 的事件流。

        - ok: 返回缓冲中 seq > last_event_id 的补发事件 + 实时队列
        - not_running: 无活跃运行 (未开始/已结束)
        - gap: 运行中但缓冲最旧序号 > last_event_id + 1, 中间事件已丢失
        """
        rs = self._runs.get(thread_id)
        if rs is None or not rs.running:
            return Subscription(SUB_NOT_RUNNING)
        if rs.buffer and rs.buffer[0][0] > last_event_id + 1:
            return Subscription(SUB_GAP)
        # 单事件循环内无 await — 快照 missed 与注册队列是原子的, 不漏不重
        missed = [(s, p) for s, p in rs.buffer if s > last_event_id]
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        rs.subscribers.add(q)
        return Subscription(SUB_OK, run=rs, missed=missed, queue=q)

    def unsubscribe(self, rs: RunStream, queue: asyncio.Queue) -> None:
        rs.subscribers.discard(queue)

    def is_running(self, thread_id: str) -> bool:
        rs = self._runs.get(thread_id)
        return rs is not None and rs.running

    def get_run(self, thread_id: str) -> Optional[RunStream]:
        return self._runs.get(thread_id)

    # ── 内部 ──────────────────────────────────────────────────────

    def _close_subscribers(self, rs: RunStream) -> None:
        for q in list(rs.subscribers):
            rs.subscribers.discard(q)
            self._force_sentinel(q)

    @staticmethod
    def _force_sentinel(q: asyncio.Queue) -> None:
        """向队列塞入结束哨兵 (None); 队列满则挤掉最旧一条腾位置。"""
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _cleanup_later(self, rs: RunStream) -> None:
        """终态后延迟清理缓冲 (迟到 resume 仍可判定 not_running/gap)"""
        try:
            await asyncio.sleep(CLEANUP_DELAY_SEC)
        except asyncio.CancelledError:
            return
        # 仅当状态未被新运行替换时才清理
        if self._runs.get(rs.thread_id) is rs and not rs.running:
            self._runs.pop(rs.thread_id, None)


_hub: Optional[StreamHub] = None


def get_stream_hub() -> StreamHub:
    global _hub
    if _hub is None:
        _hub = StreamHub()
    return _hub
