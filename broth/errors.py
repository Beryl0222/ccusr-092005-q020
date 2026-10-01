"""领域错误类型。

所有可预期的业务错误都继承 :class:`BrothError`，HTTP 层据此映射状态码；
未知异常不对外暴露内部细节。
"""

from __future__ import annotations


class BrothError(Exception):
    """业务规则错误的共同基类。"""

    #: 对外的错误码，便于前端与离线门店按码处理
    code = "bad_request"
    #: 对应的 HTTP 状态码
    http_status = 400


class NotFound(BrothError):
    code = "not_found"
    http_status = 404


class Conflict(BrothError):
    """并发或状态冲突（重复扫码、重复立案等）。"""

    code = "conflict"
    http_status = 409


class FrozenError(Conflict):
    """汤底已被冻结（隔离观察/报废），不能进入新制作。"""

    code = "frozen"
    http_status = 409


class PermissionDenied(BrothError):
    code = "forbidden"
    http_status = 403


class ValidationError(BrothError):
    code = "invalid"
    http_status = 422


def error_code(exc: BrothError) -> str:
    return exc.code
