"""谱系结构：分装/续汤/合并生成父子关系，移交与加热不另立一代。"""

from __future__ import annotations

from broth.errors import Conflict, FrozenError, ValidationError
from broth.models import B_HELD, EV_MERGE

from support import BrothTestCase


class LineageTest(BrothTestCase):
    def test_generations_and_parent_edges(self) -> None:
        root = self.repo.must_get_broth(self.root)
        s2 = self.repo.must_get_broth("broth-s2-0327")
        s28 = self.repo.must_get_broth("broth-s2-0328")
        merged = self.repo.must_get_broth(self.merge_broth)

        self.assertEqual(root["generation"], 326)
        self.assertEqual(s2["parent_ids"], [self.root])
        self.assertEqual(s28["generation"], 328)
        self.assertEqual(s28["parent_ids"], ["broth-s2-0327"])

        # 合并支系有两个父，世代取较高者 +1
        self.assertEqual(
            sorted(merged["parent_ids"]),
            ["broth-s1-0329", "broth-s3-0328"],
        )
        self.assertEqual(merged["generation"], 329)
        self.assertEqual(merged["created_via"], EV_MERGE)
        # 合并继承两脉全部香料名录
        self.assertIn("黄酱", merged["ingredients"])

    def test_merge_requires_two_parents(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.record_event(
                {
                    "event_id": "evt-bad-merge",
                    "op": "merge",
                    "store_id": "store-03",
                    "occurred_at": "2026-09-28T05:00:00Z",
                    "source_broth_ids": ["broth-merge-0330"],
                }
            )

    def test_heat_and_transfer_create_no_generation(self) -> None:
        before = len(self.repo.list("broths"))
        heat = self.svc.record_event(
            {
                "event_id": "evt-heat-extra",
                "op": "heat",
                "store_id": "store-01",
                "occurred_at": "2026-09-28T05:00:00Z",
                "source_broth_ids": [self.root],
                "temperature_c": 80,
                "stage": "reach_80_add_ingredients",
            }
        )
        self.assertEqual(len(self.repo.list("broths")), before)
        self.assertEqual(heat["event"]["temperature_c"], 80)

    def test_cannot_transfer_twice_or_transfer_frozen(self) -> None:
        # 先把一脉置为在途
        self.svc.record_event(
            {
                "event_id": "evt-tr1",
                "op": "divide",
                "store_id": "store-01",
                "occurred_at": "2026-09-28T06:00:00Z",
                "source_broth_ids": [self.root],
                "child_broth_id": "broth-transit-1",
            }
        )
        self.svc.record_event(
            {
                "event_id": "evt-tr2",
                "op": "transfer",
                "store_id": "store-01",
                "occurred_at": "2026-09-28T06:10:00Z",
                "source_broth_ids": ["broth-transit-1"],
                "target_store_id": "store-02",
            }
        )
        with self.assertRaises(Conflict):
            self.svc.record_event(
                {
                    "event_id": "evt-tr3",
                    "op": "transfer",
                    "store_id": "store-01",
                    "occurred_at": "2026-09-28T06:20:00Z",
                    "source_broth_ids": ["broth-transit-1"],
                    "target_store_id": "store-02",
                }
            )
        # 在途且被冻结的分装，接收端不能收入
        self.repo.must_get_broth("broth-transit-1")["status"] = B_HELD
        self.repo.save()
        with self.assertRaises(FrozenError):
            self.svc.receive_transfer("broth-transit-1")

    def test_filter_spawns_new_generation(self) -> None:
        result = self.svc.record_event(
            {
                "event_id": "evt-filter-1",
                "op": "filter",
                "store_id": "store-01",
                "occurred_at": "2026-09-28T07:00:00Z",
                "source_broth_ids": [self.root],
                "child_broth_id": "broth-filtered-327",
                "note": "纱布过滤一次",
            }
        )
        self.assertTrue(result["created"])
        child = result["child_broth"]
        self.assertEqual(child["parent_ids"], [self.root])
        self.assertEqual(child["generation"], 327)
