"""异常圈定与处置：狮子头风味异常 -> 自动隔离下游/在途 -> 报废/复检/恢复 -> 关单。

同时验证：
* 无关支系（邹城独立老汤、曲阜共用祖先支系）不被自动误伤；
* 同批原料横向关联只作提示，由质控人工决定；
* 任一成品可逆向追到每代老汤与全部处置决定。
"""

from __future__ import annotations

from broth.errors import Conflict, FrozenError
from broth.models import (
    B_ACTIVE,
    B_DESTROYED,
    B_HELD,
    B_RELEASED,
    P_CLEARED,
    P_DESTROYED,
    P_HELD,
)

from support import BrothTestCase


class IssueFlowTest(BrothTestCase):
    def setUp(self) -> None:
        super().setUp()
        # 立案前：从兖州支系再分一脉，发往曲阜但尚未接收（在途分装）
        self.svc.record_event(
            {
                "event_id": "evt-transit-x-divide",
                "op": "divide",
                "store_id": "store-03",
                "occurred_at": "2026-09-27T08:00:00Z",
                "source_broth_ids": ["broth-s3-0328"],
                "child_broth_id": "broth-transit-x",
            }
        )
        self.svc.record_event(
            {
                "event_id": "evt-transit-x-transfer",
                "op": "transfer",
                "store_id": "store-03",
                "occurred_at": "2026-09-27T08:30:00Z",
                "source_broth_ids": ["broth-transit-x"],
                "target_store_id": "store-02",
            }
        )

    def _open_flavor_issue(self) -> str:
        # 兖州 26 日批次狮子头被反馈风味异常，质检感官检测不合格 -> 自动立案
        result = self.svc.add_check(
            {
                "target_type": "product",
                "target_id": "prod-s3-0926",
                "check_type": "sensory",
                "result": "fail",
                "value": "风味异常",
                "recorder": "林质检",
                "note": "狮子头风味偏离基线",
            }
        )
        self.assertIsNotNone(result["issue"])
        return result["issue"]["id"]

    def test_open_issue_auto_quarantines_downstream_and_in_transit(self) -> None:
        issue_id = self._open_flavor_issue()
        issue = self.svc.get_issue(issue_id)
        scope = issue["scope"]

        # 报告点及其后代全部冻结
        self.assertEqual(
            self.repo.must_get_broth("broth-s3-0328")["status"], B_HELD
        )
        self.assertEqual(self.repo.must_get_broth("broth-merge-0330")["status"], B_HELD)
        self.assertEqual(self.repo.get("products", "prod-s3-0926")["status"], P_HELD)
        self.assertEqual(self.repo.get("products", "prod-s3-0927")["status"], P_HELD)

        # 在途分装被识别并冻结，接收端无法收入
        transit = self.repo.must_get_broth("broth-transit-x")
        self.assertEqual(transit["status"], B_HELD)
        self.assertEqual(transit["custody"]["status"], "in_transit")
        with self.assertRaises(FrozenError):
            self.svc.receive_transfer("broth-transit-x")
        self.assertEqual(scope["branches"]["in_transit"][0]["broth_id"], "broth-transit-x")

        # 冻结后不能进入任何新制作
        with self.assertRaises(FrozenError):
            self.svc.record_event(
                {
                    "event_id": "evt-cook-blocked",
                    "op": "cook",
                    "store_id": "store-03",
                    "occurred_at": "2026-09-27T09:00:00Z",
                    "source_broth_ids": ["broth-merge-0330"],
                    "items": ["狮子头"],
                }
            )

    def test_unrelated_branches_not_auto_frozen(self) -> None:
        self._open_flavor_issue()
        # 共用祖先老汤的曲阜支系：只列入观察名单，不自动冻结
        self.assertEqual(self.repo.must_get_broth("broth-s2-0328")["status"], B_ACTIVE)
        # 总店祖汤仍可继续传承工艺
        self.assertEqual(self.repo.must_get_broth(self.root)["status"], B_ACTIVE)
        # 邹城独立开坛的不同源支系完全不受影响
        self.assertEqual(self.repo.must_get_broth("broth-s4-013")["status"], B_ACTIVE)
        self.assertEqual(self.repo.get("products", "prod-s4-0926")["status"], "active")

        scope = self.svc.list_open_issues()[0]
        full = self.svc.get_issue(scope["id"])["scope"]
        shared_ids = {b["broth_id"] for b in full["shared_ancestry"]["broths"]}
        self.assertIn("broth-s2-0328", shared_ids)
        shared_product_codes = {p["code"] for p in full["shared_ancestry"]["products"]}
        self.assertIn("SB-20260924-02", shared_product_codes)

        # 横向关联：邹城成品用了同批五花肉/狮子头馅，作为原料疑点提示
        related = {
            (r["target_type"], r["target_id"])
            for r in full["possible_related_by_material"]
        }
        self.assertIn(("product", "prod-s4-0926"), related)

    def test_decisions_destroy_retest_release_and_close(self) -> None:
        issue_id = self._open_flavor_issue()

        # 1) 在途分装直接报废
        self.svc.decide(
            issue_id,
            target_type="broth",
            target_id="broth-transit-x",
            action="destroy",
            by="林质检",
            note="在途无法复检，报废",
        )
        self.assertEqual(
            self.repo.must_get_broth("broth-transit-x")["status"], B_DESTROYED
        )

        # 2) 问题成品报废
        self.svc.decide(
            issue_id,
            target_type="product",
            target_id="prod-s3-0926",
            action="destroy",
            by="林质检",
        )
        self.assertEqual(
            self.repo.get("products", "prod-s3-0926")["status"], P_DESTROYED
        )

        # 3) 没有复检合格记录不能恢复
        with self.assertRaises(Conflict):
            self.svc.decide(
                issue_id,
                target_type="broth",
                target_id="broth-merge-0330",
                action="release",
                by="林质检",
            )

        # 4) 支系复检：先 retest（维持冻结），合格后 release
        self.svc.decide(
            issue_id,
            target_type="broth",
            target_id="broth-s3-0328",
            action="retest",
            by="林质检",
        )
        self.svc.add_check(
            {
                "target_type": "broth",
                "target_id": "broth-s3-0328",
                "check_type": "microbe",
                "result": "pass",
                "recorder": "林质检",
            }
        )
        self.svc.decide(
            issue_id,
            target_type="broth",
            target_id="broth-s3-0328",
            action="release",
            by="林质检",
            note="微生物合格，恢复使用",
        )
        self.assertEqual(
            self.repo.must_get_broth("broth-s3-0328")["status"], B_RELEASED
        )

        # 合并支系与 27 日成品同样复检恢复
        self.svc.add_check(
            {
                "target_type": "broth",
                "target_id": "broth-merge-0330",
                "check_type": "microbe",
                "result": "pass",
                "recorder": "林质检",
            }
        )
        self.svc.decide(
            issue_id,
            target_type="broth",
            target_id="broth-merge-0330",
            action="release",
            by="林质检",
        )
        self.svc.add_check(
            {
                "target_type": "product",
                "target_id": "prod-s3-0927",
                "check_type": "sensory",
                "result": "pass",
                "recorder": "林质检",
            }
        )
        self.svc.decide(
            issue_id,
            target_type="product",
            target_id="prod-s3-0927",
            action="release",
            by="林质检",
        )
        self.assertEqual(
            self.repo.get("products", "prod-s3-0927")["status"], P_CLEARED
        )

        # 仍有未处置对象时不能关单（此时 s3-0328 已 release、merge 已 release）
        # 下游已全部到达终态/恢复，允许关单
        closed = self.svc.close_issue(issue_id, by="林质检", note="处置完毕")
        self.assertEqual(closed["issue"]["status"], "closed")

        # 处置决定全部留痕，且可从成品追到
        trace = self.svc.trace_product("prod-s3-0926", role="qc")
        actions = {
            (g["broth_id"], d["action"])
            for g in trace["generations"]
            for d in g["decisions"]
        }
        self.assertIn(("broth-s3-0328", "hold"), actions)
        self.assertIn(("broth-s3-0328", "release"), actions)
        prod_decisions = [
            d for d in self.repo.list_decisions("product", "prod-s3-0926")
        ]
        self.assertEqual([d["action"] for d in prod_decisions], ["hold", "destroy"])

    def test_cannot_decide_outside_scope(self) -> None:
        issue_id = self._open_flavor_issue()
        with self.assertRaises(Conflict):
            self.svc.decide(
                issue_id,
                target_type="broth",
                target_id="broth-s4-013",  # 既不同源也未共用汤底
                action="hold",
                by="林质检",
            )

    def test_trace_walks_every_generation(self) -> None:
        trace = self.svc.trace_product("prod-s3-0927", role="heir")
        gens = [(g["generation"], g["broth_id"]) for g in trace["generations"]]
        self.assertEqual(gens[0][1], self.root)
        self.assertEqual(gens[-1][1], "broth-merge-0330")
        # 每代都有代数与来源操作；八十度下料记录可追（加热是批次履历事件）
        gen328 = next(g for g in trace["generations"] if g["broth_id"] == "broth-s3-0328")
        heats = [t for t in gen328["timeline"] if t["op"] == "heat"]
        self.assertEqual(heats[-1]["temperature_c"], 80)
        self.assertEqual(heats[-1]["stage"], "reach_80_add_ingredients")
        # 合并一代有两个父
        merged_gen = next(g for g in trace["generations"] if g["broth_id"] == "broth-merge-0330")
        self.assertEqual(
            sorted(merged_gen["parent_ids"]),
            ["broth-s1-0329", "broth-s3-0328"],
        )
        # 根汤配方比例对传承人可见
        self.assertIn("recipe", trace["generations"][0])
