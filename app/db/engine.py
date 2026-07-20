"""
数据库引擎与会话管理
本地开发默认使用 SQLite，生产环境使用 PostgreSQL
"""

import os
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlmodel import SQLModel

DATABASE_URL = os.getenv("DATABASE_URL", "")

if DATABASE_URL:
    # PostgreSQL / 其他数据库
    ENGINE_URL = DATABASE_URL
else:
    # 本地开发用 SQLite
    DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".multiagent-studio", "app.db")
    DB_PATH = os.getenv("SQLITE_PATH", DEFAULT_DB)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    ENGINE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
    print(f"[db] 使用 SQLite: {DB_PATH}")

engine = create_async_engine(ENGINE_URL, echo=False, pool_size=5, max_overflow=5)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """初始化数据库表 — 开发环境自动建表, 生产环境用 Alembic"""
    import app.models.user        # noqa: F401
    import app.models.thread      # noqa: F401
    import app.models.message     # noqa: F401
    import app.models.file_record # noqa: F401
    import app.models.configuration # noqa: F401
    import app.models.scheduled_task # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖: 获取数据库会话。

    写操作的路由处理函数应在返回响应前自行 ``await db.commit()``，
    避免响应已发送但提交失败导致静默数据丢失。

    对于只读操作（未自行提交），此处执行一次安全的延迟提交。
    """
    from sqlalchemy.exc import InvalidRequestError

    async with async_session() as session:
        try:
            yield session
            # 如果处理函数未自行提交，则此处兜底提交
            try:
                await session.commit()
            except InvalidRequestError:
                # 已经提交过或事务已关闭 → 安全忽略
                pass
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
