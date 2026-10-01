"""领域模型：以可直接 JSON 序列化的 dict 表示。

谱系由两类对象构成：

* ``broth_batch`` 老汤批次（节点）。每次分装、续汤、合并、过滤都会生成新一代
  批次，``parent_ids`` 记录父子关系；合并有多个父批次。
* ``lineage_event`` 操作事件（边）。加热、跨店移交等不产生新批次的操作只登记
  事件；产生批次的操作同时登记父子两边。

物料批次（香料/肉类/配菜）独立建档，带过敏原信息；烹制批次绑定当次实际使用
的老汤批次与物料批次。温度记录与感官/微生物检测挂在批次上。
"""

from __future__ import annotations

import secrets
from typing import Any

# ---------------------------------------------------------------- 角色与权限

ROLE_HEIR = "heir"            # 传承人：可见配方比例
ROLE_QC = "qc"                # 质控（食品安全负责人）：可见配方、立案与处置
ROLE_STAFF = "staff"          # 门店普通人员：操作与追踪，不含配方比例
ROLE_CUSTOMER = "customer"    # 顾客：仅脱敏来源与过敏原

# 角色等级仅用于“配方比例”这类字段的收敛，处置权限另行判断
RECIPE_ROLES = frozenset({ROLE_HEIR, ROLE_QC})

# ---------------------------------------------------------------- 老汤批次状态

B_ACTIVE = "active"        # 正常使用
B_HELD = "held"            # 隔离观察：等待复检结论，禁止进入新制作
B_QUARANTINED = B_HELD
B_DESTROYED = "destroyed"  # 报废：终态
B_RELEASED = "released"    # 复检合格后恢复使用

#: 不能进入新制作的状态
FROZEN_STATUSES = frozenset({B_HELD, B_DESTROYED})

# ---------------------------------------------------------------- 事件类型

EV_DIVIDE = "divide"      # 分装（一父一子或多子）
EV_REFILL = "refill"      # 续汤/补汤（加基底汤或水，产生新一代）
EV_MERGE = "merge"        # 跨店/同店合并（多父一子）
EV_HEAT = "heat"          # 加热（不产生新批次，记录温度，如八十摄氏度下料）
EV_FILTER = "filter"      # 过滤（产生新一代，老汤载体延续）
EV_TRANSFER = "transfer"  # 跨店移交（不产生新批次）
EV_COOK = "cook"          # 烹制下料（成品批次，绑定实际物料）

#: 产生新一批老汤的操作
BATCH_SPAWNING_EVENTS = frozenset({EV_DIVIDE, EV_REFILL, EV_MERGE, EV_FILTER})
#: 不产生新批次、只在批次上追加履历的操作
BATCH_EVENT_ONLY = frozenset({EV_HEAT, EV_TRANSFER})

# ---------------------------------------------------------------- 异常处置

ISSUE_MICROBE = "microbe"      # 微生物异常
ISSUE_FLAVOR = "flavor"        # 风味异常
ISSUE_OPEN = "open"
ISSUE_CLOSED = "closed"

DECISION_HOLD = "hold"          # 隔离观察（立案时的初始动作）
DECISION_DESTROY = "destroy"    # 报废
DECISION_RETEST = "retest"      # 复检
DECISION_RELEASE = "release"    # 恢复使用

# 质检结论
QC_PENDING = "pending"
QC_PASS = "pass"
QC_FAIL = "fail"

# 烹制成品的处置状态
P_ACTIVE = "active"
P_HELD = "held"
P_DESTROYED = "destroyed"
P_CLEARED = "cleared"  # 复检/排查后解除风险，可正常对客

INGREDIENT_KINDS = frozenset({"spice", "meat", "side", "base"})


def now_ts() -> str:
    """单调且可排序的 UTC 时间戳（毫秒），同秒内多次登记也能区分先后。"""
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def parse_ts(value: str):
    """解析秒级或毫秒级的 ``...Z`` 时间戳，供顺序比较使用。"""
    import datetime as _dt

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(text)


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def require_fields(payload: dict[str, Any], fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if payload.get(f) in (None, "")]
    if missing:
        from .errors import ValidationError

        raise ValidationError(f"缺少必填字段: {', '.join(missing)}")
