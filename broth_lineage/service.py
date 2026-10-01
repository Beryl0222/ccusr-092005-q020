"""服务层：谱系事件受理、冻结守卫、异常圈定与处置、角色权限。

所有写操作都是带事件号的幂等事件，落 SQLite；服务重启后批次连续性
不依赖内存。门店离线补传统一走 :meth:`LineageService.sync_events`，
按 *发生时间* 排序后受理。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .errors import (
    DuplicateEventError,
    FrozenBrothError,
    IllegalTransitionError,
    PermissionDeniedError,
    UnknownBatchError,
)
from .store import LineageStore, canon

# ---------------------------------------------------------------- 常量

#: 会以汤底为父本产生新制作的事件；冻结/隔离/报废汤底一律禁止参与。
PRODUCTION_EVENTS = {"split", "topup", "merge", "heat", "filter", "transfer", "cook"}

BLOCKED_STATUSES = {"frozen", "quarantined", "destroyed"}
PASS_VERDICTS = {"normal", "pass"}
FAIL_VERDICTS = {"abnormal", "fail"}


class Role:
    MASTER = "master"        # 传承人：配方比例读写
    QC = "qc"                # 质控：配方比例只读、登记检测
    FSO = "fso"              # 食品安全负责人：处置决定
    STAFF = "staff"          # 门店员工：扫码上报
    CUSTOMER = "customer"    # 顾客：脱敏查询

    INTERNAL = {MASTER, QC, FSO, STAFF}
    RECIPE_READERS = {MASTER, QC}
    RECIPE_WRITERS = {MASTER}
    TEST_RECORDERS = {QC, MASTER}
    DISPOSERS = {FSO}
    EVENT_REPORTERS = {MASTER, QC, FSO, STAFF}


class Disposition:
    QUARANTINE = "quarantine"  # 隔离观察
    DESTROY = "destroy"        # 报废
    RELEASE = "release"        # 恢复（须复检合格）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------- 服务


class LineageService:
    def __init__(self, store: LineageStore) -> None:
        self.store = store
        self.conn = store.conn

    # ================================================================
    # 主数据登记
    # ================================================================

    def register_store(self, store_id: str, name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO stores(id, name) VALUES (?,?)", (store_id, name)
        )
        self.conn.commit()

    def register_ingredient(
        self,
        batch_id: str,
        category: str,
        name: str,
        allergens: list[str] | None = None,
        supplier: str | None = None,
        received_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO ingredient_batches
                   (id, category, name, supplier, received_at, allergens)
               VALUES (?,?,?,?,?,?)""",
            (
                batch_id,
                category,
                name,
                supplier,
                received_at or _now(),
                json.dumps(sorted(allergens or []), ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def register_recipe(
        self, id_: str, name: str, ratios: dict[str, float], role: str
    ) -> None:
        if role not in Role.RECIPE_WRITERS:
            raise PermissionDeniedError(role, "写入配方比例")
        self.conn.execute(
            "INSERT OR REPLACE INTO recipes(id, name, ratios, created_at)"
            " VALUES (?,?,?,?)",
            (id_, name, json.dumps(ratios, ensure_ascii=False), _now()),
        )
        self.conn.commit()

    def get_recipe(self, recipe_id: str, role: str) -> dict[str, Any]:
        """配方比例只向传承人和质控人员开放。"""
        row = self.conn.execute(
            "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        if row is None:
            raise UnknownBatchError("配方", recipe_id)
        result = {"id": row["id"], "name": row["name"]}
        if role in Role.RECIPE_READERS:
            result["ratios"] = json.loads(row["ratios"])
        else:
            raise PermissionDeniedError(role, "读取配方比例")
        return result

    def register_broth(
        self,
        broth_id: str,
        generation: int,
        store_id: str,
        occurred_at: str | None = None,
        note: str | None = None,
        status: str = "active",
    ) -> None:
        """登记一脉老汤的源头批次（引种/建档），非谱系变换。"""
        self.conn.execute(
            """INSERT OR IGNORE INTO broth_batches
                   (id, generation, store_id, status, created_at, note)
               VALUES (?,?,?,?,?,?)""",
            (broth_id, generation, store_id, status, occurred_at or _now(), note),
        )
        self.conn.commit()

    # ================================================================
    # 事件受理（幂等 + 离线补传）
    # ================================================================

    def sync_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """门店离线补传：按 *发生时间* 排序后逐条幂等受理。

        返回与入参等长的受理结果（event_id、是否去重命中）。即使某事件
        早已在线受理，重放也不产生第二条谱系边。
        """
        ordered = sorted(events, key=lambda e: (e["occurred_at"], e["event_id"]))
        results: list[dict[str, Any]] = []
        for event in ordered:
            results.append(self.ingest(event))
        return results

    def ingest(self, event: dict[str, Any]) -> dict[str, Any]:
        """受理单个事件。重复 event_id + 相同载荷 → 去重；载荷冲突 → 报错。"""
        role = event.get("role", Role.STAFF)
        if role not in Role.EVENT_REPORTERS:
            raise PermissionDeniedError(role, f"上报事件 {event['type']}")

        event_id = event["event_id"]
        type_ = event["type"]
        occurred_at = event["occurred_at"]
        recorded_at = event.get("recorded_at", _now())
        store_id = event.get("store_id")
        actor = event.get("actor")
        payload = event.get("payload", {})

        existing = self.store.get_event(event_id)
        if existing is not None:
            self._assert_same_event(existing, type_, occurred_at, store_id, payload)
            return {"event_id": event_id, "deduplicated": True, "replayed": False}

        try:
            self._apply(type_, payload, occurred_at, event_id)
            # 补传宽限：发生时间早于收件时间 60 秒以上即标记为离线补传
            replayed = (_parse(recorded_at) - _parse(occurred_at)).total_seconds() > 60
            self.store.insert_event(
                event_id, type_, occurred_at, recorded_at, store_id, actor,
                payload, replayed=replayed,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"event_id": event_id, "deduplicated": False, "replayed": replayed}

    @staticmethod
    def _assert_same_event(
        existing: Any, type_: str, occurred_at: str,
        store_id: str | None, payload: dict[str, Any],
    ) -> None:
        for field, incoming in (
            ("type", type_),
            ("occurred_at", occurred_at),
            ("store_id", store_id),
        ):
            if existing[field] != incoming:
                raise DuplicateEventError(existing["event_id"], field)
        if existing["payload"] != canon(payload):
            stored = json.loads(existing["payload"])
            clash = next(
                (k for k in set(stored) | set(payload) if stored.get(k) != payload.get(k)),
                "payload",
            )
            raise DuplicateEventError(existing["event_id"], clash)

    # ------------------------------------------------ 变换分发

    def _apply(
        self, type_: str, payload: dict[str, Any], occurred_at: str, event_id: str
    ) -> None:
        handler = {
            "split": self._ev_split,
            "topup": self._ev_topup,
            "merge": self._ev_merge,
            "heat": self._ev_heat,
            "filter": self._ev_filter,
            "transfer": self._ev_transfer,
            "receive": self._ev_receive,
            "cook": self._ev_cook,
        }.get(type_)
        if handler is None:
            raise ValueError(f"未知事件类型：{type_}")
        handler(payload, occurred_at, event_id)

    def _guard_active(self, broth_id: str) -> Any:
        row = self.conn.execute(
            "SELECT * FROM broth_batches WHERE id = ?", (broth_id,)
        ).fetchone()
        if row is None:
            raise UnknownBatchError("汤底", broth_id)
        if row["status"] in BLOCKED_STATUSES:
            raise FrozenBrothError(broth_id, {"frozen": "冻结",
                                              "quarantined": "隔离观察",
                                              "destroyed": "报废"}[row["status"]])
        return row

    def _create_broth(
        self, id_: str, generation: int, store_id: str, occurred_at: str,
        event_id: str, location_state: str = "on_site",
    ) -> None:
        self.conn.execute(
            """INSERT INTO broth_batches
                   (id, generation, store_id, location_state, status,
                    created_event_id, created_at)
               VALUES (?,?,?,?,'active',?,?)""",
            (id_, generation, store_id, location_state, event_id, occurred_at),
        )

    def _edge(
        self, event_id: str, relation: str,
        parent_type: str, parent_id: str, child_type: str, child_id: str,
        occurred_at: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO lineage_edges
                   (event_id, relation, parent_type, parent_id,
                    child_type, child_id, occurred_at)
               VALUES (?,?,?,?,?,?,?)""",
            (event_id, relation, parent_type, parent_id,
             child_type, child_id, occurred_at),
        )

    def _ev_split(self, p: dict[str, Any], ts: str, eid: str) -> None:
        parent = self._guard_active(p["parent_id"])
        for child in p["children"]:  # 分装：代际不变
            self._create_broth(child["id"], parent["generation"],
                               child.get("store_id", parent["store_id"]), ts, eid)
            self._edge(eid, "split", "broth", p["parent_id"],
                       "broth", child["id"], ts)

    def _ev_topup(self, p: dict[str, Any], ts: str, eid: str) -> None:
        parent = self._guard_active(p["parent_id"])  # 续汤：补汤后进入下一代
        self._create_broth(p["child_id"], parent["generation"] + 1,
                           p.get("store_id", parent["store_id"]), ts, eid)
        self._edge(eid, "topup", "broth", p["parent_id"],
                   "broth", p["child_id"], ts)

    def _ev_merge(self, p: dict[str, Any], ts: str, eid: str) -> None:
        parents = [self._guard_active(pid) for pid in p["parent_ids"]]  # 跨店合并
        generation = max(r["generation"] for r in parents) + 1
        self._create_broth(p["child_id"], generation, p["store_id"], ts, eid)
        for pr in parents:
            self._edge(eid, "merge", "broth", pr["id"],
                       "broth", p["child_id"], ts)

    def _ev_heat(self, p: dict[str, Any], ts: str, eid: str) -> None:
        parent = self._guard_active(p["parent_id"])  # 加热：同代版本
        self._create_broth(p["child_id"], parent["generation"],
                           parent["store_id"], ts, eid)
        self._edge(eid, "heat", "broth", p["parent_id"],
                   "broth", p["child_id"], ts)

    def _ev_filter(self, p: dict[str, Any], ts: str, eid: str) -> None:
        parent = self._guard_active(p["parent_id"])  # 过滤：同代版本
        self._create_broth(p["child_id"], parent["generation"],
                           parent["store_id"], ts, eid)
        self._edge(eid, "filter", "broth", p["parent_id"],
                   "broth", p["child_id"], ts)

    def _ev_transfer(self, p: dict[str, Any], ts: str, eid: str) -> None:
        parent = self._guard_active(p["parent_id"])  # 跨店移交：在途分装
        self._create_broth(p["child_id"], parent["generation"],
                           p["to_store_id"], ts, eid, location_state="in_transit")
        self._edge(eid, "transfer", "broth", p["parent_id"],
                   "broth", p["child_id"], ts)

    def _ev_receive(self, p: dict[str, Any], ts: str, eid: str) -> None:
        row = self.conn.execute(
            "SELECT * FROM broth_batches WHERE id = ?", (p["broth_id"],)
        ).fetchone()
        if row is None:
            raise UnknownBatchError("汤底", p["broth_id"])
        if row["status"] in BLOCKED_STATUSES:
            raise FrozenBrothError(p["broth_id"], "隔离/冻结")
        self.conn.execute(
            "UPDATE broth_batches SET location_state='on_site' WHERE id=?",
            (p["broth_id"],),
        )

    def _ev_cook(self, p: dict[str, Any], ts: str, eid: str) -> None:
        """烹制：老汤 + 实际原料批次 → 烹制批次 → 成品（消费批次）。"""
        broth = self._guard_active(p["broth_id"])
        for usage in p.get("ingredients", []):
            if not self.conn.execute(
                "SELECT 1 FROM ingredient_batches WHERE id=?",
                (usage["batch_id"],),
            ).fetchone():
                raise UnknownBatchError("原料", usage["batch_id"])

        self.conn.execute(
            """INSERT INTO cook_runs
                   (id, broth_id, store_id, recipe_id, temperature_c,
                    occurred_at, created_event_id)
               VALUES (?,?,?,?,?,?,?)""",
            (p["cook_run_id"], p["broth_id"],
             p.get("store_id", broth["store_id"]),
             p.get("recipe_id"), p.get("temperature_c"), ts, eid),
        )
        for usage in p.get("ingredients", []):
            self.conn.execute(
                "INSERT OR IGNORE INTO ingredient_usages"
                " (cook_run_id, ingredient_batch_id, role) VALUES (?,?,?)",
                (p["cook_run_id"], usage["batch_id"],
                 usage.get("role", "other")),
            )
            self._edge(eid, "use", "ingredient", usage["batch_id"],
                       "cook_run", p["cook_run_id"], ts)
        self._edge(eid, "cook", "broth", p["broth_id"],
                   "cook_run", p["cook_run_id"], ts)
        for product in p["products"]:
            self.conn.execute(
                """INSERT INTO finished_products
                       (id, cook_run_id, name, status, created_at)
                   VALUES (?,?,?,'active',?)""",
                (product["id"], p["cook_run_id"], product["name"], ts),
            )
            self._edge(eid, "cook", "cook_run", p["cook_run_id"],
                       "product", product["id"], ts)

    # ================================================================
    # 检测与异常圈定
    # ================================================================

    def add_test(
        self,
        test_id: str,
        target_type: str,
        target_id: str,
        kind: str,
        verdict: str,
        occurred_at: str,
        role: str,
        detail: dict[str, Any] | None = None,
        recorder: str | None = None,
        incident_id: str | None = None,
    ) -> str:
        """登记微生物/感官/温度检测。异常立即冻结被检汤底并开立异常单。"""
        if role not in Role.TEST_RECORDERS:
            raise PermissionDeniedError(role, "登记检测结果")
        self._assert_target_exists(target_type, target_id)

        created_incident = incident_id is None and verdict in FAIL_VERDICTS
        if created_incident:
            incident_id = f"inc-{uuid.uuid4().hex[:12]}"

        self.conn.execute(
            """INSERT INTO tests
                   (id, target_type, target_id, kind, verdict, detail,
                    occurred_at, recorder, incident_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (test_id, target_type, target_id, kind, verdict,
             json.dumps(detail or {}, ensure_ascii=False),
             occurred_at, recorder, incident_id),
        )

        if created_incident:
            self.conn.execute(
                """INSERT INTO incidents(id, trigger_test_id, state, opened_at)
                   VALUES (?,?, 'open', ?)""",
                (incident_id, test_id, occurred_at),
            )
            # 直接风险源立即冻结，防止风险扩大；其余支系等待负责人圈定处置
            if target_type == "broth":
                self._set_broth_status(target_id, "frozen")
            elif target_type == "product":
                self._set_product_status(target_id, "held")
                run = self.conn.execute(
                    "SELECT broth_id FROM finished_products p"
                    " JOIN cook_runs c ON c.id = p.cook_run_id"
                    " WHERE p.id=?", (target_id,),
                ).fetchone()
                if run:
                    self._set_broth_status(run["broth_id"], "frozen")
        self.conn.commit()
        return incident_id or ""

    def _assert_target_exists(self, target_type: str, target_id: str) -> None:
        table = {
            "broth": "broth_batches",
            "product": "finished_products",
            "ingredient": "ingredient_batches",
        }.get(target_type)
        if table is None:
            raise UnknownBatchError(target_type, target_id)
        if not self.conn.execute(
            f"SELECT 1 FROM {table} WHERE id=?", (target_id,)
        ).fetchone():
            raise UnknownBatchError(target_type, target_id)

    def incident_scope(self, incident_id: str) -> dict[str, Any]:
        """沿谱系圈定：成品、在途分装、可隔离观察的汤底支系。

        只收 *被检节点的后代*——合并而来的父母汤底与分出去的兄弟支系
        不在范围内，避免误伤无关支系。
        """
        incident = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)
        ).fetchone()
        if incident is None:
            raise UnknownBatchError("异常单", incident_id)
        trigger = self.conn.execute(
            "SELECT * FROM tests WHERE id=?", (incident["trigger_test_id"],)
        ).fetchone()

        root_type, root_id = trigger["target_type"], trigger["target_id"]
        if root_type == "product":
            # 成品异常：沿烹制边找到其所用老汤，整支系（同汤其他成品、
            # 后续代际）一并圈定；被检成品仍在范围内。
            broth_root = self.conn.execute(
                """SELECT e.parent_id FROM lineage_edges e
                   WHERE e.child_type='product' AND e.child_id=?
                     AND e.relation='cook'
                   UNION
                   SELECT e.parent_id FROM lineage_edges e
                   WHERE e.child_type='cook_run' AND e.relation='cook'
                     AND e.child_id IN (
                       SELECT cook_run_id FROM finished_products WHERE id=?)
                   LIMIT 1""",
                (root_id, root_id),
            ).fetchone()
            if broth_root:
                root_type, root_id = "broth", broth_root["parent_id"]

        broths: list[dict[str, Any]] = []
        products: list[dict[str, Any]] = []
        for node_type, node_id, depth in self.store.descendants(root_type, root_id):
            if node_type == "broth":
                row = self.conn.execute(
                    "SELECT * FROM broth_batches WHERE id=?", (node_id,)
                ).fetchone()
                broths.append({
                    "id": node_id, "depth": depth,
                    "status": row["status"],
                    "generation": row["generation"],
                    "store_id": row["store_id"],
                    "location_state": row["location_state"],
                    "in_transit": row["location_state"] == "in_transit",
                })
            elif node_type == "product":
                row = self.conn.execute(
                    "SELECT * FROM finished_products WHERE id=?", (node_id,)
                ).fetchone()
                products.append({
                    "id": node_id, "depth": depth,
                    "status": row["status"], "name": row["name"],
                })
        return {
            "incident_id": incident_id,
            "state": incident["state"],
            "trigger": {
                "test_id": trigger["id"],
                "target_type": trigger["target_type"],
                "target_id": trigger["target_id"],
                "kind": trigger["kind"],
                "verdict": trigger["verdict"],
            },
            "broths": sorted(broths, key=lambda b: (b["depth"], b["id"])),
            "products": sorted(products, key=lambda p: (p["depth"], p["id"])),
            "in_transit_broth_ids": [b["id"] for b in broths if b["in_transit"]],
        }

    # ------------------------------------------------ 处置决定

    def dispose(
        self,
        incident_id: str,
        action: str,
        batch_refs: Iterable[tuple[str, str]],
        role: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """食品安全负责人对圈定节点选择报废 / 隔离 / 恢复。

        batch_refs 为 (node_type, node_id)；恢复要求该节点在异常开立后
        已有复检合格记录。
        """
        if role not in Role.DISPOSERS:
            raise PermissionDeniedError(role, f"处置决定：{action}")
        if not self.conn.execute(
            "SELECT 1 FROM incidents WHERE id=?", (incident_id,)
        ).fetchone():
            raise UnknownBatchError("异常单", incident_id)

        refs = list(batch_refs)
        applied: list[dict[str, str]] = []
        for node_type, node_id in refs:
            if action == Disposition.QUARANTINE:
                if node_type == "broth":
                    self._set_broth_status(node_id, "quarantine")
                elif node_type == "product":
                    self._set_product_status(node_id, "held")
                else:
                    continue
            elif action == Disposition.DESTROY:
                if node_type == "broth":
                    self._set_broth_status(node_id, "destroyed")
                elif node_type == "product":
                    self._set_product_status(node_id, "destroyed")
                else:
                    continue
            elif action == Disposition.RELEASE:
                self._assert_retest_passed(incident_id, node_type, node_id)
                if node_type == "broth":
                    self._set_broth_status(node_id, "active")
                elif node_type == "product":
                    self._set_product_status(node_id, "active")
                else:
                    continue
            else:
                raise ValueError(f"未知处置：{action}")
            applied.append({"type": node_type, "id": node_id})

        disp_id = f"disp-{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO dispositions
                   (id, incident_id, action, scope, decided_by, decided_at, reason)
               VALUES (?,?,?,?,?,?,?)""",
            (disp_id, incident_id, action,
             json.dumps(applied, ensure_ascii=False), role, _now(), reason),
        )
        self.conn.commit()
        return {"disposition_id": disp_id, "action": action, "applied": applied}

    def _assert_retest_passed(
        self, incident_id: str, node_type: str, node_id: str
    ) -> None:
        incident = self.conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)
        ).fetchone()
        passed = self.conn.execute(
            """SELECT 1 FROM tests
               WHERE incident_id=? AND target_type=? AND target_id=?
                 AND verdict IN ('normal','pass')
                 AND occurred_at >= ?
               LIMIT 1""",
            (incident_id, node_type, node_id, incident["opened_at"]),
        ).fetchone()
        if not passed:
            raise IllegalTransitionError(
                f"{node_type} {node_id} 尚无异常开立后的复检合格记录，不能恢复"
            )

    def _set_broth_status(self, broth_id: str, status: str) -> None:
        if not self.conn.execute(
            "SELECT 1 FROM broth_batches WHERE id=?", (broth_id,)
        ).fetchone():
            raise UnknownBatchError("汤底", broth_id)
        self.conn.execute(
            "UPDATE broth_batches SET status=? WHERE id=?", (status, broth_id)
        )

    def _set_product_status(self, product_id: str, status: str) -> None:
        if not self.conn.execute(
            "SELECT 1 FROM finished_products WHERE id=?", (product_id,)
        ).fetchone():
            raise UnknownBatchError("成品", product_id)
        self.conn.execute(
            "UPDATE finished_products SET status=? WHERE id=?",
            (status, product_id),
        )

    def disposition_history(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM dispositions WHERE incident_id=? ORDER BY decided_at",
            (incident_id,),
        ).fetchall()
        return [
            {
                "id": r["id"], "action": r["action"],
                "scope": json.loads(r["scope"]),
                "decided_by": r["decided_by"], "decided_at": r["decided_at"],
                "reason": r["reason"],
            }
            for r in rows
        ]

    # ================================================================
    # 追溯与脱敏查询
    # ================================================================

    def trace(self, node_type: str, node_id: str, role: str) -> dict[str, Any]:
        """内部人员：从任一节点双向追到每代老汤、原料、检测与处置决定。"""
        if role not in Role.INTERNAL:
            raise PermissionDeniedError(role, "内部谱系追溯")
        self._assert_target_exists(node_type, node_id)

        def pack(nodes: list[tuple[str, str, int]], upstream: bool) -> list[dict[str, Any]]:
            out = []
            for t, i, depth in nodes:
                item: dict[str, Any] = {"type": t, "id": i, "depth": depth}
                if t == "broth":
                    r = self.conn.execute(
                        "SELECT * FROM broth_batches WHERE id=?", (i,)
                    ).fetchone()
                    item.update({
                        "generation": r["generation"], "status": r["status"],
                        "store_id": r["store_id"],
                        "location_state": r["location_state"],
                    })
                elif t == "product":
                    r = self.conn.execute(
                        "SELECT * FROM finished_products WHERE id=?", (i,)
                    ).fetchone()
                    item.update({"name": r["name"], "status": r["status"]})
                elif t == "ingredient":
                    r = self.conn.execute(
                        "SELECT * FROM ingredient_batches WHERE id=?", (i,)
                    ).fetchone()
                    item.update({
                        "name": r["name"], "category": r["category"],
                        "supplier": r["supplier"],
                        "allergens": json.loads(r["allergens"]),
                    })
                elif t == "cook_run":
                    r = self.conn.execute(
                        "SELECT * FROM cook_runs WHERE id=?", (i,)
                    ).fetchone()
                    item.update({
                        "temperature_c": r["temperature_c"],
                        "store_id": r["store_id"],
                        "recipe_id": r["recipe_id"],
                    })
                    if role in Role.RECIPE_READERS and r["recipe_id"]:
                        rr = self.conn.execute(
                            "SELECT ratios FROM recipes WHERE id=?",
                            (r["recipe_id"],),
                        ).fetchone()
                        if rr:
                            item["recipe_ratios"] = json.loads(rr["ratios"])
                out.append(item)
            return out

        ancestor_nodes = self.store.ancestors(node_type, node_id)
        descendant_nodes = self.store.descendants(node_type, node_id)
        graph = {(t, i) for t, i, _ in ancestor_nodes + descendant_nodes}

        tests = self.conn.execute(
            "SELECT * FROM tests WHERE target_type=? AND target_id=?"
            " ORDER BY occurred_at",
            (node_type, node_id),
        ).fetchall()

        # 异常单与处置决定沿整条谱系图汇总：任一祖先/后代被圈定都要可见
        incident_ids: set[str] = set()
        for t, i in graph:
            if t not in ("broth", "product"):
                continue
            for r in self.conn.execute(
                "SELECT DISTINCT incident_id FROM tests"
                " WHERE target_type=? AND target_id=? AND incident_id IS NOT NULL",
                (t, i),
            ).fetchall():
                incident_ids.add(r["incident_id"])
        incidents = [self.incident_scope(inc) for inc in sorted(incident_ids)]
        dispositions = []
        for inc in sorted(incident_ids):
            dispositions.extend(self.disposition_history(inc))
        dispositions.sort(key=lambda d: d["decided_at"])

        return {
            "node": {"type": node_type, "id": node_id},
            "ancestors": pack(ancestor_nodes, True),
            "descendants": pack(descendant_nodes, False),
            "tests": [
                {"id": t["id"], "kind": t["kind"], "verdict": t["verdict"],
                 "occurred_at": t["occurred_at"],
                 "incident_id": t["incident_id"],
                 "detail": json.loads(t["detail"])}
                for t in tests
            ],
            "incidents": incidents,
            "dispositions": dispositions,
        }

    def public_product_view(self, product_id: str) -> dict[str, Any]:
        """顾客凭消费批次查询：仅脱敏来源与过敏原说明，不含比例/供应商。"""
        product = self.conn.execute(
            "SELECT * FROM finished_products WHERE id=?", (product_id,)
        ).fetchone()
        if product is None:
            raise UnknownBatchError("成品", product_id)

        run = self.conn.execute(
            "SELECT * FROM cook_runs WHERE id=?", (product["cook_run_id"],)
        ).fetchone()
        usages = self.conn.execute(
            """SELECT ib.* FROM ingredient_usages u
               JOIN ingredient_batches ib ON ib.id = u.ingredient_batch_id
               WHERE u.cook_run_id=?""",
            (product["cook_run_id"],),
        ).fetchall()
        allergens = sorted({a for u in usages for a in json.loads(u["allergens"])})

        broths = [
            n for n in self.store.ancestors("product", product_id)
            if n[0] == "broth"
        ]
        generations = []
        store_ids = []
        for _, bid, _ in broths:
            r = self.conn.execute(
                "SELECT generation, store_id FROM broth_batches WHERE id=?", (bid,)
            ).fetchone()
            generations.append(r["generation"])
            store_ids.append(r["store_id"])
        store_names = []
        for sid in dict.fromkeys(store_ids):
            sr = self.conn.execute(
                "SELECT name FROM stores WHERE id=?", (sid,)
            ).fetchone()
            if sr and sr["name"] not in store_names:
                store_names.append(sr["name"])

        safety_notice = {
            "active": "该批次检测正常",
            "held": "该批次正在隔离复检，暂不建议食用",
            "destroyed": "该批次已报废，请勿食用并联系门店",
        }[product["status"]]

        return {
            "product": product["name"],
            "batch_hint": self._mask(product_id),
            "broth_heritage": (
                f"老汤第{min(generations)}代传承"
                if generations and min(generations) == max(generations)
                else (f"老汤第{min(generations)}代至第{max(generations)}代传承"
                      if generations else "当批新制汤底")
            ),
            "stores": store_names,
            "ingredients": [
                {"category": u["category"], "name": u["name"]} for u in usages
            ],
            "allergens": allergens,
            "allergen_notice": ("含：" + "、".join(allergens)) if allergens
                               else "未申报常见过敏原",
            "safety_notice": safety_notice,
        }

    @staticmethod
    def _mask(batch_id: str) -> str:
        head = batch_id[:6]
        return f"{head}***" if len(batch_id) > 6 else batch_id
