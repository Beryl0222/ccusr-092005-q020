"""HTTP API 端到端：真实起服，令牌鉴权、补传去重、自动立案、处置、脱敏查询。"""

from __future__ import annotations



import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broth.api import ApiState, make_handler
from broth.seed import seed_database
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "fixtures" / "seed.json"

TOKENS = {
    "heir-token": {"role": "heir", "name": "传承人 周师傅", "store_id": "store-01"},
    "qc-token": {"role": "qc", "name": "林质检"},
    "staff-03": {"role": "staff", "name": "兖州店当班", "store_id": "store-03"},
    "staff-04": {"role": "staff", "name": "邹城店当班", "store_id": "store-04"},
}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "broth.json")
        seed_database(SEED, self.db_path)
        state = ApiState(self.db_path, tokens=TOKENS)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method: str, path: str, token: str | None = None, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        data = json.loads(raw) if raw else {}
        conn.close()
        return resp.status, data

    def test_health_and_auth_gates(self) -> None:
        status, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)

        status, body = self.request("GET", "/trace/products/prod-s3-0927")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

        # 门店人员不能提交质检
        status, _ = self.request(
            "POST",
            "/checks",
            token="staff-03",
            body={"target_type": "broth", "target_id": "broth-s3-0328", "check_type": "microbe", "result": "pass"},
        )
        self.assertEqual(status, 403)

    def test_event_dedup_over_http(self) -> None:
        payload = {
            "event_id": "evt-api-offline",
            "op": "divide",
            "store_id": "store-01",
            "occurred_at": "2026-09-28T08:00:00Z",
            "source_broth_ids": ["broth-lineage-main"],
            "child_broth_id": "broth-api-1",
        }
        s1, b1 = self.request("POST", "/events", token="staff-03", body=payload)
        # 兖州店令牌不能登记总店事件 -> 门店归属校验
        self.assertEqual(s1, 403, b1)
        self.assertEqual(b1["error"], "forbidden")

        s2, b2 = self.request(
            "POST",
            "/events",
            token="heir-token",
            body=payload,
        )
        self.assertEqual(s2, 200, b2)
        self.assertTrue(b2["created"])
        s3, b3 = self.request("POST", "/events", token="heir-token", body=payload)
        self.assertEqual(s3, 200)
        self.assertFalse(b3["created"])

    def test_frozen_returns_409_and_incident_flow(self) -> None:
        # 质检感官不合格 -> 自动立案
        status, body = self.request(
            "POST",
            "/checks",
            token="qc-token",
            body={
                "target_type": "product",
                "target_id": "prod-s3-0926",
                "check_type": "sensory",
                "result": "fail",
                "note": "狮子头风味异常",
            },
        )
        self.assertEqual(status, 200, body)
        issue_id = body["issue"]["id"]

        # 圈定：合并支系自动 held
        status, issue = self.request("GET", f"/issues/{issue_id}", token="qc-token")
        self.assertEqual(status, 200)
        held = {b["broth_id"] for b in issue["scope"]["branches"]["already_held"]}
        self.assertIn("broth-merge-0330", held)
        in_transit = issue["scope"]["branches"]["in_transit"]
        self.assertEqual(in_transit, [])  # 种子里的在途均已接收

        # 冻结支系烹制 -> 409 frozen
        status, body = self.request(
            "POST",
            "/events",
            token="staff-03",
            body={
                "event_id": "evt-api-cook-blocked",
                "op": "cook",
                "store_id": "store-03",
                "occurred_at": "2026-09-28T09:00:00Z",
                "source_broth_ids": ["broth-merge-0330"],
                "items": ["狮子头"],
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "frozen")

        # 观察名单里的曲阜支系未被冻结，仍可操作
        status, body = self.request(
            "POST",
            "/events",
            token="staff-04",
            body={
                "event_id": "evt-api-s4-heat",
                "op": "heat",
                "store_id": "store-04",
                "occurred_at": "2026-09-28T09:10:00Z",
                "source_broth_ids": ["broth-s4-013"],
                "temperature_c": 80,
            },
        )
        self.assertEqual(status, 200, body)

        # 报废合并支系
        status, body = self.request(
            "POST",
            f"/issues/{issue_id}/decisions",
            token="qc-token",
            body={"target_type": "broth", "target_id": "broth-merge-0330", "action": "destroy"},
        )
        self.assertEqual(status, 200, body)

        # 全部处置后关单（下游其余节点也需处置：retest+release）
        for broth_id, product_id in [
            ("broth-s3-0328", None),
        ]:
            self.request(
                "POST",
                "/checks",
                token="qc-token",
                body={"target_type": "broth", "target_id": broth_id, "check_type": "microbe", "result": "pass"},
            )
            self.request(
                "POST",
                f"/issues/{issue_id}/decisions",
                token="qc-token",
                body={"target_type": "broth", "target_id": broth_id, "action": "release"},
            )
        for pid in ("prod-s3-0926", "prod-s3-0927"):
            self.request(
                "POST",
                "/checks",
                token="qc-token",
                body={"target_type": "product", "target_id": pid, "check_type": "sensory", "result": "pass"},
            )
            self.request(
                "POST",
                f"/issues/{issue_id}/decisions",
                token="qc-token",
                body={"target_type": "product", "target_id": pid, "action": "release"},
            )
        status, body = self.request("POST", f"/issues/{issue_id}/close", token="qc-token", body={})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["issue"]["status"], "closed")

    def test_trace_role_and_public_view(self) -> None:
        s, staff = self.request("GET", "/trace/products/prod-s3-0927", token="staff-03")
        self.assertEqual(s, 200)
        self.assertNotIn("recipe", staff["generations"][0])

        s, heir = self.request("GET", "/trace/products/prod-s3-0927", token="heir-token")
        self.assertEqual(s, 200)
        self.assertIn("recipe", heir["generations"][0])

        # 单代批次视图：传承人可见配方比例，门店人员不可见
        s, broth = self.request("GET", "/broths/broth-lineage-main", token="heir-token")
        self.assertEqual(s, 200, broth)
        self.assertEqual(broth["generation"], 326)
        self.assertIn("recipe", broth)
        s, broth_staff = self.request("GET", "/broths/broth-lineage-main", token="staff-03")
        self.assertEqual(s, 200)
        self.assertNotIn("recipe", broth_staff)

        # 顾客视图：无令牌、无门店/供应商/内部 id
        s, pub = self.request("GET", "/public/products/" + quote("SB-20260926-03"))
        self.assertEqual(s, 200)
        self.assertIn("鸡蛋", pub["allergens"])
        text = json.dumps(pub, ensure_ascii=False)
        for secret in ("兖州分店", "鲁西肉联", "broth-s3-0328", "MBL-20260923-A"):
            self.assertNotIn(secret, text)

        s, missing = self.request("GET", "/public/products/NOPE")
        self.assertEqual(s, 404)
        self.assertEqual(missing["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
