"""本地无 Postgres 环境下的端到端逻辑测试。

生产代码面向 PostgreSQL；这里用内存 SQLite + 轻量兼容层验证业务规则，
SQL 方言差异在 _PG_TO_SQLITE 中做最小替换，业务 SQL 本身不在测试里改写。
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

import sqlite3

from src.utils import db as dbmod
from src.services import queue as queue_mod
from src.services import moisture_ml
from src.utils.security import hp
from psycopg2.extras import Json as _Json

_PG_TO_SQLITE = [
    (r'::timestamptz', ''),
    (r'\bFOR UPDATE\b', ''),
    (r'\bnow\(\)', "(datetime('now'))"),
    (r'\bJSONB\b', 'TEXT'),
    (r'\bTIMESTAMPTZ\b', 'TEXT'),
    (r'\bSERIAL\b', 'INTEGER'),
    (r'\bNUMERIC\([^)]*\)', 'REAL'),
    (r' ON DELETE CASCADE', ''),
    (r"array_agg\((\w+(?:\.\w+)?)(?:\s+ORDER BY\s+[\w.]+)?\)", r"json_group_array(\1)"),
    (r'ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) (.+?);',
     r"ALTER TABLE \1 ADD COLUMN \2 \3;"),
]


def _conv(sql: str) -> str:
    out = sql
    for pat, rep in _PG_TO_SQLITE:
        out = re.sub(pat, rep, out, flags=re.S)
    return out.replace('%s', '?')


_MEM = sqlite3.connect(':memory:', check_same_thread=False)
_MEM.row_factory = sqlite3.Row


class DictRow(dict):
    """模拟 psycopg2 RealDictRow：既支持列名也支持位置索引。"""
    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._cols = cols

    def __getitem__(self, key):
        if isinstance(key, int):
            return super().__getitem__(self._cols[key])
        return super().__getitem__(key)


def _hydrate(row):
    for col in list(row.keys()):
        v = row[col]
        if isinstance(v, str) and v[:1] in ('{', '['):
            try:
                row[col] = json.loads(v)
            except Exception:
                pass
        if col in ('is_current',) and v is not None:
            row[col] = bool(v)
    return row


class FakeCursor:
    def __init__(self, conn):
        self.c = conn
        self.description = None

    def execute(self, sql, params=()):
        cur = self.c.cursor()
        converted = _conv(sql)
        if not params and ';' in converted.strip().rstrip(';'):
            for stmt in [s.strip() for s in converted.split(';') if s.strip()]:
                try:
                    cur.execute(stmt)
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e):
                        raise
        else:
            cur.execute(converted, tuple(params))
        self.description = cur.description
        self._cur = cur
        return cur

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return _hydrate(DictRow(cols, tuple(row)))

    def fetchall(self):
        rows = self._cur.fetchall()
        cols = [d[0] for d in self._cur.description]
        return [_hydrate(DictRow(cols, tuple(r))) for r in rows]

    def close(self):
        cur = getattr(self, '_cur', None)
        if cur is not None:
            cur.close()
            self._cur = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeConn:
    """模拟 psycopg2 连接：with 块用 SAVEPOINT 复刻子事务提交/回滚语义。"""
    _depth = 0

    def __init__(self, real):
        self._real = real
        self._sp = None

    def cursor(self, cursor_factory=None):
        return FakeCursor(self._real)

    def commit(self):
        self._real.commit()

    def rollback(self):
        self._real.rollback()

    def close(self):
        # 单连接内存库，连接生命周期由测试进程持有
        pass

    def __enter__(self):
        FakeConn._depth += 1
        self._sp = f'sp{FakeConn._depth}'
        self._real.execute(f'SAVEPOINT {self._sp}')
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._real.execute(f'RELEASE SAVEPOINT {self._sp}')
        else:
            self._real.execute(f'ROLLBACK TO SAVEPOINT {self._sp}')
        return False


def _fake_connect(*a, **k):
    return FakeConn(_MEM)


import psycopg2  # noqa: E402
psycopg2.connect = _fake_connect
queue_mod.put = lambda *a, **k: None


def _adapt_json(j):
    return json.dumps(j.adapted, ensure_ascii=False, default=str)
sqlite3.register_adapter(_Json, _adapt_json)

from fastapi.testclient import TestClient  # noqa: E402
from src.main import app, SCHEMA  # noqa: E402

# 建表（执行方言转换后的 schema；ALTER ADD COLUMN 对已存在列会报错，逐句容错）
for stmt in [s.strip() for s in _conv(SCHEMA).split(';') if s.strip()]:
    try:
        _MEM.execute(stmt)
    except sqlite3.OperationalError as e:
        if 'duplicate column' not in str(e):
            raise

client = TestClient(app)


def login(username='supervisor'):
    with dbmod.conn() as c:
        with c.cursor() as cur:
            cur.execute("INSERT OR IGNORE INTO users(username,display_name,role,password_hash) VALUES(%s,%s,%s,%s)",
                        (username, username, username, hp('Bamboo#1075')))
    r = client.post('/auth/login', json={'username': username, 'password': 'Bamboo#1075'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def seed_lot(code, readings, target=14.0, origin='浙江安吉'):
    h = login()
    r = client.post('/lots', headers=h, json={
        'lot_code': code, 'origin': origin, 'harvest_date': '2026-11-21',
        'species': '毛竹', 'weight_kg': 800, 'target_moisture': target})
    assert r.status_code == 200, r.text
    lid = r.json()['lot']['id']
    for i, v in enumerate(readings, 1):
        r = client.post(f'/lots/{lid}/samples', headers=h,
                        json={'point_code': f'P-{i:02d}', 'moisture_pct': v, 'instrument_id': 'MT-07'})
        assert r.status_code == 200, r.text
    return lid


def make_anomaly(lid, refs, points=None):
    """绕过 worker：按生产口径直接落异常单 + 检测基准。"""
    if points is None:
        points = [f'P-{i:02d}' for i in range(1, 7)]
    ref_vectors = [moisture_ml.vector(v) for v in refs]
    with dbmod.conn() as c:
        with c.cursor() as cur:
            cur.execute("INSERT INTO anomaly_lots(lot_id,status,reason,detection_baseline,baseline_points,baseline_captured_at) VALUES(%s,'OPEN','t',%s,%s,datetime('now'))",
                        (lid, _Json({'references': ref_vectors, 'reference_count': len(ref_vectors),
                                     'threshold_distance': 2.25,
                                     'model': 'OneClassSVM(kernel=rbf,gamma=scale,nu=0.2)', 'seed': 1075}),
                         _Json(points)))
            cur.execute('SELECT id FROM anomaly_lots WHERE lot_id=%s', (lid,))
            aid = cur.fetchone()['id']
            cur.execute("UPDATE bamboo_lots SET status='QUARANTINED' WHERE id=%s", (lid,))
    return aid


def schedule(aid, kiln='K-02', start='2026-11-21T13:00:00+08:00', end='2026-11-21T15:00:00+08:00', h=None):
    h = h or login()
    r = client.post(f'/anomalies/{aid}/schedule', headers=h,
                    json={'kiln_code': kiln, 'scheduled_start': start, 'scheduled_end': end})
    assert r.status_code == 200, r.text
    return r.json()


def readings(values, points=None, instrument='MT-07'):
    if points is None:
        points = [f'P-{i:02d}' for i in range(1, len(values) + 1)]
    return [{'point_code': p, 'moisture_pct': v, 'instrument_id': instrument} for p, v in zip(points, values)]
