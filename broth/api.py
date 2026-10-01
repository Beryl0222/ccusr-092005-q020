"""HTTP 接口（仅用标准库）。

令牌鉴权：``Authorization: Bearer <token>``，令牌->身份映射来自环境变量
``BROTH_TOKENS_FILE`` 指向的 JSON（默认 ``fixtures/tokens.json``），形如::

    {"store-token": {"role": "staff", "name": "三号店当班", "store_id": "store-03"}}

路由概览：

| 方法 路径                              | 角色        | 说明                       |
|---------------------------------------|-------------|----------------------------|
| POST /events                          | staff+      | 操作补传（扫码，幂等去重） |
| POST /transfers/{broth_id}/receive    | staff+      | 在途老汤接收入店           |
| POST /checks                          | qc          | 温度/感官/微生物检测       |
| GET  /issues                          | qc          | 未关闭异常单               |
| POST /issues                          | qc          | 手工立案                   |
| GET  /issues/{id}                     | qc          | 圈定范围与处置记录         |
| POST /issues/{id}/decisions           | qc          | 报废/复检/隔离/恢复        |
| POST /issues/{id}/close               | qc          | 全部处置完毕后关单         |
| GET  /trace/products/{id}             | staff+      | 成品->每代老汤全程追踪     |
| GET  /broths/{id}                     | staff+      | 单代批次视图（含配方授权） |
| GET  /public/products/{code}          | 任意（顾客）| 脱敏来源与过敏原           |
| GET  /healthz                         | 任意        | 存活探针                   |

管理类建档（门店/物料/根汤底）：POST /admin/...，限 heir/qc。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import BrothError, PermissionDenied
from .models import (
    ROLE_HEIR,
    ROLE_QC,
    ROLE_STAFF,
)
from .repository import Repository
from .service import BrothService

_STAFF_PLUS = (ROLE_STAFF, ROLE_QC, ROLE_HEIR)
_ADMIN = (ROLE_QC, ROLE_HEIR)

DEFAULT_TOKENS_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "tokens.json"


def load_tokens(path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    token_path = Path(path or os.environ.get("BROTH_TOKENS_FILE", DEFAULT_TOKENS_FILE))
    if not token_path.exists():
        return {}
    raw = json.loads(token_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("令牌文件必须是 {token: {role,...}} 对象")
    return raw


class ApiState:
    """跨线程共享的服务与锁。

    仓储的“读改写”需要互斥；锁粒度为整库，门店规模下足够，且保证并发补传
    不会基于过期谱系各建一代。
    """

    def __init__(self, repo_path: str, tokens: dict[str, dict[str, Any]] | None = None):
        self.repo = Repository(repo_path)
        self.service = BrothService(self.repo)
        self.tokens = tokens if tokens is not None else load_tokens()
        self.lock = threading.RLock()


def make_handler(state: ApiState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "BrothLineage/1.0"

        # ---------------------------------------------------------- 基础收发
        def _send_json(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise BrothError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise BrothError("请求体必须是 JSON 对象")
            return body

        def _identity(self) -> dict[str, Any] | None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            return state.tokens.get(auth[len("Bearer ") :].strip())

        def _require(self, roles: tuple[str, ...]) -> dict[str, Any]:
            identity = self._identity()
            if identity is None:
                raise PermissionDenied("缺少或无法识别的 Bearer 令牌")
            if identity.get("role") not in roles:
                raise PermissionDenied(
                    f"角色 {identity.get('role')} 无权执行此操作"
                )
            return identity

        def _assert_store_scope(self, identity: dict[str, Any], store_id: Any) -> None:
            # 传承人与质控可跨店履责；门店令牌只能操作本店事件
            if identity.get("role") in _ADMIN:
                return
            if not store_id or identity.get("store_id") != store_id:
                raise PermissionDenied("门店令牌只能登记本店的操作")

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
            if os.environ.get("BROTH_HTTP_LOG"):
                super().log_message(fmt, *args)

        # ------------------------------------------------------------- 路由
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                with state.lock:
                    self._route(method, path)
            except BrothError as exc:
                self._send_json(
                    exc.http_status, {"error": exc.code, "message": str(exc)}
                )
            except Exception as exc:  # 不向外泄露内部细节
                import traceback

                self._send_json(
                    500, {"error": "internal", "message": "服务内部错误"}
                )
                self.log_error("internal error: %s\n%s", exc, traceback.format_exc())

        def _route(self, method: str, path: str) -> None:
            p = path.split("/")
            svc = state.service
            body = self._read_json() if method == "POST" else {}

            if method == "GET" and path == "/healthz":
                return self._send_json(200, {"ok": True})

            # 顾客视图：无需内部令牌，凭消费批次码查询
            if method == "GET" and len(p) == 4 and p[1] == "public" and p[2] == "products":
                return self._send_json(200, svc.customer_view(p[3]))

            if method == "POST" and path == "/events":
                identity = self._require(_STAFF_PLUS)
                self._assert_store_scope(identity, body.get("store_id"))
                body.setdefault("actor", identity.get("name", ""))
                result = svc.record_event(body)
                return self._send_json(200, result)

            if method == "POST" and len(p) == 4 and p[1] == "transfers" and p[3] == "receive":
                identity = self._require(_STAFF_PLUS)
                broth = svc.repo.must_get_broth(p[2])
                self._assert_store_scope(identity, broth["custody"].get("to_store"))
                return self._send_json(200, svc.receive_transfer(p[2], at=body.get("at")))

            if method == "POST" and path == "/checks":
                identity = self._require((ROLE_QC,))
                body.setdefault("recorder", identity.get("name", ""))
                return self._send_json(200, svc.add_check(body))

            if method == "GET" and path == "/issues":
                self._require((ROLE_QC,))
                return self._send_json(200, {"issues": svc.list_open_issues()})

            if method == "POST" and path == "/issues":
                identity = self._require((ROLE_QC,))
                return self._send_json(
                    200,
                    svc.open_issue(
                        target_type=body["target_type"],
                        target_id=body["target_id"],
                        issue_type=body["issue_type"],
                        opener=identity.get("name", "qc"),
                        note=body.get("note", ""),
                    ),
                )

            if method == "GET" and len(p) == 3 and p[1] == "issues":
                self._require((ROLE_QC,))
                return self._send_json(200, svc.get_issue(p[2]))

            if method == "POST" and len(p) == 4 and p[1] == "issues" and p[3] == "decisions":
                identity = self._require((ROLE_QC,))
                return self._send_json(
                    200,
                    svc.decide(
                        p[2],
                        target_type=body["target_type"],
                        target_id=body["target_id"],
                        action=body["action"],
                        by=identity.get("name", "qc"),
                        note=body.get("note", ""),
                    ),
                )

            if method == "POST" and len(p) == 4 and p[1] == "issues" and p[3] == "close":
                identity = self._require((ROLE_QC,))
                return self._send_json(
                    200, svc.close_issue(p[2], by=identity.get("name", "qc"), note=body.get("note", ""))
                )

            if method == "GET" and len(p) == 4 and p[1] == "trace" and p[2] == "products":
                identity = self._require(_STAFF_PLUS)
                return self._send_json(200, svc.trace_product(p[3], role=identity["role"]))

            if method == "GET" and len(p) == 3 and p[1] == "broths":
                identity = self._require(_STAFF_PLUS)
                return self._send_json(200, svc.broth_view(p[2], role=identity["role"]))

            if method == "POST" and path == "/admin/stores":
                self._require(_ADMIN)
                store = svc.add_store(
                    body["id"], body["name"], body.get("region", "")
                )
                return self._send_json(200, {"store": store})

            if method == "POST" and path == "/admin/materials":
                self._require(_ADMIN)
                return self._send_json(200, {"material": svc.add_material(**body)})

            if method == "POST" and path == "/admin/broths/root":
                self._require(_ADMIN)
                broth = svc.register_root_broth(
                    body["store_id"],
                    broth_id=body.get("broth_id"),
                    generation=body.get("generation", 1),
                    ingredients=body.get("ingredients", ()),
                    recipe=body.get("recipe"),
                    created_at=body.get("created_at"),
                    note=body.get("note", ""),
                )
                return self._send_json(200, {"broth": broth})

            self._send_json(404, {"error": "not_found", "message": f"无此路由: {method} {path}"})

    return Handler


def build_server(host: str, port: int, repo_path: str, tokens_file: str | None = None) -> ThreadingHTTPServer:
    tokens = load_tokens(tokens_file)
    state = ApiState(repo_path, tokens=tokens)
    return ThreadingHTTPServer((host, port), make_handler(state))
