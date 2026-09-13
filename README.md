# 教学型水库水量分配模拟系统

一个面向课堂的**有限时间步水量账 + 线性规划配水**模拟器。学员可以：

1. **逐个时段回答**：水库里的水从哪里来（入流）、到哪里去（蒸发、各类取水、弃水、蓄存）；
2. **比较不同供水方案**：修改公开权重、缺测插补规则、溢洪道能力等，一键并排对比；
3. **锁定一个方案再调另一个**：锁定的方案参数不动，作为对照基准；
4. **每张图都能点回具体时段**，逐行核对水量平衡：

```
期初蓄水 + 入流 = 蒸发 + Σ各类取水 + 弃水 + 期末蓄水
```

> ⚠ **本系统仅用于教学演示**。所有内置数据均为虚构；系统**不连接任何真实闸门或 SCADA**，
> 输出**不替代现实供水/防洪调度决策**。

## 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 求解 | SciPy `linprog`(HiGHS) | 逐时段 LP：供水按公开权重最大化；生态流量、死库容/最高库容为硬约束 |
| 后端 | FastAPI + SQLAlchemy(async) | 单位换算、缺测处理、求解编排、冲突时段诊断 |
| 前端 | Angular 18（standalone + signals）+ Chart.js | 库容/水位、进出水堆叠、取水与缺口图；点击图表追溯时段 |
| 数据库 | PostgreSQL 16 | 场景与方案（含锁定标记）；本地可用 SQLite 零配置启动 |
| 部署 | docker-compose | db + api + web 三容器 |

## 目录

```
backend/app/core/
  units.py       单位层：所有输入带单位（m³、万m³、m³/s、l/s、mm、亩、h…），缺测(None)直接报错不当零
  curve.py       库容曲线：水位↔库容分段线性插值，水面面积推求；蒸发=净水深×平均面积
  missing.py     缺测来水：none(拒绝)/linear/mean/zero(显式) 四种策略，插补时段全程打标
  optimizer.py   逐时段 LP（定点迭代处理蒸发对期末库容的依赖）
  diagnose.py    无可行解时的前向模拟：定位 flood（洪峰超容）/ drought（生态与死库容冲突）时段
frontend/        Angular 单页：场景选择、权重调整、锁定对比、图表-账目联动追溯
```

## 快速开始

### 方式 A：docker-compose（推荐，含 PostgreSQL）

```bash
docker compose up --build
# 前端 http://localhost:4200 （ng serve，热更新）
# API  http://localhost:8000/docs
```

### 方式 B：本地开发（SQLite 零配置）

```bash
# 后端
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_URL="sqlite+aiosqlite:///./dev.db" uvicorn app.main:app --reload --port 8000

# 前端（另开终端）
cd frontend
npm install
npm start            # http://localhost:4200 ，/api 自动代理到 8000

# 生产态单端口：构建后由 FastAPI 直接托管
npm run build
WEB_DIST=../frontend/dist/reservoir-frontend uvicorn app.main:app --port 8000
# 打开 http://localhost:8000
```

## 课堂演示剧本（内置 5 个场景，首启自动入库）

| 按钮 | 教学点 | 预期现象 |
|---|---|---|
| **常规来水** | 权重如何决定缺额分摊 | 生活优先：农业缺约 962 万m³；农业优先：工业缺 900 万m³、生活仍受 90% 硬保证 |
| **零来水** | 真实的零 ≠ 缺测；库存耗尽与生态底线 | D6 起报 `drought` 冲突，列出具体时段与缺口水量 |
| **洪峰超容** | 洪峰超过可弃空间时没有"神仙方案" | D10/D11 报 `flood`，给出超额水量（约 105/405 万m³） |
| **混合单位** | 先换算、再算账 | 入流 m³/s、生态 l/s、时长 h、库容 m³，结果与常规场景逐笔一致 |
| **来水缺测** | 缺测不能悄悄当零 | 直接求解返回 422 并点名 D5/D6；选"线性插补"后可解，图表与账目带 `插补` 标记 |

演示对比流程：打开"常规来水" → 改权重点"重新求解" → "🔒 锁定此方案作为对照"
→ 再改另一组权重 → 左右两屏逐图、逐时段对照。

## 硬条件与公开权重

- **硬条件（违反即无可行解）**：死库容 ≤ 各时段库容 ≤ 最高蓄水量；
  各类供水 ≥ 最低保证比例 × 需求；生态流量 `Σ取水 + 弃水 ≥ R_eco`；溢洪道过流上限。
- **公开权重**：生活/工业/农业默认 3∶2∶1（界面可改、归一化后显示），
  LP 目标为 `max Σ w_j·供水量`，弃水与蓄水只有破平局的微小 ε 项。
- **找不到可行方案**：后端不返回模糊错误，而是给出逐时段前向诊断，
  标明首个/全部冲突时段、冲突类型和水量缺口。

## 测试

```bash
cd backend && pytest          # 11 项：单位、守恒闭合、权重优先、缺测、洪峰、混单位一致性
```
