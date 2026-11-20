# 考古地层对接 API（FastAPI + SQLite）

管理同一考古遗址最多 40 个层位的探方编号、层位编号与可能沉积年代段（整年闭区间，
可含空档），登记跨探方层位对接与“下层 → 上层”叠压关系，并自动完成：

- **分组**：直接或间接对接的层位视为同一次沉积，合并为连通组，全组共用一个年份；
- **成环检测**：合组后的叠压关系（含同组自环）不得成环；
- **年代约束**：下层沉积年严格早于上层；同组年份必须落在各成员年代段的交集内；
- **可行年代段求解**：对每个组给出可与其他全部层位约束同时成立的所有年份
  （保留空档的闭区间列表）；
- **野外簿整本提交**：簿内层号只在本簿有效，按提交的对照解析成现有层位；
  同一对上下层记在多本簿或手工记录中时，求解只算一条叠压；
- **撤销重算**：删除对接/叠压/野外簿后按剩余关系重新分组、重新求解。

## 快速开始（docker compose）

```bash
docker compose up --build
# API:      http://localhost:8000
# Swagger:  http://localhost:8000/docs   （可直接在页面上录入数据）
# ReDoc:    http://localhost:8000/redoc
```

SQLite 数据文件保存在命名卷 `archaeo-data`（容器内 `/data/archaeo.db`），
`docker compose down` 不会删除；如需清空：`docker compose down -v`。

## 本地裸跑

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认在 ./data/archaeo.db，可用环境变量 DB_PATH 覆盖
```

## 年代表示约定

- 年份均为整数；公元后用正整数，公元前用负整数（如 `-221` 表示公元前 221 年）。
- 每个层位给一个或多个闭区间 `[year_from, year_to]`，多段之间允许空档
  （如 `[-5000,-4000]` 与 `[-3000,-2000]`，中间一千年无沉积可能）。
- 同组年代域 = 组内各层位区间的交集；求解结果 `feasible_years` 同样是带空档的区间列表。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sites` | 建遗址 |
| GET | `/sites` / `/sites/{id}` | 遗址列表/详情 |
| POST | `/sites/{id}/strata` | 录入层位（探方、层位号、年代段；每遗址上限 40） |
| GET | `/sites/{id}/strata` / `/strata/{id}` | 层位查询 |
| POST | `/sites/{id}/correlations` | 登记两探方层位对接 |
| GET | `/sites/{id}/correlations` | 对接列表 |
| DELETE | `/correlations/{id}` | 撤销对接，按剩余对接重算 |
| POST | `/sites/{id}/superpositions` | 登记叠压（下层 → 上层） |
| GET | `/sites/{id}/superpositions` | 叠压列表 |
| DELETE | `/superpositions/{id}` | 撤销叠压并重算 |
| PUT | `/sites/{id}/notebooks` | 按簿号整本提交野外簿（不存在则新建，已存在则整本替换） |
| GET | `/sites/{id}/notebooks` / `/notebooks/{簿号}` | 野外簿列表 / 详情 |
| DELETE | `/sites/{id}/notebooks/{簿号}` | 撤去整本出处（只影响本簿） |
| GET | `/sites/{id}/superposition-sources` | 生效的上下层对及全部出处（簿号页行号、手工记录号） |
| GET | `/sites/{id}/docs/superpositions` | 同上，位于 `/docs` 的出处查询入口 |
| GET | `/sites/{id}/solution` | 整站分组、组间边、各组全部可行年代段 |
| GET | `/strata/{id}/correlation-view` | 按原始层位查同组成员与可行年代段 |

### 录入示例

```bash
SID=1
# 层位：探方 T1 第 3 层，两个可能年代段（中间有空档）
curl -X POST localhost:8000/sites/$SID/strata -H 'Content-Type: application/json' -d '{
  "trench": "T1", "layer_no": "L3",
  "intervals": [{"year_from": -5000, "year_to": -4000},
                {"year_from": -3000, "year_to": -2000}]
}'
# 叠压：下层 1 -> 上层 2
curl -X POST localhost:8000/sites/$SID/superpositions \
  -H 'Content-Type: application/json' -d '{"lower_id": 1, "upper_id": 2}'
# 跨探方对接：层位 1 与层位 3 属同一次沉积
curl -X POST localhost:8000/sites/$SID/correlations \
  -H 'Content-Type: application/json' -d '{"stratum_a_id": 1, "stratum_b_id": 3}'
```

## 野外簿（层号只在本簿有效）

野外记录里的层号只在**本簿**内有效，提交时随簿给出“簿内层号 → 现有层位 id”的
对照，以及带页行号的下层→上层记录：

```bash
curl -X PUT localhost:8000/sites/$SID/notebooks -H 'Content-Type: application/json' -d '{
  "notebook_no": "NK-2024-01",
  "layers": [
    {"label": "3", "stratum_id": 1},
    {"label": "2", "stratum_id": 2}
  ],
  "superpositions": [
    {"lower_label": "3", "upper_label": "2", "page": 12, "line": 4}
  ]
}'
```

- 同一 `notebook_no` 再次 `PUT` 即**整本替换**：旧对照与旧页行记录全部删除，
  本簿所有行按新对照重新解析。`replaced` 说明是新建还是替换。
- 出处可以有多处：同一对 `(下层, 上层)` 记在多本簿里、或已有手工叠压记录时，
  求解器只得到**一条**边（`counted_as: 1`），不重复施加约束。
- 手工登记（`POST /superpositions`）与按原记录号撤销
  （`DELETE /superpositions/{id}`）仍可独立操作；替换/删除簿只改本簿的出处。
- 逐一撤去出处，**最后一处**出处消失后，该叠压才从年代求解中移除。
- 出处与年代结论同属一次提交：`GET /sites/{id}/superposition-sources` 与
  `GET /sites/{id}/solution` 返回同一个 `version`，重启后仍可查询。

### 引用无效或合并后无解

提交会在**同一事务**内校验引用并试解，失败时事务回滚，库内旧簿、有效关系与
年代结论原样保留，接口返回 `409`：

- 引用问题：`issues[]` 逐条给出本簿的 `page` / `line` / `lower_label` /
  `upper_label` 与可读原因（层号未对照、对照的层位不存在或不属于本遗址、
  上下层指向同一层位等），整本一行都不写入；
- 合并后无解：`issues[]` 列出本簿中卷入矛盾的页行，`contradiction` 给出
  跨来源的原始记录见证（叠压见证附带 `sources`，含簿号页行与手工记录号），
  `previous_solution` 为回滚后的可行结论。

## 无解时的行为

写入会在**同一事务**内先重算再提交。若新记录导致无解（`EMPTY_DOMAIN` /
`GROUP_CYCLE` / `ORDER_CONFLICT`），事务回滚，接口返回 `409`，响应体包含：

- `contradiction`：一组本身即矛盾的**原始记录见证**（层位年代段、对接、叠压，
  含可读原因与记录 id，便于定位；叠压见证的 `sources` 列出全部出处）；
- `previous_solution`：回滚后库内已有对接与年代结论（保持可行、保持可用）。

## 一致性与并发

- 所有写操作经进程内同一把锁串行化，按**提交顺序**核算；每次成功写入使全局
  `version` 加 1，查询结果带 `version`，可据此确认读到的是哪一次已完成提交。
- 分组与年代结论**不物化落库**，每次查询及重启都由原始记录现场求解；
  野外簿的层号对照与页行记录同样落库，查询时按 `(下层, 上层)` 归并出处后
  送入同一个求解器。原始记录由 SQLite 事务保证原子性，因此任何查询看到的分组/
  年代都来自同一次完整提交，不存在半写入的中间态。
- SQLite 启用 WAL 与外键约束，连接带 30s busy timeout。

## 求解算法（`app/solver.py`）

1. 并查集按对接关系求连通组；组域为成员年代域交集（支持空档的区间运算）。
2. 叠压边提升为组间边；Kahn 拓扑排序判环，DFS 三色标记提取具体环与见证。
3. DAG 上两次贪心：
   - 最早解 E：拓扑序每组取域内 `> max(E[前驱])` 的最小年份；
   - 最晚解 L：逆拓扑序每组取域内 `< min(L[后继])` 的最大年份。
   任意可行解 F 都满足 E ≤ F ≤ L，故组 g 的全部可同时成立年份恰为
   其年代域 ∩ [E[g], L[g]]（仍可为含空档的多段区间）。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖区间工具、缺口域求解、成环/空域/次序冲突及矛盾见证、40 层上限、
无解拒绝与旧结论保留、撤销重算、并发写入串行化、重启后结论恢复；
以及野外簿整本提交/整本替换、跨簿与手工记录去重、引用无效定位页行、
合并后无解保留旧簿、最后一处出处撤去后移除叠压、重启后出处仍可查。
