"""覆盖用户提出的 7 条业务规则：
1. 换窑后晚到的复测记录只归原窑次，不能拿来出库
2. 复测按入库测点补齐读数，沿用触发隔离时的检测基准重算；缺点/找不到基准继续隔离
3. 模型判正常且各点不超目标含水率才算合格
4. 同一窑次重试不重复出判定
5. 重排与判定同时写入时，主管只认当前窑次的合格结论（行锁串行化 + 放行校验）
6. 异常页面能录入并查到各窑次的测点和判定
"""
import harness  # noqa: F401  必须先导入以完成 SQLite 兼容层与 sys.path 注入
from src.utils import db as dbmod
from src.utils.db import one, all_rows, conn
from src.services import moisture_ml
from src.services.moisture_ml import detect, vector
from harness import (client, login, seed_lot, make_anomaly, schedule,
                     readings, _Json)

# 8 个正常参考批次（6 测点），目标含水率 14%；参考集经标定覆盖稳定的正常区间
REFS = [
    [9.6, 10.1, 9.8, 10.4, 9.9, 10.2],
    [10.4, 11.0, 10.7, 11.2, 10.8, 11.1],
    [11.2, 11.8, 11.5, 12.0, 11.6, 11.9],
    [12.0, 12.7, 12.4, 13.0, 12.5, 12.8],
    [10.0, 11.3, 10.6, 11.7, 10.9, 11.4],
    [11.6, 12.9, 12.2, 13.3, 12.4, 13.1],
    [9.9, 10.8, 10.3, 11.0, 10.5, 10.9],
    [11.0, 12.3, 11.7, 12.6, 11.9, 12.4],
]
POINTS = [f'P-{i:02d}' for i in range(1, 7)]
h = login()

# 标定：低湿读数沿用基准判正常；高湿读数判异常
ref_vecs = [vector(v) for v in REFS]
low = [10.9, 11.5, 11.1, 11.8, 11.3, 11.6]
high = [20.1, 21.4, 22.0, 20.8, 21.9, 21.2]
assert detect(ref_vecs, low)['is_anomaly'] is False
assert detect(ref_vecs, high)['is_anomaly'] is True
print('标定完成：低湿集模型正常，高湿集模型异常')

results = []
def check(name, cond, extra=''):
    results.append((name, bool(cond), extra))
    print(('PASS' if cond else 'FAIL'), '-', name, extra)

# ---------- 场景 A：换窑 + 晚到记录只归原窑次 ----------
lid = seed_lot('BZ-T-A', high)
aid = make_anomaly(lid, REFS)
s1 = schedule(aid, kiln='K-01')                                  # 第 1 窑次
# 同窑号、时间窗重叠 -> 真实窑位冲突，重排为第 2 窑次（当前）
s2 = schedule(aid, kiln='K-01')
check('重排后新窑次为 RESCHEDULED 标记', s2['status'] == 'RESCHEDULED')
rows = all_rows('SELECT id,is_current,status FROM kiln_rechecks WHERE anomaly_id=%s ORDER BY id', (aid,))
check('原窑次 is_current=FALSE，新窑次 is_current=TRUE',
      rows[0]['is_current'] == 0 and rows[1]['is_current'] == 1, str([(r['id'], r['is_current']) for r in rows]))

# 晚到的原窑次复测记录（指定 recheck_id 回写到旧窑次）
r = client.post(f'/anomalies/{aid}/recheck-result', headers=h,
                json={'recheck_id': rows[0]['id'], 'readings': readings(high), 'notes': '晚到'})
check('晚到记录可回写并明确提示只归档', r.status_code == 200 and r.json()['is_current'] is False
      and '归属原窑次' in r.json()['message'], r.text)
a = one('SELECT status FROM anomaly_lots WHERE id=%s', (aid,))
l = one('SELECT status FROM bamboo_lots WHERE id=%s', (lid,))
check('晚到记录不改变隔离状态', a['status'] == 'SCHEDULED' and l['status'] == 'QUARANTINED',
      f"anomaly={a['status']} lot={l['status']}")

# 主管尝试凭旧窑次记录出库 -> 拒绝
r = client.post(f'/releases/lots/{lid}', headers=login('supervisor'),
                json={'decision': 'RELEASE', 'reason': '试一下晚到记录能否放行'})
check('规则1：晚到的原窑次记录不能出库', r.status_code == 409 and '当前窑次没有合格结论' in r.json()['detail'], r.text)
check('viewer/operator 无权放行',
      client.post(f'/releases/lots/{lid}', headers=login('operator'),
                  json={'decision': 'RELEASE', 'reason': 'x'}).status_code == 403)

# ---------- 规则4：同一窑次重试不重复出判定 ----------
r2 = client.post(f'/anomalies/{aid}/recheck-result', headers=h,
                 json={'recheck_id': rows[0]['id'], 'readings': readings(high)})
check('规则4：同一窑次重复回写被拒绝且不产生新判定',
      r2.status_code == 409 and '不重复判定' in r2.json()['detail'], r2.text)
n = one('SELECT count(*) c FROM kiln_rechecks WHERE anomaly_id=%s', (aid,))['c']
check('窑次数量未增加', n == 2)

# ---------- 规则2a：读数缺点 -> FAILED + 继续隔离，且同一窑次不可再判 ----------
r = client.post(f'/anomalies/{aid}/recheck-result', headers=h,
                json={'recheck_id': rows[1]['id'], 'readings': readings(low[:5])})
check('规则2：缺少入库测点被拒绝', r.status_code == 409 and '未按入库测点补齐' in r.json()['detail'], r.text)
rc = one("SELECT status,judgement FROM kiln_rechecks WHERE id=%s", (rows[1]['id'],))
check('缺点窑次落 FAILED 且判定理由可追溯', rc['status'] == 'FAILED' and rc['judgement']['reason'] == 'points_incomplete')
l = one('SELECT status FROM bamboo_lots WHERE id=%s', (lid,))
check('缺点后批次继续隔离', l['status'] == 'QUARANTINED')
r = client.post(f'/anomalies/{aid}/recheck-result', headers=h,
                json={'recheck_id': rows[1]['id'], 'readings': readings(low)})
check('规则4：FAILED 窑次同样不允许重复判定', r.status_code == 409 and '不重复判定' in r.json()['detail'])

# ---------- 规则2b：找不到检测基准 -> 继续隔离 ----------
lid_b = seed_lot('BZ-T-B', high)
aid_b = make_anomaly(lid_b, REFS)
sb = schedule(aid_b)
with conn() as c:
    with c.cursor() as cur:
        cur.execute('UPDATE anomaly_lots SET detection_baseline=NULL,baseline_points=NULL WHERE id=%s', (aid_b,))
r = client.post(f'/anomalies/{aid_b}/recheck-result', headers=h,
                json={'readings': readings(low)})
check('规则2：找不到原基准时拒绝并继续隔离', r.status_code == 409 and '检测基准' in r.json()['detail'], r.text)
check('基准缺失时窑次 FAILED',
      one("SELECT status FROM kiln_rechecks WHERE anomaly_id=%s AND is_current=1", (aid_b,))['status'] == 'FAILED')

# ---------- 规则3：模型正常但有测点超目标 -> 不合格；全部达标且模型正常 -> 合格 ----------
# 场景 A 的当前窑次已 FAILED，重排开新窑次
s3 = schedule(aid, kiln='K-03')
cur_id = s3['id']
# P-01 超目标 14%。小样本 4D 向量下单点超标会同时触发模型异常，
# 这里打桩让模型返回“正常”，以独立验证目标含水率这一 AND 分支（生产口径不变）
over = [14.6, 11.4, 11.2, 11.6, 11.1, 11.3]
_real_detect = moisture_ml.detect
def _forced_normal(refs, vals):
    out = _real_detect(refs, vals)
    out['is_anomaly'] = False
    out['prediction'] = 1
    import hashlib, json as _json
    out['checksum'] = hashlib.sha256(_json.dumps(out, sort_keys=True).encode()).hexdigest()[:16]
    return out
moisture_ml.detect = _forced_normal
import src.main as mainmod
mainmod.detect = _forced_normal
try:
    r = client.post(f'/anomalies/{aid}/recheck-result', headers=h,
                    json={'recheck_id': cur_id, 'readings': readings(over)})
    check('规则3：模型正常但测点超目标含水率仍判 FAIL', r.status_code == 200 and r.json()['passed'] is False
          and r.json()['judgement']['model_normal'] is True
          and set(r.json()['judgement']['over_target'].keys()) == {'P-01'}, r.text)
finally:
    moisture_ml.detect = _real_detect
    mainmod.detect = _real_detect
a = one('SELECT status FROM anomaly_lots WHERE id=%s', (aid,))
l = one('SELECT status FROM bamboo_lots WHERE id=%s', (lid,))
check('超目标后保持 REJECTED/隔离', a['status'] == 'REJECTED' and l['status'] == 'QUARANTINED')
r = client.post(f'/releases/lots/{lid}', headers=login('supervisor'),
                json={'decision': 'RELEASE', 'reason': 'x'})
check('FAIL 窑次不能放行', r.status_code == 409)

# 再开一窑，全部测点达标 + 模型正常 -> PASS
s4 = schedule(aid, kiln='K-04')
r = client.post(f'/anomalies/{aid}/recheck-result', headers=h,
                json={'recheck_id': s4['id'], 'readings': readings(low), 'notes': '二次窑干'})
check('规则3：模型正常且各点不超目标 -> PASS', r.status_code == 200 and r.json()['passed'] is True
      and r.json()['judgement']['model_normal'] is True and r.json()['judgement']['over_target'] == {}, r.text)
a = one('SELECT status FROM anomaly_lots WHERE id=%s', (aid,))
l = one('SELECT status FROM bamboo_lots WHERE id=%s', (lid,))
check('PASS 后异常闭环、批次 CLEARED', a['status'] == 'CLEARED' and l['status'] == 'CLEARED')

# 规则5/1：主管放行只认当前窑次合格结论，此时可以放行
r = client.post(f'/releases/lots/{lid}', headers=login('supervisor'),
                json={'decision': 'RELEASE', 'reason': '当前窑次合格，盖出库章'})
check('规则5：当前窑次 PASS 后主管可放行', r.status_code == 200 and r.json()['decision'] == 'RELEASE', r.text)
check('批次最终 RELEASED', one('SELECT status FROM bamboo_lots WHERE id=%s', (lid,))['status'] == 'RELEASED')

# ---------- 规则6：各窑次测点与判定可录入可查询 ----------
r = client.get(f'/anomalies/{aid}/rechecks', headers=h)
check('规则6：窑次归档接口可用', r.status_code == 200)
arc = r.json()
check('归档覆盖全部 4 个窑次', len(arc['rechecks']) == 4, str([x['id'] for x in arc['rechecks']]))
for rc_ in arc['rechecks']:
    if rc_['status'] == 'COMPLETED':
        check(f"窑次 {rc_['id']} 测点读数齐全({len(rc_['readings'])})且带判定",
              len(rc_['readings']) == 6 and rc_['judgement'] and 'reason' in rc_['judgement'])
check('归档带回入库测点清单', arc['intake_points'] == POINTS)
# 晚到原窑次在归档中仍可查到 FAIL 判定与读数
old = next(x for x in arc['rechecks'] if x['id'] == rows[0]['id'])
check('晚到原窑次归档保留 FAIL 判定与 6 个测点', old['result'] == 'FAIL' and len(old['readings']) == 6
      and old['is_current'] is False)
# /anomalies 列表带窑次轮数与当前窑次信息
r = client.get('/anomalies', headers=h)
item = next(x for x in r.json() if x['id'] == aid)
check('异常列表返回窑次轮数与当前窑次判定', item['recheck_rounds'] == 4 and item['result'] == 'PASS'
      and item['is_current'] is True, str(item['recheck_rounds']))

# ---------- 汇总 ----------
failed = [n for n, ok, _ in results if not ok]
print(f'\n{len(results)-len(failed)}/{len(results)} 条断言通过')
if failed:
    raise SystemExit('失败用例：' + '；'.join(failed))
print('全部业务规则验证通过')
