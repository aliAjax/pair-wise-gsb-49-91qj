# 未决赔款准备金评估批次

纯 Python 标准库实现的再保险未决赔款准备金(IBNR/OS)月末评估封账原型，使用 SQLite 持久化，HTTP 接口由 `http.server` 提供。

## 解决的问题

月底做未决赔款准备金时，赔案补录、撤销或核定金额一变化，上一版准备金额就说不清按什么算的。本服务把**再保险合同、赔案版本、估算规则和待复核金额**冻结成可封账的评估批次：

- 每批冻结：参与合约、赔案版本、估算规则参数（IBNR/折现）、各合约估算依据与待复核金额。
- **封账前**赔案补录/撤销/核定金额变化：只把受影响合约在未封账批次中的项目标记失效，刷新时**仅重算这些合约**，其他项目保留原依据；带失效项目不能封账。
- **已封账/已转正式**批次永不被后续赔案变化影响，原始快照、规则、金额完整保留。
- 必须**财务复核**（`finance`，且显式 `confirmed`）才能转正式准备金；重复提交同一批次或重复转正式，只留一份结果。
- 批次头落盘后逐项目独立提交，写入失败可用**同一幂等键重提续作**，从完整批次恢复，不重复、不新建。
- 旧数据缺损失日期时，按**同一事件号**其他赔案的最早损失日期回填（不递增赔案版本，仅留审计）；补不全的合约项目单独停在 **待核对（pending_reconciliation）**，不阻止整批封账、不转正式。
- 合约详情、状态统计、合约/批次**审计时间线**可互相对账：批次号、变动原因、冻结依据与正式金额都可核对。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：错误类型、Actor、输入校验（金额/日期/月度标签）。
- `src/rules.py`：合约与赔案校验、分层摊回、IBNR/折现、待复核拆分、估算规则快照、变化检测、事件号回填。
- `src/repository.py`：SQLite 建表、事务、乐观并发、批次/项目/正式准备金/统一审计。
- `src/service.py`：用例编排与全部封账不变量。
- `src/http_api.py`：HTTP 路由与统一错误响应。
- `src/audit.py`：审计时间线只读封装。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、权限/版本冲突、崩溃续作、回填与待核对、幂等转正式。

## 金额口径（每个冻结项目）

- 单笔赔案毛口径：已核定用 `approved_loss`，已报未决用 `estimated_loss`，已撤销为 0。
- 分层摊回 = `min(max(0, 毛口径 − 起赔点), 限额−起赔点) × 分保比例`。
- 带 `review_flag` 的赔案金额计入**待复核金额**，其余计入估算金额；两部分按分层摊回拆分，合计等于整体摊回，再统一乘 `(1 + ibnr_factor) × discount_factor`。

## 启动

```bash
python3 app.py --db ./reserve-batches.db --port 8325
```

默认端口 `8325`，服务启动时自动建表。

## 角色

`underwriter`（合约）、`claims_officer`（赔案补录/撤销/核定）、`actuary`（建批/刷新/封账）、`finance`（复核转正式）、`admin`（全权限只读/兜底）。除 `/health` 和 `/` 外均需 `X-User-Id`、`X-Role` 头。

## 主要接口

### 合约与赔案
- `POST /api/contracts`：`underwriter` 创建再保合约（起赔点/限额/分保比例/起止日期）。
- `GET /api/contracts`、`GET /api/contracts/{id}`：合约列表、合约详情（含赔案、参与批次、正式准备金、是否待核对）。
- `POST /api/contracts/{id}/claims`：`claims_officer` 补录赔案（自动使未封账批次的受影响项目失效）。
- `POST /api/claims/{id}/amend`：赔案撤销/核定/金额调整，体含 `expected_version`（乐观并发）与 `change_reason`。

### 评估批次
- `POST /api/batches`：`actuary` 建批。体：`{"period":"2026-09","idempotency_key":"...","ibnr_factor":0.1,"discount_factor":1.0,"contract_ids":[可选]}`。重复幂等键只返回同一份；上次写入中断则自动续作。
- `GET /api/batches`、`GET /api/batches/{id}`：批次列表/详情（GET 详情对不完整的未封账批次自动恢复）。
- `POST /api/batches/{id}/refresh`：仅重算赔案已变化（含补录）的受影响合约项目，其余保持原依据。
- `POST /api/batches/{id}/close`：封账冻结；存在失效待重算项目时 409。
- `POST /api/batches/{id}/post`：`finance` 复核转正式，体 `{"confirmed":true,"adjustments":{"<contract_id>":{"formal_amount":1500000,"reason":"..."}}}`；默认正式金额=估算金额，待核对与待复核部分不入正式。已转正式重入返回 `already_posted:true` 且不覆盖。

### 查询与对账
- `GET /api/stats`：合约状态、批次状态（open/closed/posted）、项目状态（assessed/pending_reconciliation/stale）、金额汇总、待核对合约清单、按期正式金额。
- `GET /api/contracts/{id}/audit`、`GET /api/batches/{id}/audit`、`GET /api/claims/{id}/audit`：统一审计时间线。批次动作会扇出到相关合约时间线。

## 批次/项目状态

- 批次：`open`（待封账，可刷新）→ `closed`（已封账待财务复核）→ `posted`（已转正式，终态）。
- 项目：`assessed` / `pending_reconciliation`（损失日期补不全，单独停在待核对）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：分层摊回与 IBNR/折现、待复核拆分、撤销/核定口径、事件号回填、变动检测；完整冻结-失效-局部重算-封账-复核转正式流程；乐观版本冲突、权限拒绝、重复引用、写入中断续作、待核对不转正式、重复转正式只留一份。
