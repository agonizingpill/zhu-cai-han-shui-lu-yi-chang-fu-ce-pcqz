# 竹材含水率异常复测

面向安吉青篁竹材集散中心质量组的入库、含水率异常隔离、窑干复测与出库放行工作台。系统使用多点采样建立批次湿度画像，通过 `StandardScaler + OneClassSVM` 识别偏离正常基线的批次，并把异常批次带入可追溯的窑位排程和主管放行流程。

## 真实场景

模拟客户日均 30–50 批毛竹入库。仓管在纸质批次卡写产地和采伐日，检验员使用 MT-07 手持仪在 Excel 中登记 4–6 个测点，窑炉班组通过微信群协调窑位。痛点是测点缺失后仍可能被手工判为正常，异常批次隔离和窑位重排缺少统一证据，主管盖章时难以回看复测后的最后读数。

上游输入为入库过磅、产地/采伐日和 MT-07 测点记录。本系统替代手工湿度判断、异常隔离和窑位沟通。下游为窑炉班组和出库主管，消费窑位排程、复测结论和出库放行记录。

## 功能模块

- 竹材入库 `BambooLot`：批次号、产地、采伐日、树种、毛重与目标含水率。
- 含水率采样 `MoistureSample`：每个点位只能录入一次，少于 4 点时禁止建模。
- 异常批次 `AnomalyLot`：持久化 OneClassSVM 判定、标准化距离、模型 checksum、**触发隔离时的检测基准（参考向量、阈值、模型参数）与入库测点清单**，以及隔离状态。
- 窑干复测 `KilnRecheck`：记录窑位时间窗，冲突时创建 `RESCHEDULED` 版本并保留原排程；每个窑次用 `is_current` 标记是否为当前窑次。
- 复测读数 `RecheckReading`：复测必须按触发隔离时的入库测点逐点补齐，读数挂在具体窑次下可逐轮追溯。
- 出库放行 `ReleaseDecision`：只有 supervisor 能盖章；主管只认**当前窑次**的合格结论，晚到的原窑次记录不能出库。

## 复测与放行口径

- **晚到记录只归原窑次**：换窑重排后，回写到旧窑次的复测记录只归档（`is_current=FALSE`），不改变当前隔离/放行状态，也不能作为出库依据；放行接口强制要求存在当前窑次的 `COMPLETED/PASS`。
- **按入库测点补齐**：复测读数的点位集合必须与触发隔离时的入库测点完全一致，缺点或多出测点时该窑次落 `FAILED`，批次继续隔离。
- **沿用原检测基准重算**：复测不重新拟合模型，直接复用隔离时持久化的参考向量与阈值重算 OneClassSVM；找不到原基准或基准失效时窑次 `FAILED`、继续隔离。
- **合格条件（AND）**：模型重判正常（`prediction=1` 且标准化距离 < 2.25）**且**每个测点含水率不超过批次目标含水率，任一不满足即 `FAIL` 并保持隔离。
- **同一窑次不重复判定**：窑次进入 `COMPLETED/FAILED` 后再次回写直接拒绝，需要重试必须新建窑次。
- **并发安全**：排程、复测判定、放行均在单事务内对异常单/批次/窑次加行锁（`SELECT ... FOR UPDATE`），重排与判定同时写入时按提交顺序串行化，主管最终只认当前窑次结论。
- **可追溯**：`GET /anomalies/{id}/rechecks` 返回每个窑次的排程、各测点读数与结构化判定；异常页面可直接录入并查询各窑次数据。

## 算法口径

worker 从 Redis 队列读取分析任务，将每个批次的测点压缩为 `[均值, 标准差, 最小值, 最大值]`，用无异常单的已采样批次拟合 `StandardScaler`，再训练固定参数的 `OneClassSVM(kernel=rbf, gamma=scale, nu=0.2)`。当前批次标准化距离为 `||x_scaled||₂`，`prediction=-1` 或距离不小于 `2.25` 时进入异常隔离。固定种子为 `1075`。结果写入 `analysis_jobs` 和 `anomaly_lots`。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
```

访问 `http://localhost:18274`。容器内 seed 会自动幂等执行，也可运行 `docker compose exec backend python scripts/seed_demo.py` 复核。

## 演示账号

| 账号 | 密码 | 角色 |
|---|---|---|
| operator | Bamboo#1075 | 录入、采样、送模型、排程、回写复测 |
| supervisor | Bamboo#1075 | 包含 operator 权限，可主管放行 |
| viewer | Bamboo#1075 | 查看批次、异常、放行记录 |

## 枚举位置

后端枚举在 `backend/src/constants/enums.py`，前端枚举在 `frontend/src/constants/enums.js`。具体状态前置条件在 `backend/src/main.py` 的入库、采样、分析、排程、复测和放行路由中。

## 技术栈与目录

| 层 | 技术 |
|---|---|
| frontend | Nginx + 原生 HTML/CSS/ES modules |
| backend | Python 3.11 + FastAPI + psycopg2 |
| worker | Python 3.11 + Redis BRPOP + scikit-learn |
| db | PostgreSQL 16 |
| queue | Redis 7 |

领域代码位于 `backend/src/main.py`、`backend/src/services/moisture_ml.py`、`backend/src/models/schema.sql` 和 `frontend/src/app.js`。seed 位于 `backend/scripts/seed_demo.py`。

## License

Internal demonstration project, MIT-style use for evaluation.
