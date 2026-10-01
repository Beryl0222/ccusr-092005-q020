"""权限与脱敏：配方比例仅传承人/质控可见；顾客只见脱敏来源与过敏原。"""

from __future__ import annotations

from broth.models import ROLE_CUSTOMER, ROLE_HEIR, ROLE_QC, ROLE_STAFF

from support import BrothTestCase


class AccessTest(BrothTestCase):
    def test_recipe_visible_only_to_heir_and_qc(self) -> None:
        heir = self.svc.broth_view(self.root, role=ROLE_HEIR)
        qc = self.svc.broth_view(self.root, role=ROLE_QC)
        staff = self.svc.broth_view(self.root, role=ROLE_STAFF)
        self.assertIn("recipe", heir)
        self.assertEqual(heir["recipe"]["ratios"]["黄酱"], 150)
        self.assertIn("recipe", qc)
        self.assertNotIn("recipe", staff)

        trace_staff = self.svc.trace_product("prod-s3-0927", role=ROLE_STAFF)
        self.assertNotIn("recipe", trace_staff["generations"][0])
        trace_heir = self.svc.trace_product("prod-s3-0927", role=ROLE_HEIR)
        self.assertIn("recipe", trace_heir["generations"][0])

    def test_supplier_hidden_from_staff_material_view(self) -> None:
        trace = self.svc.trace_product("prod-s3-0926", role=ROLE_STAFF)
        for m in trace["product"]["material_usages"]:
            self.assertNotIn("supplier", m)
        qc = self.svc.trace_product("prod-s3-0926", role=ROLE_QC)
        suppliers = {m.get("supplier") for m in qc["product"]["material_usages"]}
        self.assertIn("鲁西肉联", suppliers)

    def test_customer_view_is_masked(self) -> None:
        view = self.svc.customer_view("SB-20260926-03")
        self.assertEqual(view["batch_code"], "SB-20260926-03")
        self.assertIn("狮子头", view["items"])
        # 不出具体门店名、供应商名、批次号全文、内部老汤 id
        text = repr(view)
        self.assertNotIn("兖州分店", text)
        self.assertNotIn("鲁西肉联", text)
        self.assertNotIn("broth-s3-0328", text)
        self.assertNotIn("MBL-20260923-A", text)
        # 但过敏原必须如实告知
        self.assertIn("鸡蛋", view["allergens"])
        self.assertIn("小麦", view["allergens"])
        self.assertIn("大豆", view["allergens"])
        self.assertTrue(view["allergen_notice"])
        # 只展示脱敏的一脉来源与世代
        self.assertEqual(view["broth"]["generation"], 328)
        self.assertIn("一脉老汤", view["broth"]["line_origin"])

    def test_customer_cannot_see_internal_trace(self) -> None:
        # customer 角色出现在内部追踪入口时由 API 拒绝；服务层也不下发配方
        view = self.svc.trace_product("prod-s3-0926", role=ROLE_CUSTOMER)
        self.assertNotIn("recipe", view["generations"][0])
