from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from broth_lineage import LineageService
from broth_lineage.store import LineageStore

SEED_PATH = Path("fixtures/seed.json")


def load_seed(path: str | Path = SEED_PATH) -> dict[str, Any]:
    """读取项目维护的领域样例，并检查顶层结构。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError("领域样例必须包含 records 数组")
    return data


def build_service(db_path: str | Path = ":memory:", seed_path: str | Path = SEED_PATH) -> LineageService:
    """用种子数据构建一个可查询的后端服务（门店、原料、事件、检测全部入库）。"""
    seed = load_seed(seed_path)
    service = LineageService(LineageStore(db_path))

    for store in seed.get("stores", []):
        service.register_store(store["id"], store["name"])
    for ing in seed.get("ingredients", []):
        service.register_ingredient(
            ing["id"], ing["category"], ing["name"],
            allergens=ing.get("allergens", []),
            supplier=ing.get("supplier"),
            received_at=ing.get("received_at"),
        )
    for recipe in seed.get("recipes", []):
        service.register_recipe(recipe["id"], recipe["name"], recipe["ratios"], "master")
    for broth in seed.get("broths", []):
        service.register_broth(
            broth["id"], broth["generation"], broth["store_id"],
            note=broth.get("note"),
        )

    # 事件按发生时间补传入库；种子中事件顺序可能因离线延迟而乱序
    service.sync_events(seed.get("events", []))

    for test in seed.get("tests", []):
        service.add_test(
            test["id"], test["target_type"], test["target_id"],
            test["kind"], test["verdict"], test["occurred_at"],
            role=test.get("role", "qc"),
            detail=test.get("detail"),
            recorder=test.get("recorder"),
        )
    return service
