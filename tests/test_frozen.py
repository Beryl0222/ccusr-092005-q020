"""冻结规则：held/destroyed 的汤底不得进入新制作，恢复后可再用。"""

from __future__ import annotations

from broth.errors import FrozenError
from broth.models import B_ACTIVE, B_DESTROYED, B_HELD, B_RELEASED

from support import BrothTestCase


def heat(svc, event_id, broth_id, store="store-01", at="2026-09-28T05:00:00Z"):
    return svc.record_event(
        {
            "event_id": event_id,
            "op": "heat",
            "store_id": store,
            "occurred_at": at,
            "source_broth_ids": [broth_id],
            "temperature_c": 80,
            "stage": "reach_80_add_ingredients",
        }
    )


class FrozenRuleTest(BrothTestCase):
    def test_held_broth_blocked_from_new_cook(self) -> None:
        broth = self.repo.must_get_broth(self.root)
        broth["status"] = B_HELD
        self.repo.save()
        with self.assertRaises(FrozenError):
            heat(self.svc, "evt-heat-frozen", self.root)
        with self.assertRaises(FrozenError):
            self.svc.record_event(
                {
                    "event_id": "evt-divide-frozen",
                    "op": "divide",
                    "store_id": "store-01",
                    "occurred_at": "2026-09-28T05:10:00Z",
                    "source_broth_ids": [self.root],
                }
            )

    def test_destroyed_broth_blocked_and_terminal(self) -> None:
        broth = self.repo.must_get_broth(self.root)
        broth["status"] = B_DESTROYED
        self.repo.save()
        with self.assertRaises(FrozenError):
            heat(self.svc, "evt-heat-destroyed", self.root)

    def test_released_broth_usable_again(self) -> None:
        broth = self.repo.must_get_broth(self.root)
        broth["status"] = B_HELD
        self.repo.save()
        # 复检通过后由 released 恢复
        broth["status"] = B_RELEASED
        self.repo.save()
        result = heat(self.svc, "evt-heat-after-release", self.root)
        self.assertTrue(result["created"])
        self.assertEqual(self.repo.must_get_broth(self.root)["status"], B_RELEASED)

    def test_active_sibling_branch_still_usable_when_other_frozen(self) -> None:
        # 曲阜支系冻结不应影响同根的邹城独立老汤与总店本锅
        self.repo.must_get_broth("broth-s2-0328")["status"] = B_HELD
        self.repo.save()
        self.assertEqual(
            self.repo.must_get_broth(self.root)["status"], B_ACTIVE
        )
        result = heat(self.svc, "evt-heat-root-ok", self.root)
        self.assertTrue(result["created"])
        with self.assertRaises(FrozenError):
            heat(
                self.svc,
                "evt-heat-s2-blocked",
                "broth-s2-0328",
                store="store-02",
            )
