"""SQLite 持久化层：重启后批次谱系连续，不依赖内存状态。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingredient_batches (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,          -- spice 香料 / meat 肉类 / other 配菜
    name        TEXT NOT NULL,
    supplier    TEXT,
    received_at TEXT,
    allergens   TEXT NOT NULL DEFAULT '[]'   -- JSON: 过敏原清单
);

CREATE TABLE IF NOT EXISTS recipes (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    ratios     TEXT NOT NULL,           -- JSON: 配方比例，仅传承人/质控可读
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broth_batches (
    id             TEXT PRIMARY KEY,
    generation     INTEGER NOT NULL,    -- 老汤代际
    store_id       TEXT NOT NULL,       -- 当前所属/目标门店
    location_state TEXT NOT NULL DEFAULT 'on_site',  -- on_site / in_transit
    status         TEXT NOT NULL DEFAULT 'active',    -- active / frozen / quarantined / destroyed
    created_event_id TEXT,
    created_at     TEXT NOT NULL,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS cook_runs (
    id              TEXT PRIMARY KEY,
    broth_id        TEXT NOT NULL,
    store_id        TEXT NOT NULL,
    recipe_id       TEXT,
    temperature_c   REAL,               -- 下料时老汤温度（八十摄氏度下料时机）
    occurred_at     TEXT NOT NULL,
    created_event_id TEXT
);

CREATE TABLE IF NOT EXISTS ingredient_usages (
    cook_run_id           TEXT NOT NULL,
    ingredient_batch_id   TEXT NOT NULL,
    role                  TEXT NOT NULL,   -- spice / meat / side
    PRIMARY KEY (cook_run_id, ingredient_batch_id)
);

CREATE TABLE IF NOT EXISTS finished_products (
    id          TEXT PRIMARY KEY,        -- 消费批次
    cook_run_id TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',  -- active / held / destroyed
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    relation    TEXT NOT NULL,           -- split 分装 / topup 续汤 / merge 合并
                                          -- / heat 加热 / filter 过滤 / transfer 移交 / cook 烹制
    parent_type TEXT NOT NULL,           -- broth
    parent_id   TEXT NOT NULL,
    child_type  TEXT NOT NULL,           -- broth / product
    child_id    TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_parent ON lineage_edges(parent_type, parent_id);
CREATE INDEX IF NOT EXISTS idx_edges_child  ON lineage_edges(child_type, child_id);
CREATE INDEX IF NOT EXISTS idx_edges_event  ON lineage_edges(event_id);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,        -- 扫码/客户端生成，离线补传据此幂等
    type        TEXT NOT NULL,
    occurred_at TEXT NOT NULL,           -- 现场发生时间（非服务端收件时间）
    recorded_at TEXT NOT NULL,
    store_id    TEXT,
    actor       TEXT,
    payload     TEXT NOT NULL,          -- 规范化 JSON，重复扫码须与之完全一致
    replayed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tests (
    id          TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,           -- broth / product
    target_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- microbial 微生物 / sensory 感官 / temperature 温度
    verdict     TEXT NOT NULL,           -- normal 正常 / abnormal 异常 / pass / fail
    detail      TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    recorder    TEXT,
    incident_id TEXT,
    event_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tests_target ON tests(target_type, target_id);

CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    trigger_test_id TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'open',   -- open / closed
    opened_at       TEXT NOT NULL,
    closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS dispositions (
    id             TEXT PRIMARY KEY,
    incident_id    TEXT NOT NULL,
    action         TEXT NOT NULL,        -- quarantine 隔离 / destroy 报废 / retest 复检 / release 恢复
    scope          TEXT NOT NULL,        -- JSON: 实际作用到的批次
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    reason         TEXT,
    retest_test_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_disp_incident ON dispositions(incident_id);
"""


def canon(payload: dict[str, Any]) -> str:
    """事件载荷规范化：重复扫码判定与键序无关。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class LineageStore:
    """薄封装 sqlite3；业务规则在 service 层。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 基础写入 -------------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- 事件去重 -------------------------------------------------------

    def get_event(self, event_id: str) -> sqlite3.Row | None:
        return self.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()

    def insert_event(
        self,
        event_id: str,
        type_: str,
        occurred_at: str,
        recorded_at: str,
        store_id: str | None,
        actor: str | None,
        payload: dict[str, Any],
        replayed: bool = False,
    ) -> None:
        self.execute(
            """INSERT INTO events(event_id, type, occurred_at, recorded_at, store_id,
                                  actor, payload, replayed)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                event_id,
                type_,
                occurred_at,
                recorded_at,
                store_id,
                actor,
                canon(payload),
                int(replayed),
            ),
        )

    # ---- 谱系查询 -------------------------------------------------------

    def descendants(self, batch_type: str, batch_id: str) -> list[tuple[str, str, int]]:
        """返回 (type, id, depth)，含起点本身；沿父子边（含成品）。"""
        rows = self.execute(
            """
            WITH RECURSIVE linked(type, id, depth) AS (
                SELECT ?, ?, 0
                UNION
                SELECT e.child_type, e.child_id, l.depth + 1
                FROM lineage_edges e
                JOIN linked l ON e.parent_type = l.type AND e.parent_id = l.id
            )
            SELECT type, id, depth FROM linked
            """,
            (batch_type, batch_id),
        ).fetchall()
        return [(r["type"], r["id"], r["depth"]) for r in rows]

    def ancestors(self, batch_type: str, batch_id: str) -> list[tuple[str, str, int]]:
        rows = self.execute(
            """
            WITH RECURSIVE linked(type, id, depth) AS (
                SELECT ?, ?, 0
                UNION
                SELECT e.parent_type, e.parent_id, l.depth + 1
                FROM lineage_edges e
                JOIN linked l ON e.child_type = l.type AND e.child_id = l.id
            )
            SELECT type, id, depth FROM linked
            """,
            (batch_type, batch_id),
        ).fetchall()
        return [(r["type"], r["id"], r["depth"]) for r in rows]

    def edges_for_event(self, event_id: str) -> list[sqlite3.Row]:
        return self.execute(
            "SELECT * FROM lineage_edges WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()
