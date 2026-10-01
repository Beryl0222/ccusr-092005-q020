"""JSON 文件仓储：整库原子写入，服务重启不丢已确认事件。

存储结构（单个 JSON 文档）::

    {
      "stores":   {id: store},
      "materials":{id: material_batch},
      "broths":   {id: broth_batch},
      "events":   {event_id: lineage_event},
      "products": {id: product_batch},
      "checks":   {id: quality_check},
      "issues":   {id: issue},
      "decisions":[decision],
      "event_keys": {"op|store|time|parents": event_id},
      "counters": {"seq": int}
    }

事件的业务自然键是 ``(event_id, occurred_at, op)``：离线门店补传同一扫码动作
时直接返回既有事件（幂等）。``event_id`` 由门店扫码生成；即便完全相同的补传
重复到达，也不会产生第二条父子关系。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import Conflict, NotFound
from .models import new_id

_COLLECTIONS = (
    "stores",
    "materials",
    "broths",
    "events",
    "products",
    "checks",
    "issues",
)


def empty_db() -> dict[str, Any]:
    return {
        "stores": {},
        "materials": {},
        "broths": {},
        "events": {},
        "products": {},
        "checks": {},
        "issues": {},
        "decisions": [],
        "counters": {"seq": 0},
    }


class Repository:
    """内存集合 + JSON 落盘。

    所有写操作先改内存再原子落盘（同目录临时文件 + ``os.replace``），
    进程在任意时刻崩溃都只会停留在上一个完整版本，不会出现半截文档。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.db = self._load()

    # ------------------------------------------------------------ 载入与落盘
    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_db()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"批次档案损坏，无法解析 {self.path}: {exc}") from exc
        for name in _COLLECTIONS:
            data.setdefault(name, {})
        data.setdefault("decisions", [])
        data.setdefault("counters", {"seq": 0})
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.db, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --------------------------------------------------------------- 通用方法
    def next_seq(self) -> int:
        self.db["counters"]["seq"] += 1
        return self.db["counters"]["seq"]

    @staticmethod
    def _put(collection: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        rid = record["id"]
        if rid in collection:
            raise Conflict(f"记录已存在: {rid}")
        collection[rid] = record
        return record

    def get(self, kind: str, rid: str) -> dict[str, Any]:
        collection = self.db[kind]
        if rid not in collection:
            raise NotFound(f"{kind} 不存在: {rid}")
        return collection[rid]

    def must_get_broth(self, bid: str) -> dict[str, Any]:
        return self.get("broths", bid)

    def list(self, kind: str) -> list[dict[str, Any]]:
        return list(self.db[kind].values())

    # ------------------------------------------------------------------ 门店
    def add_store(self, store: dict[str, Any]) -> dict[str, Any]:
        return self._put(self.db["stores"], store)

    # ------------------------------------------------------------------ 物料
    def add_material(self, material: dict[str, Any]) -> dict[str, Any]:
        return self._put(self.db["materials"], material)

    # ------------------------------------------------------------------ 老汤
    def add_broth(self, batch: dict[str, Any]) -> dict[str, Any]:
        return self._put(self.db["broths"], batch)

    def update_broth(self, batch: dict[str, Any]) -> None:
        self.db["broths"][batch["id"]] = batch

    # ------------------------------------------------------------------ 事件
    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        events = self.db["events"]
        eid = event["event_id"]
        if eid in events:
            return events[eid]  # 补传幂等：返回既有事件，由服务层比对
        events[eid] = event
        return event

    def find_event(self, event_id: str) -> dict[str, Any] | None:
        return self.db["events"].get(event_id)

    # ------------------------------------------------------------------ 成品
    def add_product(self, product: dict[str, Any]) -> dict[str, Any]:
        return self._put(self.db["products"], product)

    def update_product(self, product: dict[str, Any]) -> None:
        self.db["products"][product["id"]] = product

    # ------------------------------------------------------------------ 质检
    def add_check(self, check: dict[str, Any]) -> dict[str, Any]:
        return self._put(self.db["checks"], check)

    # ------------------------------------------------------------------ 异常单
    def add_issue(self, issue: dict[str, Any]) -> dict[str, Any]:
        return self._put(self.db["issues"], issue)

    def update_issue(self, issue: dict[str, Any]) -> None:
        self.db["issues"][issue["id"]] = issue

    # ------------------------------------------------------------------ 处置
    def add_decision(self, decision: dict[str, Any]) -> None:
        self.db["decisions"].append(decision)

    def list_decisions(self, target_type: str, target_id: str) -> list[dict[str, Any]]:
        return [
            d
            for d in self.db["decisions"]
            if d["target_type"] == target_type and d["target_id"] == target_id
        ]

    def all_decisions(self) -> list[dict[str, Any]]:
        return list(self.db["decisions"])


def make_id(prefix: str) -> str:
    return new_id(prefix)
