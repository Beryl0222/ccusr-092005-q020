"""重启连续性：重新打开档案后批次号、谱系、冻结状态、处置记录全部保留。"""

from __future__ import annotations

from broth.models import B_HELD
from broth.repository import Repository
from broth.service import BrothService

from support import BrothTestCase


class RestartTest(BrothTestCase):
    def test_state_survives_reopen_and_sequence_continues(self) -> None:
        # 立案冻结一个支系
        self.svc.add_check(
            {
                "target_type": "product",
                "target_id": "prod-s3-0926",
                "check_type": "sensory",
                "result": "fail",
                "recorder": "林质检",
            }
        )
        self.assertEqual(
            self.repo.must_get_broth("broth-s3-0328")["status"], B_HELD
        )
        events_before = len(self.repo.list("events"))

        # 重新加载同一档案，模拟服务重启
        repo2 = Repository(self.db_path)
        svc2 = BrothService(repo2)
        self.assertEqual(
            repo2.must_get_broth("broth-s3-0328")["status"], B_HELD
        )
        self.assertEqual(len(repo2.list("events")), events_before)

        # 冻结状态仍生效
        from broth.errors import FrozenError

        with self.assertRaises(FrozenError):
            svc2.record_event(
                {
                    "event_id": "evt-after-restart-blocked",
                    "op": "heat",
                    "store_id": "store-03",
                    "occurred_at": "2026-09-28T10:00:00Z",
                    "source_broth_ids": ["broth-s3-0328"],
                    "temperature_c": 80,
                }
            )

        # 未冻结的总店祖汤继续操作，世代连续，不产生重复 id
        result = svc2.record_event(
            {
                "event_id": "evt-after-restart-ok",
                "op": "divide",
                "store_id": "store-01",
                "occurred_at": "2026-09-28T10:10:00Z",
                "source_broth_ids": [self.root],
                "child_broth_id": "broth-after-restart",
            }
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["child_broth"]["generation"], 327)

        # 处置记录保留
        decisions = repo2.list_decisions("broth", "broth-s3-0328")
        self.assertTrue(any(d["action"] == "hold" for d in decisions))
