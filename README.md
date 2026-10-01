# 传统老汤批次传承

记录甏肉老汤在门店之间**分装、续汤、合并、加热、过滤、跨店移交**形成的代际谱系，
并把香料、肉类、配菜批次、过敏原、温度与感官/微生物检测绑定到实际烹制批次。
当出现“某家门店狮子头风味异常”的反馈时，系统能沿谱系圈定成品、在途分装与
可隔离支系，由食品安全负责人决定报废、复检或恢复，既不误伤无关支系，也不
中断一锅老汤的工艺传承。

## 模型一览

- **broth_batch 老汤批次**：谱系节点。每次分装/续汤/合并/过滤生成新一代，
  `parent_ids` 记录父子关系（合并有多个父），`generation` 为父代最高值 +1。
- **lineage_event 操作事件**：谱系边与履历。`divide / refill / merge / filter`
  产生新一代；`heat`（如八十摄氏度下料）与 `transfer`（跨店移交）不另立一代，
  只追加履历。移交期间批次 `custody = in_transit`。
- **material_batch 物料批次**：香料/肉类/配菜/基底汤独立建档，带过敏原。
- **product_batch 烹制批次**：绑定当次实际使用的老汤批次与物料批次。
- **check 检测 / issue 异常单 / decision 处置决定**：感官或微生物不合格自动立案，
  报废、复检、恢复全程留痕。

### 异常圈定的三层范围

立案（或不合格检测自动立案）后：

1. **downstream 直接支系**（报告点及其后代、相关成品、在途分装）立即自动冻结
   （`held`）并逐条写入处置决定；冻结汤底不能进入任何新制作、不能移交/接收；
2. **shared_ancestry 共同祖先支系**（共用一脉老汤的其他门店，如曲阜支系）只列入
   观察名单，**不自动冻结**，由质控人工判断，避免误伤；
3. **possible_related_by_material 同批原料关联对象**（原料批次横向波及，如邹城
   门店用了同批五花肉）仅作提示，可人工纳入处置。

恢复（`release`）前必须存在立案之后的复检合格记录；全部下游到达报废或恢复
终态后异常单才能关闭。

### 去重与连续性

- 离线补传以**扫码事件号** `event_id` 为幂等键；同号重放直接返回既有事件。
- 门店离线后换了扫码编号但操作时间与对象完全一致时，以操作指纹
  （操作类型＋门店＋发生时间＋源批次＋物料＋目标店等）二次去重。
- 同一事件号对应不同内容会被拒绝；发生时间不同视为真实的两次操作。
- 档案以单 JSON 原子落盘（临时文件 + `os.replace`），重启后批次世代、
  冻结状态与处置记录全部保留，已冻结汤底不会因重启复活。

### 权限

| 角色 | 能力 |
|------|------|
| `heir` 传承人 | 全部操作；**可见配方比例** |
| `qc` 食安负责人 | 检测、立案、处置；**可见配方比例** |
| `staff` 门店人员 | 本店操作与内部追踪；不可见配方比例与供应商 |
| 顾客（无令牌） | 凭消费批次码只看脱敏来源与过敏原说明 |

顾客视图不包含门店全名、供应商名、完整物料批号与任何内部批次 id。

## 运行

仅依赖 Python 3 标准库（测试用 pytest）。

```bash
# 1. 从种子初始化档案（幂等，已存在则拒绝覆盖）
python3 -m broth.seed fixtures/seed.json data/broth_db.json

# 2. 启动服务
BROTH_DB=data/broth_db.json BROTH_TOKENS_FILE=fixtures/tokens.json \
  BROTH_HOST=127.0.0.1 BROTH_PORT=8080 python3 -m broth
```

令牌映射在 `fixtures/tokens.json`（**示例令牌，生产环境必须替换**），
请求头携带 `Authorization: Bearer <token>`。

## API 摘要

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /events` | staff+ | 登记/补传一次操作（分装、续汤、合并、加热、过滤、移交、烹制），幂等去重 |
| `POST /transfers/{broth_id}/receive` | staff+（接收店） | 在途老汤确认入库；已冻结则拒收 |
| `POST /checks` | qc | 温度/感官/微生物检测，不合格自动立案 |
| `GET  /issues` · `POST /issues` | qc | 未关闭异常单 / 手工立案 |
| `GET  /issues/{id}` | qc | 三层圈定范围与全部处置记录 |
| `POST /issues/{id}/decisions` | qc | `hold` 隔离 / `retest` 复检 / `destroy` 报废 / `release` 恢复 |
| `POST /issues/{id}/close` | qc | 下游全部处置完毕后关单 |
| `GET  /trace/products/{id}` | staff+ | 成品 → 每代老汤、检测、温度与处置决定 |
| `GET  /broths/{id}` | staff+ | 单代批次（heir/qc 含配方比例） |
| `GET  /public/products/{消费批次码}` | 顾客 | 脱敏来源与过敏原说明 |
| `POST /admin/stores` · `/admin/materials` · `/admin/broths/root` | heir/qc | 建档 |

### 事件示例

```bash
curl -X POST http://127.0.0.1:8080/events \
  -H "Authorization: Bearer staff-03" -H "Content-Type: application/json" \
  -d '{
    "event_id": "evt-s3-merge",
    "op": "merge",
    "store_id": "store-03",
    "occurred_at": "2026-09-27T05:30:00Z",
    "source_broth_ids": ["broth-s3-0328", "broth-s1-0329"],
    "child_broth_id": "broth-merge-0330",
    "note": "跨店合并：兖州续汤支系与总店新送支系并汤"
  }'
```

烹制事件在 `materials` 中绑定实际物料批次、`temperature_c` 记录下料温度，
`code` 即印给顾客的消费批次码（脱敏查询用）。

## 目录

```
broth/
  models.py       角色、状态、操作类型常量
  errors.py       业务错误与 HTTP 状态映射
  repository.py   JSON 原子持久化
  service.py      谱系、去重、冻结、圈定、处置、追踪与脱敏视图
  api.py          标准库 HTTP 服务与 Bearer 令牌鉴权
  seed.py         种子导入（按发生时间重放事件）
fixtures/
  seed.json       四门店、物料批次、含配方根汤与跨店合并事件流
  tokens.json     示例令牌
tests/            谱系/去重/冻结/异常隔离/权限/重启/HTTP 共 28 个用例
```

## 测试

```bash
python3 -m pytest tests -q          # 或：
python3 -m unittest discover -s tests
```

`project_data.py` 提供对种子文档顶层结构的样例读取与检查。
