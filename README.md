# 机房配电检修 · 数据库集群断电演练台（Flask + Docker Compose）

值班员在检修配电支路前，用本页面判断：断开某条支路会不会让三副本数据库集群停服、
连锁跳闸会跳哪些支路、复电按什么顺序合哪些开关、以及什么时候凑齐至少两个就绪副本恢复服务。

- 后端：Python 3.11+/Flask 3，单进程内存演练状态，`threading.RLock` 保证“支路操作 / 就绪上报”交错时一致。
- 前端：服务端渲染 + 原生表单（无构建、无外部 CDN），少量 JS 仅用于把输入框改成旧编号。
- 启动：Docker Compose（见下），也可直接 `python app.py`。

---

## 1. 用 Docker Compose 启动

```bash
cd <仓库目录>
docker compose up --build -d          # Compose v2；或 docker-compose up --build -d
# 打开 http://localhost:8000
docker compose logs -f                # 内置 healthcheck：GET /healthz
docker compose down
```

仓库同时提供 `compose.yaml` 与等价的 `docker-compose.yml`，兼容 v2 / v1 命名习惯。
容器固定 `gunicorn --workers 1 --threads 8`：演练状态在进程内存中，多 worker 会各持一份状态，
因此必须单 worker（多线程足以应对多人/并发上报）。

不用 Docker 时：

```bash
python3 -m pip install -r requirements.txt
python3 app.py                        # http://127.0.0.1:8000 ，等价于 flask run
# 或： gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 app:app
```

---

## 2. 内置样例与推荐演练剧本（网页复现连锁跳闸 + 旧编号拒报）

页面顶部可在两个教案间切换，并有“复位演练”。初始为健康基线：三服务器均有电、启动编号 `#1`、
三副本就绪、绿色“集群可用”。

### 样例一（默认）· 交叉双路：同轮双跳 + 次轮连锁

单机整机功耗 **1000 W**，双电源交叉挂在 4 条支路，每条支路都标明功率上限：

| 支路 | 上限 | 供电馈线 |
|---|---:|---|
| **WL1** A列母线支路 | **2200 W** | db-1 电源1、db-2 电源1 |
| **WL2** 机柜1 B路 | **750 W** | db-1 电源2 |
| **WL3** 跨接支路 | **1000 W** | db-2 电源2、db-3 电源1 |
| **WL4** 机柜3 B路 | **750 W** | db-3 电源2 |

网页操作与预期：

1. 在 **WL1** 卡片点“断开 WL1”。
2. “本次操作 · 每轮跳闸支路”显示：
   - **第 1 轮 · 同时跳闸：`WL2`（1000W > 750W）、`WL3`（1500W > 1000W）**；
   - 按剩余支路重算后 **第 2 轮：`WL4`（1000W > 750W）**。
   - 三服务器“两路都断”全部掉电，就绪标记清空，顶部红色“数据库集群不可用（就绪副本不足 2 个）”。
3. 掉电期间直接给 db-1“确认就绪” → 红色记录“服务器当前掉电…不予接受”。
4. 复电到法定多数：合 **WL1**（db-1/db-2 满载共 2000W ≤ 2200W，不跳），再合 **WL2**（500W ≤ 750W）。
   db-1/db-2 卡片显示 **`#2` 启动编号**和“复电后的新启动编号”徽标；db-3 仍掉电、编号保持 `#1`。
5. 在 db-1 卡片点 **“用旧编号试报”**（或把输入框改成 `1` 再确认）：
   上报携带旧启动编号 `#1`，页面和“就绪上报校验记录”给出
   **“启动编号 #1 已失效：当前启动编号为 #2，只接受当前编号的就绪上报”**，该副本不会就绪。
6. 输入当前编号 `2` 后“确认就绪”：1/3 就绪时集群仍不可用；给 db-2 也用 `2` 报就绪，
   **2/3 → 顶部变绿“数据库集群可用（至少 2 副本就绪）”**。
7. （可选）完整恢复 db-3：勾选 **WL3、WL4**，点右上角 **“同时合闸勾选支路”**。
   两条并列合下后 db-3 双路各摊 500W，不再跳闸，db-3 启动编号变为 `#2`，确认后 3/3。
   注意：单独合 WL3 会因 `db-2 半载 500W + db-3 满载 1000W = 1500W > 1000W` 立即再次跳闸
   （页面会如实显示这次“合闸后再跳”的轮次）；单独合 WL4 也会因 1000W > 750W 跳开。

### 样例二 · 共母线链式：两轮连锁 + 纯顺序完整复电

WL1（2000W，带三台电源1）、WL2（3000W，带 db-1/db-2 电源2）、WL3（500W，带 db-3 电源2）。

- 断开 **WL2** → 第 1 轮 **WL1**（2500W > 2000W）→ 第 2 轮 **WL3**（1000W > 500W）。
- 逐条手动合闸 **WL2 → WL1 → WL3**，每一步都不超载，三台全部复电并得到新启动编号 `#2`。

两个教案都可随时“复位演练”。

---

## 3. 模型与实现说明

### 3.1 负载分摊（`power_model.py: compute_loads`）

对每台双路服务器，设当前有 `k` 条馈线支路处于合闸：

- `k = 2`：每条支路分摊 `P / 2`（双路平分整机功耗）；
- `k = 1`：该支路承担 `P / 1 = P`（只剩一路承担全部功耗）；
- `k = 0`：服务器掉电，不取流。

支路负载 = 所有挂在它上面且有电分摊的服务器份额之和。

### 3.2 连锁跳闸（`power_model.py: run_cascade`）

每轮基于当前合闸支路计算全部负载，挑出所有 `负载 > 功率上限`（严格大于；恰好等于上限算满载不跳）
的支路，**作为同一轮一次性全部移除（同时跳闸）**，再对剩余支路重算；重复直到没有超载。
每轮记录支路编号、跳闸瞬间负载与上限，供页面按轮展示。跳闸后没有任何自动重合逻辑，
只能由值班员手动“合闸”；若合闸后仍超载，会再次进入同一套轮次算法并可能再跳。

### 3.3 掉电、启动编号与就绪校验

- 只有当服务器的两条馈线都断开（`k=0`）时才判定掉电；单路断开/恢复不重启、启动编号不变、已就绪状态保留。
- 状态在一次操作前后做稳定态对比：`有电 → 无电` 时清空就绪标记；`无电 → 有电` 时
  **`boot_no += 1`（复电后的新启动编号）**并要求重新确认就绪。
- `POST /api/replicas/<id>/ready` 仅当 ① 服务器当前有电 ② 上报的 `boot_no` 等于当前启动编号
  才接受；否则返回 `409`，错误码分别为 `unpowered` / `stale_boot`，并在页面留痕。
- 集群可用条件唯一：**已接受的就绪副本数 ≥ 2**。
- 所有状态变更在同一把 `RLock` 内完成，交错并发下不会出现“旧编号被接受 / 就绪但掉电”。

### 3.4 页面与接口

- `GET /`：服务端渲染的演练台（支路配电盘 + 功率条、副本卡片、每轮跳闸时间线、上报校验记录）。
- 浏览器表单走 `/ui/...`，flash + PRG 重定向，**禁用 JS 也能完整演练**；
  “用旧编号试报”是一个 `name=mode value=stale` 的提交按钮，服务端故意取 `当前编号-1`。
- JSON API：

```text
GET  /api/state
POST /api/reset                      {"sample":"cross|chain"}
POST /api/branches/<WL>/disconnect
POST /api/branches/<WL>/close
POST /api/branches/close-many        {"ids":["WL3","WL4"]}
POST /api/replicas/<db>/ready        {"boot_no":2} 或 {"stale":true}
GET  /healthz
```

curl 复测旧编号：

```bash
curl -s -X POST localhost:8000/api/branches/WL1/disconnect ; \
curl -s -X POST localhost:8000/api/branches/WL1/close ; \
curl -i -X POST localhost:8000/api/replicas/db-1/ready \
     -H 'Content-Type: application/json' -d '{"boot_no":1}'
# HTTP/1.1 409 CONFLICT  "code":"stale_boot","current_boot_no":2
```

---

## 4. 交付前实测结果（本仓库内真实执行）

执行环境：Debian 12 / Python 3.11.2；**该沙箱没有 docker CLI/daemon**，因此 Compose 的实际
`build/up` 无法在此执行（见“余项”）。改用与镜像相同的依赖与 gunicorn 启动命令做了真机验证。

1. **单元 + HTTP 测试：19/19 通过**（`python -m unittest discover -s tests`），覆盖：
   双路平分/单路满载/两路全断、**同轮多支路同时跳闸**、多轮重算到稳定、启动编号只在完整掉复电时
   递增、掉电与旧编号拒报、法定 2 副本、两个教案的黄金路径、并列合闸，以及
   **8 线程 × 60 次“支路操作/随机编号上报”交错的状态不变量**。

   ```text
   Ran 19 tests in 1.149s
   OK
   ```

2. **对 gunicorn 实跑服务的端到端脚本：23/23 通过**
   （`python3 scripts/e2e_check.py http://127.0.0.1:8000`，仅用标准库）：

   ```text
   ✅ 断开 WL1 后跳闸轮次为 [WL2,WL3] -> [WL4] 实际=[['WL2', 'WL3'], ['WL4']]
   ✅ 连锁后 0/3 有电、集群不可用
   ✅ 掉电中就绪上报返回 409/unpowered HTTP 409
   ✅ 合 WL1 后 db-1/db-2 启动编号为 #2
   ✅ 旧启动编号 #1 被拒（409/stale_boot，当前 #2） HTTP 409
   ✅ 当前启动编号 #2 被接受
   ✅ 仅 1/3 就绪时集群不可用
   ✅ db-2 就绪后 2/3、集群可用
   ✅ 单独合 WL3 因 1500W>1000W 再次跳闸
   ✅ WL3/WL4 同时合闸成功且无跳闸；三服务器全部复电
   ✅ db-3 用新编号 #2 就绪，3/3
   ✅ 网页 HTML 含 '第 1 轮' 'WL2' 'WL3' '第 2 轮' '2200 W' '数据库集群可用' '旧编号' …
   全部 23 项实测通过
   ```

3. **完全按网页表单契约（urlencoded + Cookie/flash）用 curl 复现**，保存渲染后 HTML 核对：
   - 断开 WL1 后：页面同时含 `第 1 轮 · 同时跳闸`、`第 2 轮 · 同时跳闸`、
     `数据库集群不可用（就绪副本不足 2 个）`，三个启动编号仍为 `#1`；
   - 合 WL1/WL2 后用表单提交 `boot_no=1`：渲染页含
     `启动编号 #1 已失效` 与记录徽标 `旧编号 #1 被拒（当前 #2）`，两张副本卡为 `#2`；
   - 再用 `boot_no=2` 确认 db-1/db-2 后：页面含
     `数据库集群可用（至少 2 副本就绪）`、`2/3 副本就绪`；
   - `POST /ui/branches/close-many`（`ids=WL3&ids=WL4`）返回 302，随后 `/api/state`
     显示 4 条支路全合、`powered_count=3`。
   - 静态资源 `style.css`、`app.js` 均 `200`。

4. **交付物校验**：`compose.yaml` 与 `docker-compose.yml` 均通过 PyYAML 解析，
   端口 `8000:8000`、build context `.`、healthcheck 齐全；
   在全新目录 `pip install -r requirements.txt --target …` 成功解析安装
   `Flask 3.1.3 / gunicorn 23.0.0 / Werkzeug 3.1.9 / Jinja2 3.1.6`；
   并用 `gunicorn --bind … --workers 1 --threads 8 app:app` 实跑通过上述全部检查。

---

## 5. 余项与已知限制（如实说明）

1. **未在本沙箱真正执行 `docker compose up --build`**：环境中没有 docker 可执行文件与
   `/var/run/docker.sock`。已完成 Compose YAML 合法性、依赖全新安装、以及用 Dockerfile 同款
   gunicorn 命令实跑验证；首次在带 Docker 的机器上仍建议实际 `--build` 一次确认基础镜像拉取。
2. **未做真实浏览器像素级截图**：环境无 Chromium/Playwright；已通过真实 HTTP 服务 +
   服务端渲染 HTML 的关键串断言、表单 302/Cookie 流程验证页面行为，未做视觉回归。
3. **状态为进程内存态**：容器重启或 `docker compose restart` 后回到初始健康基线；
   未接数据库/卷持久化，也没有多副本部署与鉴权——定位是内网单人/小班演练工具。
   若要横向扩多 worker，需要把 `Drill` 状态外置（Redis/DB）或保持粘性单 worker。
4. **模型采用稳态额定功耗**：不模拟上电浪涌、UPS/STS 切换时延与机柜散热；
   “合闸后同一轮即跳开”的瞬时上电不计入启动编号，只在稳定的“无电→有电”跳变时发新编号。
5. **样例一 db-3 是刻意的教学约束**：因其两条支路上限分别低于单路满载，顺序单合无法恢复，
   必须 WL3/WL4 并列合闸（页面支持）或检修后更换/增容开关；样例二提供纯顺序全恢复路径。
6. 容量恰好等于上限按“满载但不跳”处理（严格 `>` 才算超载）；若现场规程要求达到额定值即告警/跳闸，
   把 `power_model.py` 中比较改为 `>=` 并同步样例容量即可，测试中有明确用例可调整。

---

## 6. 目录结构

```text
app.py                 # Flask 路由 / JSON API / 表单 PRG / 全局锁与状态
power_model.py         # 负载分摊、同轮同时跳闸算法、Drill 状态机与视图模型
samples.py             # 两个内置三副本双路供电样例（容量与黄金路径）
templates/index.html   # 演练台页面
static/style.css       # 样式
static/app.js          # 少量渐进增强 JS（无 JS 也可完整操作）
tests/test_drill.py    # 19 个 unittest（规则 + HTTP + 并发交错）
scripts/e2e_check.py   # 对运行中服务的 23 项端到端复测（标准库）
Dockerfile             # python:3.12-slim + gunicorn 单 worker
compose.yaml           # Docker Compose v2
docker-compose.yml     # 等价的 v1 文件名
requirements.txt       # Flask==3.1.3, gunicorn==23.0.0
```
