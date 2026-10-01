"""端到端场景：跨店合并谱系、重复扫码去重、异常支系圈定与处置、
冻结守卫、重启连续性、角色权限与顾客脱敏。"""

import unittest
from pathlib import Path
import tempfile

from project_data import build_service, load_seed
from broth_lineage import (
    LineageService, Disposition, Role,
    DuplicateEventError, FrozenBrothError, PermissionDeniedError,
    IllegalTransitionError,
)
from broth_lineage.store import LineageStore
from broth_lineage.errors import UnknownBatchError


def fresh_service():
    return build_service(":memory:")


class CrossStoreMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_service()
        self.store = self.svc.store

    def test_merge_carries_both_parents_and_generation(self):
        """跨店合并：两家店的汤底成为同一新批次的父本，代际取高者+1。"""
        parents = self.store.execute(
            """SELECT parent_id FROM lineage_edges
               WHERE child_id='broth-merge-m1' AND relation='merge'
               ORDER BY parent_id"""
        ).fetchall()
        self.assertEqual(
            [r["parent_id"] for r in parents],
            ["broth-a-t03", "broth-b-t03"],
        )
        merged = self.store.execute(
            "SELECT generation, store_id FROM broth_batches WHERE id='broth-merge-m1'"
        ).fetchone()
        # 总店支 326 代、城南支 210 代 → 合并后 327
        self.assertEqual(merged["generation"], 327)
        self.assertEqual(merged["store_id"], "store-03")

    def test_finished_product_traces_back_every_generation(self):
        """从任一成品可追到每代老汤与全部原料、温度记录。"""
        trace = self.svc.trace("product", "prod-lion-0923", Role.QC)
        broth_chain = {a["id"] for a in trace["ancestors"] if a["type"] == "broth"}
        # 合并后的狮子头同时挂在两脉老汤下
        self.assertIn("broth-merge-m1", broth_chain)
        self.assertIn("broth-a-t03", broth_chain)       # 总店移交支
        self.assertIn("broth-b-t03", broth_chain)       # 城南移交支
        self.assertIn("broth-lineage-main", broth_chain)  # 总店 326 代源头
        self.assertIn("broth-store02-root", broth_chain)  # 城南 210 代源头
        ingredients = {a["id"] for a in trace["ancestors"] if a["type"] == "ingredient"}
        self.assertIn("ing-meat-m09", ingredients)  # 狮子头肉馅实际批次
        runs = [a for a in trace["ancestors"] if a["type"] == "cook_run"]
        self.assertEqual(runs[0]["temperature_c"], 80)  # 八十摄氏度下料
        # 质控可见配方比例
        self.assertIn("recipe_ratios", runs[0])

    def test_staff_cannot_see_recipe_ratios(self):
        trace = self.svc.trace("product", "prod-lion-0923", Role.STAFF)
        runs = [a for a in trace["ancestors"] if a["type"] == "cook_run"]
        self.assertNotIn("recipe_ratios", runs[0])
        with self.assertRaises(PermissionDeniedError):
            self.svc.get_recipe("recipe-beng-broth", Role.STAFF)
        recipe = self.svc.get_recipe("recipe-beng-broth", Role.MASTER)
        self.assertEqual(recipe["ratios"]["草果"], 1.2)


class DeduplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_service()

    def _scan(self, **overrides):
        event = {
            "event_id": "evt-rescan-x1", "type": "split", "store_id": "store-01",
            "actor": "张师傅", "role": "staff",
            "occurred_at": "2026-09-25T09:00:00+08:00",
            "recorded_at": "2026-09-25T09:00:00+08:00",
            "payload": {"parent_id": "broth-main-g327",
                        "children": [{"id": "broth-rescan-x", "store_id": "store-01"}]},
        }
        event.update(overrides)
        return event

    def test_repeated_scan_is_idempotent(self):
        r1 = self.svc.ingest(self._scan())
        self.assertFalse(r1["deduplicated"])
        r2 = self.svc.ingest(self._scan(recorded_at="2026-09-25T12:00:00+08:00"))
        self.assertTrue(r2["deduplicated"])
        # 只生成一对父子边、一个新汤底
        edges = self.svc.store.edges_for_event("evt-rescan-x1")
        self.assertEqual(len(edges), 1)

    def test_same_event_id_with_conflicting_payload_rejected(self):
        self.svc.ingest(self._scan())
        bad = self._scan(
            payload={"parent_id": "broth-main-g327",
                     "children": [{"id": "broth-different", "store_id": "store-01"}]},
        )
        with self.assertRaises(DuplicateEventError):
            self.svc.ingest(bad)
        # 冲突事件不产生任何边或汤底
        self.assertIsNone(self.svc.store.execute(
            "SELECT 1 FROM broth_batches WHERE id='broth-different'"
        ).fetchone())

    def test_offline_backfill_applied_by_occurrence_time(self):
        """离线补传：晚发生的事件先到、早发生的后到，仍按发生时间生效，
        且不会在服务重启后打断连续性。"""
        events = [
            {
                "event_id": "evt-late", "type": "topup", "store_id": "store-02",
                "actor": "王师傅", "role": "staff",
                "occurred_at": "2026-09-26T10:00:00+08:00",
                "recorded_at": "2026-09-26T18:00:00+08:00",
                "payload": {"parent_id": "broth-b2", "child_id": "broth-b-late"},
            },
            {
                "event_id": "evt-early", "type": "split", "store_id": "store-02",
                "actor": "王师傅", "role": "staff",
                "occurred_at": "2026-09-26T08:00:00+08:00",
                "recorded_at": "2026-09-26T18:05:00+08:00",
                "payload": {"parent_id": "broth-b2",
                            "children": [{"id": "broth-b-early", "store_id": "store-02"}]},
            },
        ]
        results = self.svc.sync_events(events)
        self.assertEqual([r["event_id"] for r in results], ["evt-early", "evt-late"])
        self.assertTrue(all(r["replayed"] for r in results))
        # 再次全量补传 → 全部去重，谱系不产生重复边
        again = self.svc.sync_events(events)
        self.assertTrue(all(r["deduplicated"] for r in again))
        self.assertEqual(self.svc.store.execute(
            "SELECT COUNT(*) AS n FROM lineage_edges WHERE event_id IN ('evt-early','evt-late')"
        ).fetchone()["n"], 2)


class FrozenGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_service()

    def test_frozen_broth_cannot_enter_new_production(self):
        """感官异常后合并汤底已冻结：分装/续汤/合并/烹制一律拒绝。"""
        merged = self.svc.store.execute(
            "SELECT status FROM broth_batches WHERE id='broth-merge-m1'"
        ).fetchone()
        self.assertEqual(merged["status"], "frozen")

        def attempt(event_id, type_, payload, ts):
            with self.assertRaises(FrozenBrothError):
                self.svc.ingest({
                    "event_id": event_id, "type": type_, "store_id": "store-03",
                    "role": "staff", "occurred_at": ts, "payload": payload,
                })

        attempt("evt-x1", "split",
                {"parent_id": "broth-merge-m1",
                 "children": [{"id": "bx", "store_id": "store-03"}]},
                "2026-09-24T12:00:00+08:00")
        attempt("evt-x2", "topup",
                {"parent_id": "broth-merge-m1", "child_id": "bx2"},
                "2026-09-24T12:01:00+08:00")
        attempt("evt-x3", "cook",
                {"cook_run_id": "run-x", "broth_id": "broth-merge-m1",
                 "ingredients": [], "products": [{"id": "px", "name": "甏肉"}]},
                "2026-09-24T12:02:00+08:00")
        # 被拒绝的事件不落库，恢复后可原 event_id 重新扫码
        self.assertIsNone(self.svc.store.get_event("evt-x1"))

    def test_unrelated_branch_keeps_working(self):
        """无关支系（总店 g327）不被误伤，仍可正常烹制。"""
        self.svc.ingest({
            "event_id": "evt-cook-0103", "type": "cook", "store_id": "store-01",
            "role": "staff", "occurred_at": "2026-09-24T09:00:00+08:00",
            "payload": {
                "cook_run_id": "cook-run-store-01-03", "broth_id": "broth-main-g327",
                "temperature_c": 80,
                "ingredients": [{"batch_id": "ing-pork-p09", "role": "meat"}],
                "products": [{"id": "prod-beng-0924", "name": "甏肉"}],
            },
        })
        self.assertEqual(self.svc.store.execute(
            "SELECT status FROM finished_products WHERE id='prod-beng-0924'"
        ).fetchone()["status"], "active")


class IncidentContainmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_service()
        self.incident_id = self.svc.store.execute(
            "SELECT incident_id FROM tests WHERE id='test-sensory-lion-0923'"
        ).fetchone()["incident_id"]

    def test_scope_covers_products_and_in_transit_portions_only(self):
        scope = self.svc.incident_scope(self.incident_id)
        broth_ids = {b["id"] for b in scope["broths"]}
        product_ids = {p["id"] for p in scope["products"]}

        # 合并支系：合并汤底 + 在途移交分装
        self.assertIn("broth-merge-m1", broth_ids)
        self.assertIn("broth-m2-t01", broth_ids)
        self.assertEqual(scope["in_transit_broth_ids"], ["broth-m2-t01"])
        # 同汤所有成品（含未被投诉的甏肉、豆腐泡）一并圈定
        self.assertEqual(
            product_ids,
            {"prod-beng-0923", "prod-lion-0923", "prod-tofu-0923"},
        )
        # 不误伤：两脉父母汤底、两家门店此前批次、其他成品
        self.assertNotIn("broth-a-t03", broth_ids)
        self.assertNotIn("broth-b-t03", broth_ids)
        self.assertNotIn("broth-lineage-main", broth_ids)
        self.assertNotIn("prod-beng-0919", product_ids)
        self.assertNotIn("prod-beng-0920", product_ids)
        self.assertNotIn("prod-beng-0922", product_ids)

    def test_only_fso_can_decide_disposition(self):
        scope = self.svc.incident_scope(self.incident_id)
        held = [("product", p["id"]) for p in scope["products"]]
        with self.assertRaises(PermissionDeniedError):
            self.svc.dispose(self.incident_id, Disposition.DESTROY, held,
                             role=Role.QC, reason="质控无权报废")

    def test_destroy_retest_release_workflow(self):
        scope = self.svc.incident_scope(self.incident_id)
        in_transit = scope["in_transit_broth_ids"]
        products = [p["id"] for p in scope["products"]]

        # 1) 负责人选择：问题成品报废；在途分装隔离观察
        self.svc.dispose(self.incident_id, Disposition.DESTROY,
                         [("product", pid) for pid in products],
                         role=Role.FSO, reason="感官异常批次成品销毁")
        self.svc.dispose(self.incident_id, Disposition.QUARANTINE,
                         [("broth", in_transit[0])],
                         role=Role.FSO, reason="在途分装隔离等待微生物复检")
        self.assertEqual(self.svc.store.execute(
            "SELECT status FROM finished_products WHERE id='prod-lion-0923'"
        ).fetchone()["status"], "destroyed")
        self.assertEqual(self.svc.store.execute(
            "SELECT status FROM broth_batches WHERE id='broth-m2-t01'"
        ).fetchone()["status"], "quarantine")

        # 2) 无复检合格记录不能恢复
        with self.assertRaises(IllegalTransitionError):
            self.svc.dispose(self.incident_id, Disposition.RELEASE,
                             [("broth", "broth-m2-t01")], role=Role.FSO)

        # 3) 微生物复检合格后恢复，支系重新可用
        self.svc.add_test(
            "test-micro-m2-retest", "broth", "broth-m2-t01",
            "microbial", "normal", "2026-09-25T10:00:00+08:00",
            role=Role.QC, detail={"cfu_g": 1200, "limit": 100000},
            incident_id=self.incident_id,
        )
        self.svc.dispose(self.incident_id, Disposition.RELEASE,
                         [("broth", "broth-m2-t01")], role=Role.FSO,
                         reason="复检合格恢复使用")
        self.assertEqual(self.svc.store.execute(
            "SELECT status FROM broth_batches WHERE id='broth-m2-t01'"
        ).fetchone()["status"], "active")

        # 恢复后可用于新制作
        self.svc.ingest({
            "event_id": "evt-cook-m2-released", "type": "cook",
            "store_id": "store-01", "role": "staff",
            "occurred_at": "2026-09-25T11:00:00+08:00",
            "payload": {
                "cook_run_id": "run-m2-released", "broth_id": "broth-m2-t01",
                "temperature_c": 80,
                "ingredients": [{"batch_id": "ing-pork-p09", "role": "meat"}],
                "products": [{"id": "prod-m2-ok", "name": "甏肉"}],
            },
        })

        # 处置决定全部留痕，可从成品追溯到
        history = self.svc.disposition_history(self.incident_id)
        self.assertEqual([h["action"] for h in history],
                         ["destroy", "quarantine", "release"])
        trace = self.svc.trace("product", "prod-m2-ok", Role.QC)
        actions = {d["action"] for d in trace["dispositions"]}
        self.assertEqual(actions, {"destroy", "quarantine", "release"})

    def test_destroyed_broth_never_returns_to_production(self):
        self.svc.dispose(self.incident_id, Disposition.DESTROY,
                         [("broth", "broth-m2-t01")], role=Role.FSO)
        with self.assertRaises(FrozenBrothError):
            self.svc.ingest({
                "event_id": "evt-after-destroy", "type": "cook",
                "store_id": "store-01", "role": "staff",
                "occurred_at": "2026-09-25T12:00:00+08:00",
                "payload": {
                    "cook_run_id": "run-dead", "broth_id": "broth-m2-t01",
                    "ingredients": [], "products": [{"id": "pdead", "name": "甏肉"}],
                },
            })


class PersistenceTest(unittest.TestCase):
    def test_restart_keeps_lineage_continuity_and_freeze_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lineage.db"
            svc = build_service(db)
            incident = svc.store.execute(
                "SELECT incident_id FROM tests WHERE id='test-sensory-lion-0923'"
            ).fetchone()["incident_id"]
            svc.dispose(incident, Disposition.QUARANTINE,
                        [("broth", "broth-m2-t01")], role=Role.FSO,
                        reason="重启前隔离")
            svc.store.close()

            # 模拟服务重启：新建服务指向同一数据库
            reopened = LineageService(LineageStore(db))
            self.assertEqual(reopened.store.execute(
                "SELECT status FROM broth_batches WHERE id='broth-merge-m1'"
            ).fetchone()["status"], "frozen")
            self.assertEqual(reopened.store.execute(
                "SELECT status FROM broth_batches WHERE id='broth-m2-t01'"
            ).fetchone()["status"], "quarantine")
            # 补传老事件仍去重
            seed = load_seed()
            results = reopened.sync_events(seed["events"])
            self.assertTrue(all(r["deduplicated"] for r in results))
            # 谱系完整：成品仍能追到 326 代源头
            ancestors = {a["id"] for a in reopened.trace(
                "product", "prod-lion-0923", Role.QC)["ancestors"]}
            self.assertIn("broth-lineage-main", ancestors)
            reopened.store.close()


class CustomerViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_service()

    def test_public_view_is_masked_and_lists_allergens(self):
        view = self.svc.public_product_view("prod-lion-0919")
        self.assertEqual(view["product"], "狮子头")
        self.assertTrue(view["batch_hint"].endswith("***"))
        self.assertIn("326", view["broth_heritage"])
        self.assertIn("高铁新城分店", view["stores"])
        # 蛋（肉馅）、大豆/麸质（黄酱）全部汇总脱敏呈现
        self.assertEqual(set(view["allergens"]), {"蛋", "大豆", "麸质"})
        self.assertIn("蛋", view["allergen_notice"])
        # 不泄露供应商、配方比例、内部批次全号
        rendered = repr(view)
        self.assertNotIn("梁山", rendered)
        self.assertNotIn("recipe", rendered)
        self.assertNotIn("ing-meat-m09", rendered)

    def test_public_view_reflects_safety_state(self):
        ok = self.svc.public_product_view("prod-beng-0920")
        self.assertIn("正常", ok["safety_notice"])
        held = self.svc.public_product_view("prod-lion-0923")
        self.assertIn("复检", held["safety_notice"])

    def test_customer_cannot_trace_internal_lineage(self):
        with self.assertRaises(PermissionDeniedError):
            self.svc.trace("product", "prod-lion-0923", Role.CUSTOMER)

    def test_unknown_product(self):
        with self.assertRaises(UnknownBatchError):
            self.svc.public_product_view("prod-nope")


class SeedShapeTest(unittest.TestCase):
    def test_seed_has_named_records(self) -> None:
        data = load_seed()
        self.assertTrue(data["project"])
        self.assertTrue(data["records"])
        for record in data["records"]:
            self.assertTrue(record["id"])


if __name__ == "__main__":
    unittest.main()
