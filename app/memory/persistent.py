"""
持久记忆模块 — 跨会话知识存储。

借鉴 OpenClaw 的 facts.db 思想和 Hermes 的持久记忆层：
存储用户偏好、历史结论、关键实体等跨会话信息。

使用 SQLite（轻量、零配置）作为存储后端。
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger("persistent_memory")

# 记忆条目最大数（按用户）
_MAX_FACTS_PER_USER = 50
# 过期天数（超期未访问的条目自动清理）
_FACT_TTL_DAYS = 30


def _get_db_path() -> str:
    """获取 SQLite 数据库文件路径。"""
    db_dir = Path(__file__).parent
    return str(db_dir / "facts.db")


def _init_db() -> None:
    """初始化 SQLite 数据库表结构。"""
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                created_at REAL NOT NULL,
                accessed_at REAL NOT NULL,
                access_count INTEGER DEFAULT 1,
                UNIQUE(user_id, key)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(user_id, category)
        """)
        conn.commit()


class PersistentMemory:
    """持久记忆引擎。

    存储内容：
    - 用户偏好（默认地市、常用指标等）
    - 历史查询的关键结论
    - 用户经常关注的实体（小区 CGI、地市等）

    不存储：完整对话历史（由 database.py 的 Conversation 负责）
    """

    def __init__(self) -> None:
        _init_db()

    async def get(self, user_id: str, key: str) -> Any | None:
        """获取一条记忆。"""
        db_path = _get_db_path()
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM facts WHERE user_id = ? AND key = ?",
                    (user_id, key),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE facts SET accessed_at = ?, access_count = access_count + 1 WHERE user_id = ? AND key = ?",
                        (time.time(), user_id, key),
                    )
                    conn.commit()
                    return json.loads(row[0])
        except Exception as exc:
            logger.warning("读取记忆失败: %s", exc)
        return None

    async def set(
        self, user_id: str, key: str, value: Any,
        category: str = "general",
    ) -> None:
        """写入一条记忆（upsert）。"""
        db_path = _get_db_path()
        now = time.time()
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO facts (user_id, key, value, category, created_at, accessed_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, key) DO UPDATE SET
                       value = excluded.value, category = excluded.category,
                       accessed_at = excluded.accessed_at,
                       access_count = access_count + 1""",
                    (user_id, key, json.dumps(value, ensure_ascii=False), category, now, now),
                )
                conn.commit()
            # 清理超量条目
            await self._cleanup(user_id)
        except Exception as exc:
            logger.warning("写入记忆失败: %s", exc)

    async def get_all(self, user_id: str) -> dict[str, Any]:
        """获取用户的所有记忆（返回 key → value 映射）。"""
        db_path = _get_db_path()
        try:
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT key, value FROM facts WHERE user_id = ? ORDER BY accessed_at DESC",
                    (user_id,),
                ).fetchall()
                return {row[0]: json.loads(row[1]) for row in rows}
        except Exception as exc:
            logger.warning("读取全部记忆失败: %s", exc)
            return {}

    async def get_similar(
        self, user_id: str, query: str, limit: int = 3,
    ) -> list[dict[str, Any]]:
        """基于关键词的简单模糊匹配（不引入向量检索的复杂度）。

        匹配 key 中包含 query 关键词的记忆条目。
        """
        db_path = _get_db_path()
        results: list[dict[str, Any]] = []
        try:
            with sqlite3.connect(db_path) as conn:
                # 将查询拆解为关键词，每个词单独 LIKE 匹配
                keywords = query.strip().split()
                conditions = " OR ".join(["key LIKE ?"] * len(keywords))
                params = [f"%{kw}%" for kw in keywords]
                rows = conn.execute(
                    f"SELECT key, value, category FROM facts WHERE user_id = ? AND ({conditions}) LIMIT ?",
                    [user_id, *params, limit],
                ).fetchall()
                for row in rows:
                    results.append({
                        "key": row[0],
                        "value": json.loads(row[1]),
                        "category": row[2],
                    })
        except Exception as exc:
            logger.warning("模糊匹配记忆失败: %s", exc)
        return results

    async def extract_and_save(
        self, user_id: str, query: str, answer: str,
    ) -> None:
        """从对话中自动提取关键信息并保存（借鉴 OpenClaw Metabolism 思想）。

        简单的规则提取（不做 LLM 提取以保持轻量）：
        - 检测地市名称
        - 检测小区 CGI 格式
        - 记录查询类别
        """
        now = time.time()
        facts: list[tuple[str, Any, str]] = []

        # 提取地市名
        import re
        cities = re.findall(r"(\w+市)", query + answer)
        for city in cities[:3]:
            facts.append((f"dist:{city}", {"district": city, "source": "auto-extract"}, "location"))

        # 提取 CGI
        cgis = re.findall(r"(460-\d{2}-\d+-\d+)", query)
        for cgi in cgis[:3]:
            facts.append((f"cgi:{cgi}", {"cgi": cgi, "source": "auto-extract"}, "entity"))

        # 数量限制
        if len(facts) > 5:
            facts = facts[:5]

        for key, value, category in facts:
            await self.set(user_id, key, value, category)

    async def _cleanup(self, user_id: str) -> None:
        """清理超量和超期的记忆条目。"""
        db_path = _get_db_path()
        cutoff_time = time.time() - _FACT_TTL_DAYS * 86400
        try:
            with sqlite3.connect(db_path) as conn:
                # 删除超期条目
                conn.execute(
                    "DELETE FROM facts WHERE user_id = ? AND accessed_at < ?",
                    (user_id, cutoff_time),
                )
                # 保留最近访问的 N 条
                conn.execute("""
                    DELETE FROM facts WHERE id IN (
                        SELECT id FROM facts WHERE user_id = ?
                        ORDER BY accessed_at DESC
                        LIMIT -1 OFFSET ?
                    )
                """, (user_id, _MAX_FACTS_PER_USER))
                conn.commit()
        except Exception as exc:
            logger.warning("清理记忆失败: %s", exc)


# 全局单例
persistent_memory = PersistentMemory()
