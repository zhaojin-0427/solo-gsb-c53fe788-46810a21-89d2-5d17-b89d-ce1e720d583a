# 事件时序矛盾排查台

用整数分钟变量 `t` 描述事件时间，编辑事件时间范围与先后/间隔约束，实时计算
**全体可行解的最紧上下界**；不可满足时给出**项数最少的矛盾输入项集合**，
并在时间轴与约束列表间联动定位。支持分叉版本、字段级三方合并与事务化提交。

技术栈：**Python 3.12 · FastAPI · SQLite · 原生 JavaScript（无前端构建）**。

## 语义约定

| 概念 | 含义 |
|---|---|
| 时间范围 | 同上下界的闭区间 `[L, U]`，任一侧可缺省（无下界 / 无上界） |
| A 早于 B | `tA + 1 ≤ tB` |
| A 晚于 B | `tB + 1 ≤ tA` |
| 同一时刻 | `tA = tB`（可通过把两侧范围设为同一值表达） |
| A 晚于 B 至少 n 分钟 | `tA − tB ≥ max(1, n)`，`n` 为非负整数 |
| A 晚于 B 至多 n 分钟 | `1 ≤ tA − tB ≤ n`（`n = 0` 时该单项即矛盾） |

每个**事件的时间范围**和每条**关系**各算一个输入项，输入项按全局创建序号标识。

- 求解：差分约束系统 `x_u − x_v ≤ w`，Floyd–Warshall 判负环；
  可行时输出每个事件的最紧下界/上界，无界一侧显示“**无下界/无上界**”。
- 不可满足：返回**项数最少**的矛盾集；多个最小时按**创建序号字典序**取最小者。
  （该问题一般情况下为 NP-hard，程序在时间预算内做精确枚举，病态实例退化为
  包含极小矛盾集，保证始终快速返回。）
- 性能：100 个事件、500 条可满足关系的边界求解约 < 100 ms（要求 ≤ 2 s）。

## 分支与合并

- **新建分叉**：从 main 最新版本复制工作区，并记录分叉基线版本。
- **字段级三方合并**（base = 分叉版本，mine = 当前分支，theirs = 来源分支）：
  - 双方改动无交叠的字段自动合入；
  - 同字段被双方改成不同值 → 冲突，逐项二选一；
  - 一方删除、另一方修改 → 删除/修改冲突，逐项选择保留或删除；
  - 双方都删除即删除；引用被删事件的关系自动丢弃；
  - 新增实体自动合入。
- **提交事务化**：合并提交在**同一 SQLite 事务**内复核
  ① 目标分支基线版本未被他人改动（乐观锁）② 合并结果可满足；
  任一失败整体回滚，**不会部分写入**。
- 普通编辑允许保存不可满足的中间状态（排查过程需要留痕），每次“提交版本”
  生成不可变快照，可在“版本历史”查看。

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
```

浏览器访问：

```
http://localhost:8000
```

数据通过命名卷 `tconsole-data` 持久化到容器内 `/data/tconsole.db`，
**停止/重建容器后数据保留**；`docker compose down -v` 才会清除数据卷。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 连同数据卷一起删除
```

### 端口与数据卷配置

编辑 `docker-compose.yml`：

```yaml
services:
  web:
    ports:
      - "8000:8000"          # 左侧改宿主机端口
    volumes:
      - tconsole-data:/data  # 或改为 ./data:/data 使用宿主机目录
```

## 本地开发（不使用 Docker）

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 数据库路径（默认容器内 /data/tconsole.db）
export DATABASE_PATH=./tconsole.db

uvicorn app.main:app --reload --port 8000
# 访问 http://localhost:8000
```

## 主要 HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/branches/{id}/state` | 事件、关系、求解结果（最紧界或矛盾集） |
| POST/PATCH/DELETE | `/api/branches/{id}/events[/{ref}]` | 事件增改删（删事件连带删相关约束） |
| POST/PATCH/DELETE | `/api/branches/{id}/relations[/{ref}]` | 关系增改删 |
| POST | `/api/branches` | 从某版本分叉（默认 main 最新版本） |
| POST | `/api/branches/{id}/commit` | 生成不可变版本快照 |
| GET | `/api/branches/{id}/versions` | 版本历史 |
| POST | `/api/branches/{id}/merges/preview` | 三方合并分析（自动合入项 + 冲突清单） |
| POST | `/api/branches/{id}/merges/commit` | 按逐项裁决事务化提交合并 |

合并提交体示例：

```json
{
  "source_id": 2,
  "expected_head": 17,
  "resolutions": {
    "fields":   {"E4.upper_bound": "theirs"},
    "entities": {"R9": "delete"}
  }
}
```

`expected_head` 为提交时目标分支的当前版本号；与库中不一致时返回 `409`。

## 界面操作

- 时间轴上**深蓝实心条**为最紧可行界，**浅蓝虚线条**为原始输入范围；
  红色条属于最小矛盾集。
- 顶部红色横幅中的 `#序号` 徽标点击后自动滚动并高亮对应时间范围/关系；
  点击时间轴事件标签可在右侧定位与之相关的全部约束。
- 当前分支选择保存在浏览器 `localStorage`，刷新页面后自动回到该分支；
  全部业务数据保存在 SQLite，刷新/重启不丢失。

## 测试

```bash
python -m tests.test_solver     # 求解器语义、最小矛盾集、2s 性能要求
python -m tests.test_crossval   # 800 随机实例与暴力枚举交叉验证
python -m tests.test_merge      # 字段级三方合并
python -m tests.test_api        # API、乐观锁、事务回滚端到端
```

## 目录结构

```
app/
  main.py      FastAPI 路由与事务编排
  solver.py    差分约束求解、最紧界、最小矛盾集
  merge.py     字段级三方合并
  db.py        SQLite 表结构、版本快照、事务工具
  static/      index.html / style.css / app.js（原生 JS）
tests/         四组测试
Dockerfile  docker-compose.yml  requirements.txt
```
