"""领域错误类型。"""

from __future__ import annotations


class LineageError(Exception):
    """谱系服务的基础错误。"""

    pass


class DuplicateEventError(LineageError):
    """同一事件号以不同内容重复提交（扫码重复但载荷冲突）。"""

    def __init__(self, event_id: str, field: str | None = None) -> None:
        detail = f"（字段 {field} 不一致）" if field else ""
        super().__init__(f"事件 {event_id} 已存在且内容不一致{detail}")
        self.event_id = event_id


class FrozenBrothError(LineageError):
    """汤底处于冻结状态，不能进入新制作。"""

    def __init__(self, broth_id: str, reason: str = "冻结") -> None:
        super().__init__(f"汤底 {broth_id} 当前为{reason}状态，禁止用于新制作")
        self.broth_id = broth_id


class UnknownBatchError(LineageError):
    def __init__(self, kind: str, batch_id: str) -> None:
        super().__init__(f"未知{kind}批次：{batch_id}")
        self.kind = kind
        self.batch_id = batch_id


class PermissionDeniedError(LineageError):
    def __init__(self, role: str, action: str) -> None:
        super().__init__(f"角色 {role} 无权执行：{action}")
        self.role = role
        self.action = action


class IllegalTransitionError(LineageError):
    """处置决定与当前隔离状态不兼容（例如未复检合格就恢复）。"""
