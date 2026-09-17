# 事件时序矛盾排查台

基于 **Python + FastAPI + SQLite + 原生 JavaScript** 的 Web 应用，用于在整数分钟
时间轴上编排事件、施加时序关系、求解全体可行解的最紧边界，并在约束不可满足时
定位项数最少的矛盾集；支持分叉分支、字段级三方合并与冲突逐项裁决。

## 一、配置与启动（Docker Compose）

需要 Docker Engine 20.10+ 与 Docker Compose v2。

```bash
# 在项目根目录构建并启动
docker compose up -d --build

# 查看状态 / 日志
docker compose ps
docker compose logs -f

# 停止
docker compose down
```

启动后访问：

> **http://localhost:8000/**

- 端口可在 `docker-compose.yml` 的 `ports` 中修改（默认 `8000:8000`）。
- SQLite 数据库与 WAL 文件持久化在宿主机 `./data` 目录（挂载到容器
  `/app/data`）；首次启动自动建表并创建 `main` 分支的初始空提交。删除
  `./data/app.db*` 即完全重置。
- 容器健康检查：`GET /` 返回 200。

### 不使用 Docker 的本地运行方式

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 二、时序语义（整数分钟变量 t）

所有关系都以 **A 相对 B 的方向**指定先后，禁止只写“间隔 n 分钟”而不带方向。

| 关系 | 形式化定义 |
|---|---|
| A 早于 B | t_A + 1 ≤ t_B（即 t_A − t_B ≤ −1） |
| A 晚于 B | t_B + 1 ≤ t_A（即 t_B − t_A ≤ −1） |
| A 与 B 同一时刻 | t_A = t_B |
| A 晚于 B **至少 n 分钟** | t_A − t_B ≥ max(1, n)，n 为非负整数 |
| A 晚于 B **至多 n 分钟** | 1 ≤ t_A − t_B ≤ n（n = 0 时该单项自身即矛盾） |

- 事件的“时间点”是一个**闭区间**（整数分钟），上下界各可缺省一侧：
  `lower ≤ t ≤ upper`；只填下界、只填上界、全不填均可。
- 求解结果中某一侧无界时显示 **“无下界” / “无上界”**。
- 每次编辑提交后，求解全体可行解上的**最紧**上下界（可实现的最大值/最小值）。

## 三、输入项与最小矛盾集

- **每个事件的时间范围、每一条关系各算一个输入项**；输入项在创建时获得全局
  递增的“创建序号”（界面中以 `#序号` 徽章显示，永不复用）。
- 约束不可满足时，返回**项数最少的矛盾集**（即负环上的输入项集合，单项矛盾
  如“下界 > 上界”“至多 0 分钟”也会被识别为 1 项）。
- 当存在多个同样大小的矛盾集时，按各自创建序号集合的**字典序**选最小者
  （先比最小序号，再依次比较）。
- 矛盾项在时间轴、事件列表、关系列表、状态栏中同步高亮，点击徽章可联动定位。

## 四、求解算法（`app/solver.py`）

- 全部约束统一写成差分约束 `t_v − t_u ≤ w`（有向边 u→v 权 w），并加入固定
  原点 t₀ = 0 表达绝对时间上下界。
- 可行性 = 约束图无负环（Bellman–Ford）；上界 = 原点最短路，下界 = 反图
  最短路取负；不可达即为无界。
- 最小矛盾集 = 含最少不同输入项的负环：
  1. 分层矩阵最短路 DP 精确求出最小负环长度 k*（O(k*·|E|·|V|)）；
  2. 再做带（权重、序号得分）双目标的 Pareto 分层搜索，按创建序号集合
     字典序选出最优环。
- 性能：**100 个事件、500 条可满足关系**的边界求解在普通机器上约
  **几十毫秒**（SLA 2 秒），见下方自检脚本。

## 五、分支与字段级三方合并

- 顶部可从任意分支的当前版本「分叉新建」分支；每个分支是一条提交链
  （合并提交记录来源分支）。
- 合并以分叉点（提交 DAG 上的最近公共祖先）为 base，做**字段级三方合并**：
  - 双方改动**无交叠**（不同字段、不同实体、新增互不相同等）→ 自动合入；
  - **同一字段双方都改成不同值** → 冲突，逐项选择「当前分支 / 源分支」；
  - **一方删除、另一方修改** → 删除/修改冲突，逐项选择；
  - 双方都删除 → 自动删除。
- 合并对话框先「预览」（冲突清单、自动合入清单、合并结果可满足性分析），
  每做一次选择都会用当前全部裁决重新计算预览；全部冲突裁决后才能提交。
- **提交在同一个 SQLite 事务内**：
  1. 复核请求携带的基线版本是否仍为该分支 HEAD（乐观并发，过期返回 409）；
  2. 应用编辑/合并结果后复核整体**可满足性**；
  任一失败立即回滚，**不会产生部分写入**（seq 计数、快照、HEAD 全部不变）。

## 六、界面与数据保留

- 左栏：事件与时间范围（可就地编辑、清空某一侧使其无界、删除；删除事件会
  级联标记其关联关系）。
- 中栏：可行解时间轴 SVG，色条表示最紧可行区间，缺界方向延伸到轴端并以
  「无上界/无下界」标注；可缩放、自适应；与两侧列表及状态栏联动定位。
- 右栏：关系列表与新增表单，表单实时显示所填关系的数学含义。
- **所有数据保存在服务端 SQLite**，刷新页面、重启浏览器、重启容器（保留
  `./data` 挂载）后数据与分支均保留；当前选中的分支名存于 localStorage。

## 七、HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state?branch=main` | 完整状态 + 最紧边界（或最小矛盾集） |
| GET | `/api/branches` | 分支列表 |
| POST | `/api/branches` | 从某分支（指定版本）分叉 |
| GET | `/api/branches/{name}/history` | 提交历史 |
| POST | `/api/branches/{name}/commit` | 基于 `base_id` 提交一批编辑操作 |
| POST | `/api/branches/{name}/merge-preview` | 三方合并预览（含裁决） |
| POST | `/api/branches/{name}/merge` | 事务内复核并提交合并 |

提交操作（ops）示例：

```json
[
  {"op": "event_create", "label": "开工", "lower": 10, "upper": null},
  {"op": "event_update", "id": "…", "clear": ["upper"]},
  {"op": "relation_create", "type": "at_least", "a": "<事件id>", "b": "<事件id>", "n": 15},
  {"op": "relation_delete", "id": "…"}
]
```

错误码：`400` 输入非法；`409/stale_base` 基线过期；
`409/unresolved_conflicts` 合并冲突未裁决完；
`422/unsatisfiable` 不可满足（响应体携带最小矛盾集）。

## 八、自检

```bash
pip install httpx          # 仅 TestClient 运行测试时需要
python3 scripts/test_solver.py   # 求解器正确性 + 性能用例
python3 scripts/test_api.py      # 63 项端到端断言（会重建 data/app.db）
```

## 九、目录结构

```
app/
  main.py      FastAPI 路由与事务边界
  db.py        SQLite 连接、提交快照、LCA
  model.py     编辑校验、差分约束建模、全量分析
  solver.py    Bellman–Ford 边界求解 + 最小负环（最小矛盾集）
  merge.py     字段级三方合并
static/        原生 JS 单页（index.html / app.js / style.css）
scripts/       求解器与端到端自检脚本
Dockerfile, docker-compose.yml, requirements.txt
```
