"""把 fixtures/seed.json 导入为运行档案。

用法：``python -m broth.seed [seed.json] [out_db.json]``
幂等：输出文件已存在时报错退出，避免覆盖在役档案。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .repository import Repository
from .service import BrothService


def seed_database(seed_path: str | Path, out_path: str | Path) -> Repository:
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    out = Path(out_path)
    if out.exists():
        raise FileExistsError(f"档案已存在，拒绝覆盖: {out}")

    repo = Repository(out)
    svc = BrothService(repo)
    for store in data["stores"]:
        svc.add_store(store["id"], store["name"], store.get("region", ""))
    for mat in data["materials"]:
        svc.add_material(
            mat["kind"],
            mat["name"],
            batch_no=mat["batch_no"],
            supplier=mat["supplier"],
            origin_region=mat.get("origin_region", ""),
            allergens=mat.get("allergens", []),
            material_id=mat["id"],
            received_at=mat.get("received_at"),
        )
    for root in data.get("roots", []):
        svc.register_root_broth(
            root["store_id"],
            broth_id=root["broth_id"],
            generation=root.get("generation", 1),
            ingredients=root.get("ingredients", []),
            recipe=root.get("recipe"),
            created_at=root.get("created_at"),
            note=root.get("note", ""),
        )

    # 事件必须按发生时间顺序重放，离线补传场景也走同一入口
    events = sorted(data["events"], key=lambda e: e["occurred_at"])
    receives = {r["broth_id"]: r for r in data.get("receives", [])}
    for event in events:
        svc.record_event(event)
        if event["op"] == "transfer":
            child = event["source_broth_ids"][0]
            rec = receives.get(child)
            if rec:
                svc.receive_transfer(child, at=rec["at"])

    repo.save()
    return repo


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    seed_path = args[0] if args else "fixtures/seed.json"
    out_path = args[1] if len(args) > 1 else "data/broth_db.json"
    repo = seed_database(seed_path, out_path)
    print(
        f"已初始化档案 {out_path}："
        f"{len(repo.list('broths'))} 代老汤，"
        f"{len(repo.list('events'))} 条事件，"
        f"{len(repo.list('products'))} 个烹制批次"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
