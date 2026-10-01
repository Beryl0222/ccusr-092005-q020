"""老汤谱系与烹制批次服务。

一次方法调用 = 一笔已落盘的业务事实。所有产生新一代汤底的操作
（分装/续汤/合并/过滤）都同时写入：

1. 一条 ``lineage_event``（操作履历，离线可幂等补传）；
2. 一个新的 ``broth_batch``，其 ``parent_ids`` 构成父子边；合并有多父。

加热与跨店移交不产生新一代，只在既有批次上追加履历；移交期间批次处于
``in_transit``，异常圈定时会被识别为“在途分装”。
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from typing import Any, Iterable

from .errors import Conflict, FrozenError, NotFound, ValidationError
from .models import (
    B_ACTIVE,
    B_DESTROYED,
    B_HELD,
    B_RELEASED,
    BATCH_SPAWNING_EVENTS,
    DECISION_DESTROY,
    DECISION_HOLD,
    DECISION_RELEASE,
    DECISION_RETEST,
    EV_COOK,
    EV_HEAT,
    EV_MERGE,
    EV_TRANSFER,
    FROZEN_STATUSES,
    INGREDIENT_KINDS,
    ISSUE_CLOSED,
    ISSUE_FLAVOR,
    ISSUE_MICROBE,
    ISSUE_OPEN,
    P_ACTIVE,
    P_CLEARED,
    P_DESTROYED,
    P_HELD,
    QC_FAIL,
    QC_PASS,
    RECIPE_ROLES,
    parse_ts,
    require_fields,
)
from .repository import Repository, make_id

_ALL_OPS = BATCH_SPAWNING_EVENTS | {EV_HEAT, EV_TRANSFER, EV_COOK}
_TERMINAL = frozenset({B_DESTROYED})


class BrothService:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ================================================================ 基础档案
    def add_store(self, store_id: str, name: str, region: str = "") -> dict[str, Any]:
        return self.repo.add_store(
            {"id": store_id, "name": name, "region": region, "active": True}
        )

    def add_material(
        self,
        kind: str,
        name: str,
        *,
        batch_no: str,
        supplier: str,
        origin_region: str = "",
        allergens: Iterable[str] = (),
        material_id: str | None = None,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        if kind not in INGREDIENT_KINDS:
            raise ValidationError(f"未知物料类型: {kind}")
        record = {
            "id": material_id or make_id("mat"),
            "kind": kind,
            "name": name,
            "batch_no": batch_no,
            "supplier": supplier,
            "origin_region": origin_region,
            "allergens": sorted(set(allergens)),
            "received_at": received_at,
        }
        return self.repo.add_material(record)

    def register_root_broth(
        self,
        store_id: str,
        *,
        broth_id: str | None = None,
        generation: int = 1,
        ingredients: Iterable[str] = (),
        recipe: dict[str, Any] | None = None,
        created_at: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        self._must_store(store_id)
        batch = {
            "id": broth_id or make_id("broth"),
            "generation": generation,
            "store_id": store_id,
            "parent_ids": [],
            "created_via": "root",
            "source_event_id": None,
            "created_at": created_at or _stamp(),
            "status": B_ACTIVE,
            "custody": {"status": "at_store", "store_id": store_id},
            "ingredients": list(ingredients),
            "note": note,
        }
        if recipe is not None:
            batch["recipe"] = recipe
        return self.repo.add_broth(batch)

    # ================================================================ 谱系操作
    def record_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """登记一次操作（含离线补传）。

        返回 ``{"event": ..., "created": bool, "deduplicated": bool}``：
        同一 ``event_id`` 重放或同一操作指纹的补传都不会重复建边。
        """
        require_fields(
            payload, ("event_id", "op", "store_id", "occurred_at")
        )
        op = payload["op"]
        if op not in _ALL_OPS:
            raise ValidationError(f"未知操作类型: {op}")
        self._must_store(payload["store_id"])

        sources = list(payload.get("source_broth_ids") or [])
        materials = _clean_materials(payload.get("materials"))
        fingerprint = _fingerprint(op, payload, sources, materials)

        existing = self.repo.find_event(payload["event_id"])
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise Conflict("同一扫码编号对应了不同的操作内容，拒绝覆盖")
            return {"event": existing, "created": False, "deduplicated": False}

        for ev in self.repo.list("events"):
            if ev["fingerprint"] == fingerprint:
                # 门店离线后重新生成了扫码编号，但操作时间与对象完全一致
                return {"event": ev, "created": False, "deduplicated": True}

        for m in materials:  # 绑定的必须是已建档的实际物料批次
            self.repo.get("materials", m["material_id"])

        if op == EV_COOK:
            event, record = self._do_cook(payload, sources, materials, fingerprint)
        elif op in BATCH_SPAWNING_EVENTS:
            event, record = self._do_spawn(op, payload, sources, materials, fingerprint)
        elif op == EV_HEAT:
            event, record = self._do_heat(payload, sources, materials, fingerprint)
        else:  # EV_TRANSFER
            event, record = self._do_transfer(payload, sources, fingerprint)

        self.repo.save()
        result: dict[str, Any] = {
            "event": event,
            "created": True,
            "deduplicated": False,
        }
        if record is not None:
            result["child_broth" if op in BATCH_SPAWNING_EVENTS else "product"] = record
        return result

    def receive_transfer(self, broth_id: str, *, at: str | None = None) -> dict[str, Any]:
        """接收门店确认一钵在途老汤入库。"""
        broth = self.repo.must_get_broth(broth_id)
        custody = broth["custody"]
        if custody["status"] != "in_transit":
            raise Conflict(f"老汤 {broth_id} 不在在途状态")
        if broth["status"] in FROZEN_STATUSES:
            raise FrozenError(f"老汤 {broth_id} 已冻结，不能接收入店")
        event_id = custody.get("transfer_event_id")
        event = self.repo.get("events", event_id) if event_id else None
        to_store = custody["to_store"]
        custody["status"] = "at_store"
        custody["store_id"] = to_store
        custody.pop("to_store", None)
        broth["store_id"] = to_store
        if event is not None:
            event["received_at"] = at or _stamp()
            event["custody_status"] = "received"
        self.repo.save()
        return {"event": event, "broth": broth}

    # ------------------------------------------------ 产生新一代的四类操作
    def _do_spawn(
        self,
        op: str,
        payload: dict[str, Any],
        sources: list[str],
        materials: list[dict[str, Any]],
        fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if op == EV_MERGE:
            if len(sources) < 2:
                raise ValidationError("合并至少需要两脉老汤")
        else:
            if len(sources) != 1:
                raise ValidationError(f"{op} 必须且只能指定一脉源老汤")
        for sid in sources:
            self._assert_usable(sid, op, payload["store_id"])

        child_id = payload.get("child_broth_id") or make_id("broth")
        generation = max(
            self.repo.must_get_broth(sid)["generation"] for sid in sources
        ) + 1
        ingredients = sorted(
            {
                ing
                for sid in sources
                for ing in self.repo.must_get_broth(sid).get("ingredients", [])
            }
        )
        child = {
            "id": child_id,
            "generation": generation,
            "store_id": payload["store_id"],
            "parent_ids": sources,
            "created_via": op,
            "source_event_id": payload["event_id"],
            "created_at": payload["occurred_at"],
            "recorded_at": _stamp(),
            "status": B_ACTIVE,
            "custody": {"status": "at_store", "store_id": payload["store_id"]},
            "ingredients": ingredients,
            "note": payload.get("note", ""),
        }
        recipe = payload.get("recipe")
        if recipe is not None:
            child["recipe"] = recipe
        self.repo.add_broth(child)

        event = _base_event(op, payload, sources, materials, fingerprint)
        event["child_broth_id"] = child_id
        event = self.repo.add_event(event)
        return event, child

    def _do_heat(
        self,
        payload: dict[str, Any],
        sources: list[str],
        materials: list[dict[str, Any]],
        fingerprint: str,
    ) -> tuple[dict[str, Any], None]:
        if len(sources) != 1:
            raise ValidationError("加热必须针对一脉老汤")
        broth = self._assert_usable(sources[0], EV_HEAT, payload["store_id"])
        temp = payload.get("temperature_c")
        if temp is None:
            raise ValidationError("加热必须记录温度（如八十摄氏度下料）")
        event = _base_event(EV_HEAT, payload, sources, materials, fingerprint)
        event["temperature_c"] = temp
        event["stage"] = payload.get("stage", "")  # 如 reach_80_add_ingredients
        event["broth_status_after"] = broth["status"]
        event = self.repo.add_event(event)
        return event, None

    def _do_transfer(
        self,
        payload: dict[str, Any],
        sources: list[str],
        fingerprint: str,
    ) -> tuple[dict[str, Any], None]:
        require_fields(payload, ("target_store_id",))
        if len(sources) != 1:
            raise ValidationError("一次移交只能针对一脉分装")
        if payload["target_store_id"] == payload["store_id"]:
            raise ValidationError("移交目标不能是本店")
        self._must_store(payload["target_store_id"])
        broth = self.repo.must_get_broth(sources[0])
        if broth["custody"]["status"] == "in_transit":
            raise Conflict(f"老汤 {broth['id']} 已在途，不能重复移交")
        if broth["status"] in FROZEN_STATUSES:
            raise FrozenError(f"老汤 {broth['id']} 已冻结，禁止移交")
        if broth["store_id"] != payload["store_id"] or broth["custody"]["status"] != "at_store":
            raise Conflict("只有当前持有门店可以发起移交")

        broth["custody"] = {
            "status": "in_transit",
            "store_id": payload["store_id"],
            "to_store": payload["target_store_id"],
            "transfer_event_id": payload["event_id"],
        }
        event = _base_event(EV_TRANSFER, payload, sources, [], fingerprint)
        event["target_store_id"] = payload["target_store_id"]
        event["custody_status"] = "in_transit"
        event = self.repo.add_event(event)
        return event, None

    def _do_cook(
        self,
        payload: dict[str, Any],
        sources: list[str],
        materials: list[dict[str, Any]],
        fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if len(sources) != 1:
            raise ValidationError("烹制必须指定一脉实际使用的老汤")
        items = payload.get("items") or payload.get("products")
        if not items:
            raise ValidationError("烹制批次至少包含一个成品（如狮子头）")
        broth = self._assert_usable(sources[0], EV_COOK, payload["store_id"])

        product_id = payload.get("product_id") or make_id("prod")
        product = {
            "id": product_id,
            "code": payload.get("code") or product_id,
            "store_id": payload["store_id"],
            "items": list(items),
            "broth_id": broth["id"],
            "broth_generation": broth["generation"],
            "event_id": payload["event_id"],
            "cooked_at": payload["occurred_at"],
            "recorded_at": _stamp(),
            "status": P_ACTIVE,
            "material_usages": materials,
            "temperature_c": payload.get("temperature_c"),
            "sensory_state": payload.get("sensory_state", "normal"),
            "note": payload.get("note", ""),
        }
        self.repo.add_product(product)

        event = _base_event(EV_COOK, payload, sources, materials, fingerprint)
        event["product_id"] = product_id
        event["temperature_c"] = payload.get("temperature_c")
        event = self.repo.add_event(event)
        return event, product

    # --------------------------------------------------------------- 质检
    def add_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        require_fields(payload, ("target_type", "target_id", "check_type", "result"))
        if payload["target_type"] not in ("broth", "product"):
            raise ValidationError("检测对象只能是 broth 或 product")
        kind = payload["target_type"]
        target = self.repo.get(kind + "s", payload["target_id"])
        if payload["result"] not in ("pass", "fail", "pending"):
            raise ValidationError("检测结论只能是 pass/fail/pending")

        check = {
            "id": make_id("chk"),
            "target_type": kind,
            "target_id": target["id"],
            "check_type": payload["check_type"],  # microbe / sensory / temperature
            "result": payload["result"],
            "value": payload.get("value"),
            "unit": payload.get("unit", ""),
            "recorder": payload.get("recorder", ""),
            "checked_at": payload.get("checked_at") or _stamp(),
            "note": payload.get("note", ""),
        }
        self.repo.add_check(check)

        auto_issue = None
        if check["result"] == QC_FAIL and check["check_type"] in (ISSUE_MICROBE, "sensory"):
            issue_type = (
                ISSUE_MICROBE if check["check_type"] == ISSUE_MICROBE else ISSUE_FLAVOR
            )
            auto_issue = self.open_issue(
                target_type=kind,
                target_id=target["id"],
                issue_type=issue_type,
                opener=payload.get("recorder", "qc"),
                note=f"由检测 {check['id']} 不合格自动立案：{check['note']}",
                check_id=check["id"],
            )["issue"]
        self.repo.save()
        return {"check": check, "issue": auto_issue}

    # --------------------------------------------------------------- 异常立案
    def open_issue(
        self,
        *,
        target_type: str,
        target_id: str,
        issue_type: str,
        opener: str,
        note: str = "",
        check_id: str | None = None,
    ) -> dict[str, Any]:
        if issue_type not in (ISSUE_MICROBE, ISSUE_FLAVOR):
            raise ValidationError("异常类型只能是 microbe 或 flavor")
        if target_type == "product":
            product = self.repo.get("products", target_id)
            root_broth_id = product["broth_id"]
        elif target_type == "broth":
            self.repo.must_get_broth(target_id)
            root_broth_id = target_id
        else:
            raise ValidationError("立案对象只能是 broth 或 product")

        # 同一根汤底已有未关闭的同类异常单 -> 沿用，避免重复圈定
        for issue in self.repo.list("issues"):
            if (
                issue["status"] == ISSUE_OPEN
                and issue["root_broth_id"] == root_broth_id
                and issue["issue_type"] == issue_type
            ):
                return {"issue": issue, "created": False}

        issue = {
            "id": make_id("iss"),
            "issue_type": issue_type,
            "status": ISSUE_OPEN,
            "root_broth_id": root_broth_id,
            "report_source": {"target_type": target_type, "target_id": target_id},
            "opened_by": opener,
            "opened_at": _stamp(),
            "closed_at": None,
            "note": note,
            "check_ids": [check_id] if check_id else [],
            "extra_targets": [],
        }
        scope = self.compute_scope(root_broth_id)
        issue["scope_snapshot"] = scope["summary"]
        issue["auto_held"] = []
        self.repo.add_issue(issue)

        # 立案即沿谱系冻结：可隔离支系与在途分装全部置为 held 并逐条留痕，
        # 在途分装保持在途状态同时被冻结，接收端将无法接收入店。
        for row in scope["branches"]["isolatable"] + scope["branches"]["in_transit"]:
            bid = row["broth_id"]
            broth = self.repo.must_get_broth(bid)
            broth["status"] = B_HELD
            self.repo.update_broth(broth)
            self.repo.add_decision(
                {
                    "id": make_id("dec"),
                    "issue_id": issue["id"],
                    "target_type": "broth",
                    "target_id": bid,
                    "action": DECISION_HOLD,
                    "by": opener,
                    "at": _stamp(),
                    "note": "异常立案自动隔离",
                    "previous_status": B_ACTIVE,
                }
            )
            issue["auto_held"].append(bid)
        for prow in scope["products"]:
            product = self.repo.get("products", prow["product_id"])
            if product["status"] == P_ACTIVE:
                product["status"] = P_HELD
                self.repo.update_product(product)
                self.repo.add_decision(
                    {
                        "id": make_id("dec"),
                        "issue_id": issue["id"],
                        "target_type": "product",
                        "target_id": product["id"],
                        "action": DECISION_HOLD,
                        "by": opener,
                        "at": _stamp(),
                        "note": "异常立案自动隔离成品",
                        "previous_status": P_ACTIVE,
                    }
                )
                issue["auto_held"].append(f"product:{product['id']}")
        self.repo.update_issue(issue)
        self.repo.save()
        return {"issue": issue, "created": True, "scope": self.compute_scope(root_broth_id)}

    def compute_scope(self, root_broth_id: str) -> dict[str, Any]:
        """沿谱系圈定：成品、在途分装与可隔离支系。

        范围分两层：

        * ``downstream`` —— 报告点及其全部后代（同脉直接支系），立案即自动冻结；
        * ``shared_ancestry`` —— 祖先老汤与旁支分装（共用一脉老汤，可能同源），
          只列出供食品安全负责人决定，不自动冻结，避免误伤尚未出现异常的支系。

        此外汇总各代使用的物料批次，找出“系外但共用同一物料批次”的潜在关联对象
        （原料批次问题会横向波及），作为第三层提示。
        """
        self.repo.must_get_broth(root_broth_id)
        downstream_ids = self._descendants(root_broth_id)
        ancestors = self._ancestors(root_broth_id)
        ancestor_ids = [b["id"] for b in ancestors]

        shared_ids: set[str] = set()
        for aid in ancestor_ids:
            shared_ids.update(self._descendants(aid))
        shared_ids.difference_update(downstream_ids)
        shared_ids.difference_update(ancestor_ids)

        products_down = [
            p for p in self.repo.list("products") if p["broth_id"] in downstream_ids
        ]
        shared_product_ids = {
            p["id"]
            for p in self.repo.list("products")
            if p["broth_id"] in shared_ids or p["broth_id"] in ancestor_ids
        }

        isolatable, in_transit, already_frozen, terminal, released = self._branch_rows(
            downstream_ids
        )
        shared_rows = [
            self._branch_row(self.repo.must_get_broth(bid))
            for bid in sorted(shared_ids)
            if self.repo.must_get_broth(bid)["status"] != B_DESTROYED
        ]
        upstream = [
            {
                "broth_id": b["id"],
                "generation": b["generation"],
                "store_id": b["store_id"],
                "status": b["status"],
                "custody": b["custody"]["status"],
            }
            for b in ancestors
        ]

        product_rows = [self._product_row(p) for p in products_down]
        shared_products = [
            self._product_row(p)
            for p in self.repo.list("products")
            if p["id"] in shared_product_ids and p["status"] != P_DESTROYED
        ]

        used_material_ids = self._materials_around(downstream_ids, products_down)
        related = self._material_siblings(
            used_material_ids, downstream_ids, products_down
        )

        scope = {
            "root_broth_id": root_broth_id,
            "downstream_broth_ids": downstream_ids,
            "upstream_broths": upstream,
            "shared_ancestry": {
                "broths": shared_rows,
                "products": shared_products,
            },
            "products": product_rows,
            "branches": {
                "isolatable": isolatable,
                "in_transit": in_transit,
                "already_held": already_frozen,
                "released": released,
                "destroyed": terminal,
            },
            "material_ids": sorted(used_material_ids),
            "possible_related_by_material": related,
        }
        scope["summary"] = {
            "downstream_broth_count": len(downstream_ids),
            "product_count": len(product_rows),
            "isolatable_count": len(isolatable),
            "in_transit_count": len(in_transit),
            "shared_ancestry_broth_count": len(shared_rows),
            "shared_ancestry_product_count": len(shared_products),
            "material_related_count": len(related),
        }
        return scope

    def _branch_row(self, b: dict[str, Any]) -> dict[str, Any]:
        return {
            "broth_id": b["id"],
            "generation": b["generation"],
            "store_id": b["store_id"],
            "status": b["status"],
            "custody": b["custody"]["status"],
        }

    def _branch_rows(
        self, broth_ids: list[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        isolatable, in_transit, already_frozen, terminal, released = [], [], [], [], []
        for bid in broth_ids:
            b = self.repo.must_get_broth(bid)
            row = self._branch_row(b)
            if b["status"] == B_DESTROYED:
                terminal.append(row)
            elif b["status"] == B_RELEASED:
                released.append(row)
            elif b["custody"]["status"] == "in_transit":
                in_transit.append(row)
            elif b["status"] == B_HELD:
                already_frozen.append(row)
            else:
                isolatable.append(row)
        return isolatable, in_transit, already_frozen, terminal, released

    def _product_row(self, p: dict[str, Any]) -> dict[str, Any]:
        return {
            "product_id": p["id"],
            "code": p["code"],
            "items": p["items"],
            "store_id": p["store_id"],
            "broth_id": p["broth_id"],
            "status": p["status"],
            "cooked_at": p["cooked_at"],
        }

    def decide(
        self,
        issue_id: str,
        *,
        target_type: str,
        target_id: str,
        action: str,
        by: str,
        note: str = "",
    ) -> dict[str, Any]:
        """食品安全负责人对圈定对象执行：hold / retest / destroy / release。"""
        issue = self.repo.get("issues", issue_id)
        if issue["status"] != ISSUE_OPEN:
            raise Conflict("异常单已关闭")
        if action not in (DECISION_HOLD, DECISION_RETEST, DECISION_DESTROY, DECISION_RELEASE):
            raise ValidationError(f"未知处置: {action}")
        target = self.repo.get(target_type + "s", target_id)

        scope = self.compute_scope(issue["root_broth_id"])
        scope_ids = set(scope["downstream_broth_ids"])
        scope_ids.update(row["broth_id"] for row in _all_scope_broths(scope))
        scope_ids.update(row["broth_id"] for row in scope["upstream_broths"])
        scope_ids.update(row["broth_id"] for row in scope["shared_ancestry"]["broths"])
        if target_type == "broth":
            in_scope = target_id in scope_ids
        else:
            product_ids = {row["product_id"] for row in scope["products"]}
            product_ids.update(
                row["product_id"] for row in scope["shared_ancestry"]["products"]
            )
            in_scope = target_id in product_ids
        related_ids = {
            f"{r['target_type']}:{r['target_id']}"
            for r in scope["possible_related_by_material"]
        }
        if not in_scope and f"{target_type}:{target_id}" not in related_ids:
            raise Conflict("处置对象不在本异常单的圈定范围内")

        if action == DECISION_RELEASE:
            self._assert_release_ok(issue, target_type, target)
            new_status = B_RELEASED if target_type == "broth" else P_CLEARED
        elif action == DECISION_DESTROY:
            new_status = B_DESTROYED if target_type == "broth" else P_DESTROYED
        else:  # hold / retest 都维持冻结
            new_status = B_HELD if target_type == "broth" else P_HELD

        decision = {
            "id": make_id("dec"),
            "issue_id": issue_id,
            "target_type": target_type,
            "target_id": target_id,
            "action": action,
            "by": by,
            "at": _stamp(),
            "note": note,
            "previous_status": target["status"],
        }
        self.repo.add_decision(decision)
        target["status"] = new_status
        if target_type == "broth":
            self.repo.update_broth(target)
            if action == DECISION_DESTROY:
                target["custody"] = {
                    "status": "disposed",
                    "store_id": target["custody"].get("store_id", target["store_id"]),
                }
        else:
            self.repo.update_product(target)

        key = f"{target_type}:{target_id}"
        if key not in issue["extra_targets"] and target_id != issue["root_broth_id"]:
            issue["extra_targets"].append(key)
        self.repo.update_issue(issue)
        self.repo.save()
        return {"decision": decision, "target": target}

    def close_issue(self, issue_id: str, *, by: str, note: str = "") -> dict[str, Any]:
        issue = self.repo.get("issues", issue_id)
        scope = self.compute_scope(issue["root_broth_id"])
        pending = (
            scope["branches"]["isolatable"]
            + scope["branches"]["in_transit"]
            + scope["branches"]["already_held"]
        )
        active_products = [
            p for p in scope["products"] if p["status"] not in (P_DESTROYED, P_CLEARED)
        ]
        if pending or active_products:
            raise Conflict("圈定范围内仍有未处置的支系或成品，不能关单")
        issue["status"] = ISSUE_CLOSED
        issue["closed_at"] = _stamp()
        issue["closed_by"] = by
        issue["close_note"] = note
        self.repo.update_issue(issue)
        self.repo.save()
        return {"issue": issue}

    # ================================================================ 追踪与视图
    def trace_product(self, product_id: str, *, role: str) -> dict[str, Any]:
        """内部正向+逆向追踪：任一成品 -> 每代老汤、检测与处置决定。"""
        product = self.repo.get("products", product_id)
        broth = self.repo.must_get_broth(product["broth_id"])

        generations = []
        queue = deque(broth["parent_ids"])
        seen = {broth["id"]}
        chain_nodes = [broth]
        while queue:
            pid = queue.popleft()
            if pid in seen:
                continue
            seen.add(pid)
            parent = self.repo.must_get_broth(pid)
            chain_nodes.append(parent)
            queue.extend(parent["parent_ids"])
        chain_nodes.sort(key=lambda b: b["generation"])

        for b in chain_nodes:
            generations.append(self._broth_generation_view(b, role))

        descendant_ids = self._descendants(broth["id"])
        forward = []
        for bid in descendant_ids:
            if bid == broth["id"]:
                continue
            db = self.repo.must_get_broth(bid)
            forward.append(
                {
                    "broth_id": bid,
                    "generation": db["generation"],
                    "status": db["status"],
                    "created_via": db["created_via"],
                    "store_id": db["store_id"],
                }
            )

        related_products = [
            {"product_id": p["id"], "items": p["items"], "status": p["status"]}
            for p in self.repo.list("products")
            if p["broth_id"] in descendant_ids
        ]
        issues = [
            {
                "issue_id": i["id"],
                "type": i["issue_type"],
                "status": i["status"],
                "root_broth_id": i["root_broth_id"],
            }
            for i in self.repo.list("issues")
            if i["root_broth_id"] in descendant_ids
        ]

        view = {
            "product": {
                "id": product["id"],
                "code": product["code"],
                "items": product["items"],
                "store_id": product["store_id"],
                "cooked_at": product["cooked_at"],
                "status": product["status"],
                "sensory_state": product.get("sensory_state"),
            },
            "root_line_id": generations[0]["broth_id"] if generations else None,
            "generations": generations,
            "forward_descendants": forward,
            "related_products": related_products,
            "issues": issues,
        }
        _attach_materials(view["product"], [product], self, role)
        return view

    def broth_view(self, broth_id: str, *, role: str) -> dict[str, Any]:
        broth = self.repo.must_get_broth(broth_id)
        view = self._broth_generation_view(broth, role)
        view["children"] = [
            b["id"] for b in self.repo.list("broths") if broth_id in b["parent_ids"]
        ]
        return view

    def customer_view(self, code: str) -> dict[str, Any]:
        """顾客凭消费批次仅获取脱敏来源与过敏原说明。"""
        product = next(
            (p for p in self.repo.list("products") if p["code"] == code), None
        )
        if product is None:
            raise NotFound("消费批次不存在")
        broth = self.repo.must_get_broth(product["broth_id"])
        store = self.repo.get("stores", product["store_id"])

        allergens, masked_sources = set(), []
        seen_mat = set()
        for usage in product.get("material_usages", []):
            mat = self.repo.get("materials", usage["material_id"])
            if mat["id"] in seen_mat:
                continue
            seen_mat.add(mat["id"])
            allergens.update(mat["allergens"])
            masked_sources.append(_mask_material(mat))

        root = self._root_of(broth["id"])
        root_store = self.repo.get("stores", root["store_id"])
        return {
            "batch_code": product["code"],
            "items": product["items"],
            "served_by": _mask_store(store),
            "broth": {
                "generation": broth["generation"],
                "line_origin": f"{_mask_text(root_store['name'])}一脉老汤",
                "safety_status": self._public_broth_status(broth, product),
            },
            "ingredients_summary": masked_sources,
            "allergens": sorted(allergens),
            "allergen_notice": ("含：" + "、".join(sorted(allergens))) if allergens else "未登记常见过敏原",
        }

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        issue = self.repo.get("issues", issue_id)
        view = dict(issue)
        view["scope"] = self.compute_scope(issue["root_broth_id"])
        view["decisions"] = [
            d for d in self.repo.all_decisions() if d["issue_id"] == issue_id
        ]
        return view

    def list_open_issues(self) -> list[dict[str, Any]]:
        return [i for i in self.repo.list("issues") if i["status"] == ISSUE_OPEN]

    # ================================================================ 内部辅助
    def _assert_usable(self, broth_id: str, op: str, store_id: str) -> dict[str, Any]:
        broth = self.repo.must_get_broth(broth_id)
        if broth["status"] in FROZEN_STATUSES:
            raise FrozenError(
                f"老汤 {broth_id} 当前状态 {broth['status']}，不能用于{op}"
            )
        custody = broth["custody"]
        if custody["status"] == "in_transit":
            raise FrozenError(f"老汤 {broth_id} 在途，须接收入店后才可操作")
        if custody["status"] == "at_store" and custody.get("store_id", broth["store_id"]) != store_id:
            raise Conflict(f"老汤 {broth_id} 不在门店 {store_id}，本店不能操作")
        return broth

    def _apply_status(self, target_type: str, target_id: str, status: str) -> None:
        if target_type == "broth":
            target = self.repo.must_get_broth(target_id)
            target["status"] = status
            self.repo.update_broth(target)
        else:
            target = self.repo.get("products", target_id)
            target["status"] = status
            self.repo.update_product(target)

    def _assert_release_ok(
        self, issue: dict[str, Any], target_type: str, target: dict[str, Any]
    ) -> None:
        opened_at = parse_ts(issue["opened_at"])
        checks = [
            c
            for c in self.repo.list("checks")
            if c["target_type"] == target_type
            and c["target_id"] == target["id"]
            and parse_ts(c["checked_at"]) >= opened_at
        ]
        passed = [c for c in checks if c["result"] == QC_PASS]
        if not passed:
            raise Conflict("恢复使用前必须有立案之后的复检合格记录")
        newest = max(checks, key=lambda c: parse_ts(c["checked_at"]))
        if newest["result"] != QC_PASS:
            raise Conflict("最新检测仍不合格，不能恢复")

    def _descendants(self, root_id: str) -> list[str]:
        children: dict[str, list[str]] = {}
        for b in self.repo.list("broths"):
            for pid in b["parent_ids"]:
                children.setdefault(pid, []).append(b["id"])
        result, queue, seen = [], deque([root_id]), {root_id}
        while queue:
            cur = queue.popleft()
            result.append(cur)
            for child in children.get(cur, []):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return result

    def _ancestors(self, broth_id: str) -> list[dict[str, Any]]:
        """沿父边向上的全部祖先（多父合并时包含全部祖先），按代数升序。"""
        result: dict[str, dict[str, Any]] = {}
        queue = deque([broth_id])
        seen = {broth_id}
        while queue:
            cur = queue.popleft()
            b = self.repo.must_get_broth(cur)
            for pid in b["parent_ids"]:
                if pid not in seen:
                    seen.add(pid)
                    parent = self.repo.must_get_broth(pid)
                    result[pid] = parent
                    queue.append(pid)
        return sorted(result.values(), key=lambda b: b["generation"])

    def _root_of(self, broth_id: str) -> dict[str, Any]:
        broth = self.repo.must_get_broth(broth_id)
        while broth["parent_ids"]:
            broth = self.repo.must_get_broth(broth["parent_ids"][0])
        return broth

    def _materials_around(
        self, broth_ids: list[str], products: list[dict[str, Any]]
    ) -> set[str]:
        ids: set[str] = set()
        event_index = {e["event_id"]: e for e in self.repo.list("events")}
        for bid in broth_ids:
            b = self.repo.must_get_broth(bid)
            ev = event_index.get(b.get("source_event_id") or "")
            if ev:
                ids.update(m["material_id"] for m in ev.get("materials", []))
        for p in products:
            ids.update(m["material_id"] for m in p.get("material_usages", []))
        return ids

    def _material_siblings(
        self,
        material_ids: set[str],
        scoped_broths: list[str],
        scoped_products: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """共用同批原料、但不在已圈定范围内的支系/成品。"""
        scoped_product_ids = {p["id"] for p in scoped_products}
        scoped_broth_set = set(scoped_broths)
        hits: dict[str, dict[str, Any]] = {}
        event_index = {e["event_id"]: e for e in self.repo.list("events")}
        for b in self.repo.list("broths"):
            ev = event_index.get(b.get("source_event_id") or "")
            if ev and material_ids & {m["material_id"] for m in ev.get("materials", [])}:
                if b["id"] not in scoped_broth_set and b["status"] != B_DESTROYED:
                    hits[f"broth:{b['id']}"] = {
                        "target_type": "broth",
                        "target_id": b["id"],
                        "store_id": b["store_id"],
                        "status": b["status"],
                        "reason": "shared_material_batch",
                    }
        for p in self.repo.list("products"):
            if p["id"] in scoped_product_ids:
                continue
            if material_ids & {m["material_id"] for m in p.get("material_usages", [])}:
                if p["status"] != P_DESTROYED:
                    hits[f"product:{p['id']}"] = {
                        "target_type": "product",
                        "target_id": p["id"],
                        "store_id": p["store_id"],
                        "status": p["status"],
                        "reason": "shared_material_batch",
                    }
        return list(hits.values())

    def _broth_generation_view(self, b: dict[str, Any], role: str) -> dict[str, Any]:
        view = {
            "broth_id": b["id"],
            "generation": b["generation"],
            "store_id": b["store_id"],
            "parent_ids": b["parent_ids"],
            "created_via": b["created_via"],
            "created_at": b.get("created_at"),
            "status": b["status"],
            "custody": b["custody"],
            "ingredients": b.get("ingredients", []),
        }
        event = None
        if b.get("source_event_id"):
            event = self.repo.find_event(b["source_event_id"])
        if event:
            view["operation"] = {
                "op": event["op"],
                "occurred_at": event["occurred_at"],
                "actor": event.get("actor"),
                "store_id": event["store_id"],
                "temperature_c": event.get("temperature_c"),
                "materials": _material_refs(event.get("materials", []), self.repo, role),
            }
            if event["op"] == EV_TRANSFER or b["custody"]["status"] in (
                "in_transit",
                "disposed",
            ):
                view["operation"]["target_store_id"] = event.get("target_store_id")
        view["checks"] = [
            _check_brief(c)
            for c in self.repo.list("checks")
            if c["target_type"] == "broth" and c["target_id"] == b["id"]
        ]
        view["timeline"] = [
            {
                "event_id": e["event_id"],
                "op": e["op"],
                "occurred_at": e["occurred_at"],
                "actor": e.get("actor"),
                "store_id": e["store_id"],
                "temperature_c": e.get("temperature_c"),
                "stage": e.get("stage"),
                "target_store_id": e.get("target_store_id"),
            }
            for e in self.repo.list("events")
            if e["op"] in ("heat", "transfer") and b["id"] in e["source_broth_ids"]
        ]
        view["decisions"] = [
            _decision_brief(d)
            for d in self.repo.list_decisions("broth", b["id"])
        ]
        if role in RECIPE_ROLES and "recipe" in b:
            view["recipe"] = deepcopy(b["recipe"])
        return view

    def _must_store(self, store_id: str) -> dict[str, Any]:
        return self.repo.get("stores", store_id)

    def _public_broth_status(self, broth: dict[str, Any], product: dict[str, Any]) -> str:
        if product["status"] == P_DESTROYED or broth["status"] == B_DESTROYED:
            return "该批次已按食品安全流程报废"
        if product["status"] in (P_HELD,) or broth["status"] == B_HELD:
            return "该批次正在复检观察"
        return "合格在售"


# ---------------------------------------------------------------- 模块级辅助
def _all_scope_broths(scope: dict[str, Any]) -> list[dict[str, Any]]:
    branches = scope["branches"]
    return (
        branches["isolatable"]
        + branches["in_transit"]
        + branches["already_held"]
        + branches["released"]
        + branches["destroyed"]
    )


def _stamp() -> str:
    from .models import now_ts

    return now_ts()


def _clean_materials(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    cleaned = []
    for item in raw:
        if isinstance(item, str):
            cleaned.append({"material_id": item, "qty": None})
        elif isinstance(item, dict) and item.get("material_id"):
            cleaned.append(
                {"material_id": item["material_id"], "qty": item.get("qty")}
            )
        else:
            raise ValidationError("materials 条目必须是物料批次 id 或对象")
    return cleaned


def _fingerprint(
    op: str,
    payload: dict[str, Any],
    sources: list[str],
    materials: list[dict[str, Any]],
) -> str:
    mat = ",".join(
        sorted(f"{m['material_id']}:{m['qty'] or ''}" for m in materials)
    )
    parts = [
        op,
        payload["store_id"],
        payload["occurred_at"],
        ",".join(sorted(sources)),
        mat,
        str(payload.get("target_store_id", "")),
        str(payload.get("temperature_c", "")),
        ",".join(sorted(payload.get("items") or payload.get("products") or [])),
        payload.get("code", ""),
    ]
    return "|".join(parts)


def _base_event(
    op: str,
    payload: dict[str, Any],
    sources: list[str],
    materials: list[dict[str, Any]],
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "event_id": payload["event_id"],
        "op": op,
        "store_id": payload["store_id"],
        "actor": payload.get("actor", ""),
        "occurred_at": payload["occurred_at"],
        "recorded_at": _stamp(),
        "source_broth_ids": sources,
        "materials": materials,
        "note": payload.get("note", ""),
        "fingerprint": fingerprint,
    }


def _material_refs(
    usages: list[dict[str, Any]], repo: Repository, role: str
) -> list[dict[str, Any]]:
    out = []
    for u in usages:
        mat = repo.get("materials", u["material_id"])
        row: dict[str, Any] = {
            "material_id": mat["id"],
            "kind": mat["kind"],
            "name": mat["name"],
            "batch_no": mat["batch_no"],
            "allergens": mat["allergens"],
            "qty": u.get("qty"),
        }
        if role in RECIPE_ROLES:
            row["supplier"] = mat["supplier"]
        out.append(row)
    return out


def _attach_materials(
    into: dict[str, Any], products: list[dict[str, Any]], svc: "BrothService", role: str
) -> None:
    into["material_usages"] = _material_refs(
        [u for p in products for u in p.get("material_usages", [])],
        svc.repo,
        role,
    )


def _check_brief(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_id": c["id"],
        "check_type": c["check_type"],
        "result": c["result"],
        "value": c.get("value"),
        "checked_at": c["checked_at"],
        "note": c.get("note", ""),
    }


def _decision_brief(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": d["id"],
        "issue_id": d["issue_id"],
        "action": d["action"],
        "by": d["by"],
        "at": d["at"],
        "note": d.get("note", ""),
    }


def _mask_text(text: str) -> str:
    if not text:
        return "***"
    if len(text) == 1:
        return text + "**"
    return text[0] + "*" * (len(text) - 1)


def _mask_store(store: dict[str, Any]) -> str:
    region = store.get("region") or "某"
    return f"{region}门店"


def _mask_material(mat: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": mat["kind"],
        "name": mat["name"],
        "source": f"{mat.get('origin_region') or '国内'}合规供应商",
        "batch": _mask_text(mat["batch_no"]),
    }
