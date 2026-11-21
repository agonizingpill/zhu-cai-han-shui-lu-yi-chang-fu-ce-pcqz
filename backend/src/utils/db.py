import os,time,psycopg2
from contextlib import contextmanager
from psycopg2.extras import RealDictCursor
URL=os.getenv('DATABASE_URL','postgresql://bamboo:bamboo_dev_password@db:5432/bamboo')
@contextmanager
def conn():
    last=None
    for _ in range(30):
        try: c=psycopg2.connect(URL); break
        except Exception as e: last=e; time.sleep(1)
    else: raise last
    try: yield c; c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()
def one(sql,params=()):
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur: cur.execute(sql,params); return cur.fetchone()
def all_rows(sql,params=()):
    with conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur: cur.execute(sql,params); return cur.fetchall()
