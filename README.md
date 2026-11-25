# 乡村广播太阳能备用班次

岭声乡村广播的节目组和电源值守共用一张备播卡，在版权离线时段安排本地校时与蓄电余量。

## 现场工作与联系人

模拟客户为岭声乡村广播合作社。广播时刻表、蓄电日志、用量周期预测、备用机优先级；节目版权离线时段只能接本地时钟。

- 节目时段表 `airslots`，字段为 `title`（节目）、`hour`（起播小时）、`license`（节目源许可）、`offline`（离线时段）。模型约束位于 `backend/domain/airslots_model.py`，规则位于 `airslots_service.py`。
- 蓄电班记与测量 `battery_logs`，字段为 `meter`（电表编号）、`hour`（两日内整点）、`load_wh`（用量 Wh）、`clear`（天空晴）、`verified`（抄表审核）。模型约束位于 `backend/domain/battery_logs_model.py`，规则位于 `battery_logs_service.py`。
- 备用分钟核算 `reserves`，字段为 `airslot`（节目时段）、`plan_wh`（计划备用 Wh）、`fallback`（备用源优先级）、`mae`（时间验证MAE Wh）、`token`（计算号）。模型约束位于 `backend/domain/reserves_model.py`，规则位于 `reserves_service.py`。
- 班组备播卡 `switchcards`，字段为 `reserve`（备用评估）、`source`（实际时钟源）、`approved_wh`（锁定备用 Wh）、`state`（状态）。模型约束位于 `backend/domain/switchcards_model.py`，规则位于 `switchcards_service.py`。

团队为模拟的6 人广播合作社值守组，负责两个电源档位的备播。

值守员用节目表和电池抄数安排断电时段。周期负荷难从表格读出，漏掉一小时会扭曲拟合，离线版权段容易误选台外时钟。

节目时刻表和已签电量日志进入估计，接班人拿到有限额与时钟源的备用卡。

### 一天的业务操作

1. 07:00 确认 AIR-17 晚间村讯版权离线。
2. 08:00 班组登记 BAT-11 整点 88.4 Wh。
3. 09:00 核对 BAT-04 两日班记。
4. 11:20 用 24 小时 Fourier 基回测末 6 小时。
5. 15:00 签发 RV-24 备用电量评估。
6. 17:45 台外时钟请求被拦后改用本地时钟。
7. 22:10 持 WR-77 完成闭台交接。

## 启动

```sh
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```

打开 `http://127.0.0.1:18317`。FastAPI OpenAPI 在 `http://127.0.0.1:19317/docs`。
Compose 运行 Nginx、Python FastAPI、独立 Redis 消费进程、SQL 数据库和 Redis，SQL 与 Redis 使用命名卷。
本机开发时，在 Python 3.11 环境安装 `backend/requirements.txt` 后从 backend 目录运行 `uvicorn core.app:app`。须提供 `DATABASE_URL`、`REDIS_URL`、`JWT_SECRET`，`SEED_DEMO` 控制演示初始化。

| 演示账号 | 权限 |
|---|---|
| operator@demo.test | 提交领域记录和计算请求 |
| reviewer@demo.test | 提交记录并执行有业务约束的复核操作 |
| viewer@demo.test | 浏览业务台账 |

三个本地演示账号均使用 `DemoC0de!2026`。角色在 `backend/core/roles.py` 和 `frontend/src/store.js` 中声明。计算状态在 worker、queueing、API 和同一前端 store 中使用。

## 页面与接口

| 业务任务 | FastAPI 路由 | 页面 |
|---|---|---|
| 节目时段表 | `/airslots` /airslots/mark-offline | `frontend/src/pages/airslots.js` |
| 蓄电班记与测量 | `/battery_logs` /battery_logs/log-charge，/battery_logs/backfill，/battery_logs/approve-meter，/battery_logs/confirm-shift，/battery_logs/pending-shifts | `frontend/src/pages/battery_logs.js` |
| 备用分钟核算 | `/reserves` /reserves/forecast-wh，/reserves/save-reserve | `frontend/src/pages/reserves.js` |
| 班组备播卡 | `/switchcards` /switchcards/issue-switch，/switchcards/handover | `frontend/src/pages/switchcards.js` |

SQL 实体与约束在 `backend/domain` 下维护。前端使用原生 ES modules。核心算子为 Fourier 基 + OLS。OLS 在训练时段拟合一、二阶谐波和晴天标记。备用量为截零后的点预测加末 6 小时 MAE。

### 抄数复核、尖峰挂起与换档确认

同一只电表有至少 8 条更早的已审签读数后，`domain/review_engine.py` 用这些读数拟合 Fourier+OLS，给出预测与历史误差带（MAE / 残差尺度与额定相对容差取大）。每笔抄数在 `GET /battery_logs` 都带 `verdict` / `verdict_label`（页面“判定”列）：

- `in_band` 误差带内，复核员 `approve-meter` 后审签；历史不足时为 `history_short`，按纸本基线审签为 `baseline`。
- 孤立的同向超带判为 `spike`（尖峰，疑似单次抄错），连续 2 小时为 `watch`，均保持 `verified=false` 挂起。
- 连续至少 3 小时同向超带形成 `shift_pending` 待审换档段，`GET /battery_logs/pending-shifts`（及 `/battery_logs/reviews` 等别名）返回段范围、`range_token` 与整数 `version`。
- 复核员 `POST /battery_logs/confirm-shift`（可只给段内 `record`，或 `meter`+`start_hour`+`end_hour`，并附 `range_token`/`version`）确认换档后，整段置 `verified=true`、结论 `shift_confirmed`，于是进入后续 Fourier 拟合。
- 晚到抄数走 `/battery_logs/backfill`（与 `log-charge` 同体），会实时重建连续游程，可能补齐或延长待审段并改变 `range_token`。
- 确认所依据的范围已被补录改变时，返回 409 `code=range_changed`、文案含“待审范围已变…重新审核”，响应体带最新 `pending_range`/`range_token`；用新令牌重审后即可确认。电表版本行 + 内容令牌 + 行锁保证补录与确认并发时不丢范围。

按模拟广播班记推演。卡片供人工值守查阅，应用只处理纸本条件的数字记录。

[MIT](LICENSE)
