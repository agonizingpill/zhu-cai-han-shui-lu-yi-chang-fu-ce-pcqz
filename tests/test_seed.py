"""验证 seed_demo.py 在新 schema 下可幂等执行，并落出基准/多窑次/晚到归档数据。"""
import os
from pathlib import Path

import harness  # noqa: F401  注入 SQLite 兼容层
from src.utils.db import one, all_rows

BACKEND = Path('/workspace/backend')
code = (BACKEND / 'scripts/seed_demo.py').read_text().replace('/app', str(BACKEND))
os.environ['SEED_DEMO'] = 'true'

# 执行两次验证幂等
for round_ in (1, 2):
    exec(compile(code, 'seed_demo.py', 'exec'), {'__name__': '__main__'})

lots = all_rows('SELECT lot_code,status FROM bamboo_lots ORDER BY lot_code')
print('lots:', [(r['lot_code'], r['status']) for r in lots])
assert len(lots) == 6

# BZ-606：基准已落、存在 2 个窑次，旧窑次晚到 FAIL 归档(is_current=0)，新窑次当前 SCHEDULED
a = one("""SELECT a.id,a.baseline_points,a.detection_baseline,a.status FROM anomaly_lots a
           JOIN bamboo_lots l ON l.id=a.lot_id WHERE l.lot_code='BZ-606-20261120'""")
assert a['baseline_points'] == ['P-01', 'P-02', 'P-03', 'P-04', 'P-05', 'P-06']
assert a['detection_baseline']['reference_count'] >= 3
assert a['status'] == 'SCHEDULED'
ks = all_rows('SELECT id,kiln_code,status,is_current,result FROM kiln_rechecks WHERE anomaly_id=%s ORDER BY id', (a['id'],))
print('BZ-606 kiln rounds:', ks)
assert len(ks) == 2
old, cur = ks
assert old['is_current'] is False and old['status'] == 'COMPLETED' and old['result'] == 'FAIL'
assert cur['is_current'] is True and cur['status'] == 'SCHEDULED'
rd = all_rows('SELECT count(*) c FROM recheck_readings WHERE recheck_id=%s', (old['id'],))[0]['c']
assert rd == 6, rd

# BZ-604：单窑次 SCHEDULED + 当前
b = one("""SELECT a.id FROM anomaly_lots a JOIN bamboo_lots l ON l.id=a.lot_id
           WHERE l.lot_code='BZ-604-20261120'""")
kb = all_rows('SELECT status,is_current FROM kiln_rechecks WHERE anomaly_id=%s', (b['id'],))
assert len(kb) == 1 and kb[0]['is_current'] is True and kb[0]['status'] == 'SCHEDULED'
print('BZ-604 kiln rounds:', kb)

print('seed 幂等性与晚到归档场景验证通过')
