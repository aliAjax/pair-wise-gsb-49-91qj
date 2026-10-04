# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。月底未决赔款准备金以**可封账评估批次**方式管理：每批冻结参与合约、赔案版本、估算规则和待复核金额。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额、恢复保费、冲突检查、赔案撤销与核定金额重算。
- `src/repository.py`：SQLite建表、事务和查询（含批次、成员、明细、批次审计）。
- `src/service.py`：合约用例编排、权限检查、乐观并发和审计。
- `src/reserves.py`：准备金评估批次——冻结依据、失效重算、断点续作、封账、财务转正式、旧数据回填。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和评估批次测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。动作含`bind`、`submit_claim`、`calculate`、`recalculate`（核定金额变化，版本递增）、`withdraw_claim`（赔案撤销）、`settle`、`reject`。
- `GET /api/records/{id}/engagements`：合约详情，含其在各评估批次中的依据、状态与正式金额。

### 未决赔款准备金评估批次

- `POST /api/batches`：创建并立即估算批次，`{"reference":"RES-2026-09","period":"2026-09","rules":{"ibnr_pct":0.1,"expense_pct":0.05}}`。创建时冻结参与合约（在途赔案）、合约/赔案版本与规则快照；同`reference`重复提交只回放同一批次（`idempotent_replay:true`），不产生第二份结果。
- `GET /api/batches` / `GET /api/batches/{id}`：批次列表与进度（`cursor`/`pending`/`estimated_total`）。
- `GET /api/batches/{id}/items?status=needs_verification`：批次明细，含版本、损失日期、估算依据、规则快照、变动原因。
- `POST /api/batches/{id}/resume`：断点续作；明细落库后才推进成员游标，写入失败可从完整批次恢复，重复执行幂等。
- `POST /api/batches/{id}/refresh`：仅草稿态可用。封账前赔案补录自动追加为成员；版本变化（撤销/核定金额变化/日期补录）只把受影响合约的旧明细置为`invalid`并按新版本重算，其余合约依据不动；已`sealed`/`posted`批次拒绝刷新，保留原依据。
- `POST /api/batches/{id}/items/{record_id}/repair`：对`needs_verification`明细人工补损失日期（`{"loss_date":"2026-09-28"}`）。旧数据缺损失日期时先按事件号从同事件记录回填（`loss_date_source=event_backfill`），补不全的合约停在`needs_verification`，阻止封账。
- `POST /api/batches/{id}/seal`：封账（claims_officer/finance）。存在未估算成员或待核对合约时拒绝；封账后冻结`estimated_total`。
- `POST /api/batches/{id}/post`：财务复核转正式准备金（仅finance），写`formal_total`与每条正式金额，并回写合约审计`reserve_posted`；重复提交幂等。
- `GET /api/batches/{id}/audit`：批次审计时间线（created/evaluated/refreshed/repaired/sealed/posted，含失效合约与变动原因）。
- `GET /api/batches/stats`：批次状态统计与明细分状态金额汇总。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及评估批次的封账转正式、失效重算、赔案补录、幂等提交、写盘后续作和旧数据损失日期回填。
