"""离线补传去重：同扫码编号重放、同操作指纹换编号都不得重复建边。"""

from __future__ import annotations

from broth.errors import Conflict

from support import BrothTestCase


class DedupTest(BrothTestCase):
    def _payload(self, event_id: str) -> dict:
        return {
            "event_id": event_id,
            "op": "divide",
            "store_id": "store-01",
            "actor": "周师傅",
            "occurred_at": "2026-09-28T08:00:00Z",
            "source_broth_ids": [self.root],
            "child_broth_id": "broth-offline-1",
        }

    def test_same_event_id_replay_is_idempotent(self) -> None:
        first = self.svc.record_event(self._payload("evt-offline"))
        replay = self.svc.record_event(self._payload("evt-offline"))
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertFalse(replay["deduplicated"])
        self.assertEqual(
            len([b for b in self.repo.list("broths") if b["id"] == "broth-offline-1"]),
            1,
        )
        self.assertEqual(len(self.repo.list("events")), 18 + 1)

    def test_same_fingerprint_new_scan_id_is_deduplicated(self) -> None:
        first = self.svc.record_event(self._payload("evt-offline-a"))
        again = self.svc.record_event(self._payload("evt-offline-b"))
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["event"]["event_id"], "evt-offline-a")
        self.assertEqual(len(self.repo.list("broths")), 10)

    def test_same_event_id_different_payload_rejected(self) -> None:
        self.svc.record_event(self._payload("evt-offline"))
        changed = self._payload("evt-offline")
        changed["occurred_at"] = "2026-09-28T09:00:00Z"
        with self.assertRaises(Conflict):
            self.svc.record_event(changed)

    def test_different_time_means_different_event(self) -> None:
        # 同店同锅但不同发生时间 = 真实的第二次操作，必须保留
        p1 = self._payload("evt-time-a")
        p2 = self._payload("evt-time-b")
        p2["occurred_at"] = "2026-09-28T08:30:00Z"
        p2["child_broth_id"] = "broth-offline-2"
        self.svc.record_event(p1)
        r2 = self.svc.record_event(p2)
        self.assertTrue(r2["created"])
