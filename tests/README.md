# 本地端到端测试

当前环境没有 Docker / PostgreSQL，测试用内存 SQLite + 最小方言兼容层
（`harness.py`）驱动真实的 FastAPI 路由与业务 SQL，验证复测闭环规则。
生产代码仍只面向 PostgreSQL，未为测试改动业务 SQL。

## 运行

```bash
/workspace/.venv/bin/python tests/test_rules.py   # 7 条业务规则，29 条断言
/workspace/.venv/bin/python tests/test_seed.py    # seed 幂等性与晚到归档演示数据
```

## 覆盖的规则

1. 换窑后晚到的复测记录只归原窑次（`is_current=FALSE`），不能拿来出库
2. 复测按入库测点补齐读数；缺点位 / 找不到原基准 -> 窑次 FAILED、继续隔离
3. 沿用触发隔离时的检测基准重算（baseline 中的 references / 阈值）
4. 模型判正常且各点不超目标含水率才算合格（两个条件 AND）
5. 同一窑次重试不重复出判定（COMPLETED / FAILED 后拒绝）
6. 重排与判定并发时行锁串行化；主管放行只认当前窑次的 COMPLETED/PASS
7. `GET /anomalies/{id}/rechecks` 可查各窑次测点与结构化判定
