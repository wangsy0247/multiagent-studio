"""
SQLite FTS5 全文索引层（messages 表）

设计参照 hermes-agent `hermes_state.py`：
- 两张 FTS5 虚拟表：`messages_fts`（unicode61 分词，英文/通用）与
  `messages_fts_trigram`（trigram 分词，中日韩 ≥3 字查询）
- FTS 表 rowid 与 messages 隐式 rowid 一一对应，只建一列索引列，
  其余字段查询时 JOIN 回主表
- 索引文本 = content + extra_metadata 中所有 JSON 文本值（json_tree 提取）。
  参照 hermes 把工具名与工具调用参数纳入索引 —— tool_call 消息 content
  为空，不索引工具信息就完全搜不到；且 SQLAlchemy JSON 序列化默认
  ensure_ascii=True，中文会以 \\uXXXX 转义存储，必须经 json_tree 还原
- 3 个触发器（INSERT/UPDATE/DELETE）自动同步两张 FTS 表，写入侧零改动；
  ensure_fts 每次启动 DROP+CREATE 触发器以保证定义最新
- 仅在 SQLite 后端启用（生产 PostgreSQL 跳过）
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def _indexed_expr(p: str) -> str:
    """索引文本表达式。``p`` 为列前缀：触发器内 ``new.``，回填 SELECT 用 ``m.``。"""
    return (
        f"COALESCE({p}content, '')"
        f" || ' ' || COALESCE("
        f"(SELECT group_concat(jt.value, ' ')"
        f" FROM json_tree({p}extra_metadata) jt WHERE jt.type = 'text'), '')"
    )


_STATEMENTS = [
    # 英文/通用：unicode61 分词
    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        content, tokenize='unicode61'
    )""",
    # 中日韩：trigram 分词（需 ≥3 个汉字才能命中）
    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
        content, tokenize='trigram'
    )""",
    # 触发器每次启动重建（DROP+CREATE），保证表达式升级后旧库也能更新
    "DROP TRIGGER IF EXISTS messages_fts_insert",
    "DROP TRIGGER IF EXISTS messages_fts_update",
    "DROP TRIGGER IF EXISTS messages_fts_delete",
    f"""CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, content)
        SELECT new.rowid, {_indexed_expr('new.')};
        INSERT INTO messages_fts_trigram(rowid, content)
        SELECT new.rowid, {_indexed_expr('new.')};
    END""",
    f"""CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
        DELETE FROM messages_fts WHERE rowid = old.rowid;
        DELETE FROM messages_fts_trigram WHERE rowid = old.rowid;
        INSERT INTO messages_fts(rowid, content)
        SELECT new.rowid, {_indexed_expr('new.')};
        INSERT INTO messages_fts_trigram(rowid, content)
        SELECT new.rowid, {_indexed_expr('new.')};
    END""",
    """CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
        DELETE FROM messages_fts WHERE rowid = old.rowid;
        DELETE FROM messages_fts_trigram WHERE rowid = old.rowid;
    END""",
]

# 存量回填（m. 前缀，两表独立判空）
_BACKFILL = {
    "messages_fts": f"""INSERT INTO messages_fts(rowid, content)
        SELECT m.rowid, {_indexed_expr('m.')} FROM messages m""",
    "messages_fts_trigram": f"""INSERT INTO messages_fts_trigram(rowid, content)
        SELECT m.rowid, {_indexed_expr('m.')} FROM messages m""",
}


async def ensure_fts(conn: AsyncConnection) -> None:
    """幂等创建 FTS5 表与触发器，并对存量消息做一次回填。

    在 ``init_db()`` 的 ``engine.begin()`` 块内、``create_all`` 之后调用。
    回填判据：对应 FTS 表为空且 messages 非空（两表独立判断，
    触发器会保证此后增量同步）。
    """
    for stmt in _STATEMENTS:
        await conn.execute(text(stmt))

    msg_count = (await conn.execute(text("SELECT count(*) FROM messages"))).scalar_one()
    if msg_count == 0:
        return
    for table, backfill_sql in _BACKFILL.items():
        fts_count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        if fts_count == 0:
            await conn.execute(text(backfill_sql))
