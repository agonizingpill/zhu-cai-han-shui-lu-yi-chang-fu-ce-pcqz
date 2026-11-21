import json, os
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from .utils.db import conn, one, all_rows
from .utils.security import hp, token, user, roles
from .config.settings import MIN_SAMPLE_POINTS
from .services.queue import put
from .middlewares.audit import AuditMiddleware
from .middlewares.rate_limit import RateLimitMiddleware

app=FastAPI(title='Bamboo Moisture Recheck',version='1.0.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
app.add_middleware(RateLimitMiddleware); app.add_middleware(AuditMiddleware)
SCHEMA=(Path(__file__).parent/'models/schema.sql').read_text()

class Login(BaseModel): username:str; password:str
class LotIn(BaseModel): lot_code:str; origin:str; harvest_date:str; species:str='毛竹'; weight_kg:float=Field(gt=0); target_moisture:float=Field(gt=0,lt=100)
class SampleIn(BaseModel): point_code:str; moisture_pct:float=Field(ge=0,le=100); instrument_id:str
class ScheduleIn(BaseModel): kiln_code:str; scheduled_start:str; scheduled_end:str
class ResultIn(BaseModel): post_moisture:float=Field(ge=0,le=100); result:str; notes:str=''
class ReleaseIn(BaseModel): decision:str; reason:str

def detail(lot_id):
    l=one('SELECT * FROM bamboo_lots WHERE id=%s',(lot_id,))
    if not l: raise HTTPException(404,'竹材批次不存在')
    ss=all_rows('SELECT * FROM moisture_samples WHERE lot_id=%s ORDER BY point_code',(lot_id,)); a=one('SELECT * FROM anomaly_lots WHERE lot_id=%s',(lot_id,))
    return {'lot':dict(l),'samples':[dict(x) for x in ss],'anomaly':dict(a) if a else None}

def reference_vectors(exclude):
    rows=all_rows("SELECT l.id,array_agg(s.moisture_pct ORDER BY s.point_code) vals FROM bamboo_lots l JOIN moisture_samples s ON s.lot_id=l.id LEFT JOIN anomaly_lots a ON a.lot_id=l.id WHERE l.id<>%s AND a.id IS NULL GROUP BY l.id HAVING count(s.id)>=%s",(exclude,MIN_SAMPLE_POINTS))
    import numpy as np
    return [[float(np.mean(v)), float(np.std(v)), float(min(v)), float(max(v))] for v in [[float(x) for x in r['vals']] for r in rows]]

@app.on_event('startup')
def startup():
    with conn() as c:
        with c.cursor() as cur: cur.execute(SCHEMA)

@app.get('/health')
def health():
    with conn() as c:
        with c.cursor() as cur: cur.execute('SELECT 1'); cur.fetchone()
    return {'status':'ok','service':'backend'}

@app.post('/auth/login')
def login(d:Login):
    u=one('SELECT * FROM users WHERE username=%s',(d.username,))
    if not u or u['password_hash']!=hp(d.password): raise HTTPException(401,'账号或密码错误')
    return {'access_token':token(u),'token_type':'bearer','user':{'username':u['username'],'display_name':u['display_name'],'role':u['role']}}

@app.get('/dashboard')
def dashboard(u=Depends(roles('operator','supervisor','viewer'))):
    return {'lots':one('SELECT count(*) n FROM bamboo_lots')['n'],'sampled':one("SELECT count(*) n FROM bamboo_lots WHERE status<>'RECEIVED'")['n'],'open_anomalies':one("SELECT count(*) n FROM anomaly_lots WHERE status IN ('OPEN','SCHEDULED','RECHECKING')")['n'],'pending_release':one("SELECT count(*) n FROM bamboo_lots WHERE status='CLEARED'")['n']}

@app.get('/lots')
def lots(u=Depends(roles('operator','supervisor','viewer'))):
    return [dict(x) for x in all_rows("SELECT l.*,count(s.id) sample_count,a.status anomaly_status,a.standardized_distance FROM bamboo_lots l LEFT JOIN moisture_samples s ON s.lot_id=l.id LEFT JOIN anomaly_lots a ON a.lot_id=l.id GROUP BY l.id,a.status,a.standardized_distance ORDER BY l.received_at DESC")]

@app.post('/lots')
def create_lot(d:LotIn,u=Depends(roles('operator','supervisor'))):
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute('INSERT INTO bamboo_lots(lot_code,origin,harvest_date,species,weight_kg,target_moisture,operator_id) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',(d.lot_code,d.origin,d.harvest_date,d.species,d.weight_kg,d.target_moisture,u['id']))
                lid=cur.fetchone()[0]
        return detail(lid)
    except Exception as e:
        if 'duplicate key' in str(e).lower(): raise HTTPException(409,'批次号已存在，请使用已有批次')
        raise

@app.get('/lots/{lot_id}')
def get_lot(lot_id:int,u=Depends(roles('operator','supervisor','viewer'))): return detail(lot_id)

@app.post('/lots/{lot_id}/samples')
def sample(lot_id:int,d:SampleIn,u=Depends(roles('operator','supervisor'))):
    l=one('SELECT status FROM bamboo_lots WHERE id=%s',(lot_id,))
    if not l: raise HTTPException(404,'批次不存在')
    if l['status']=='RELEASED': raise HTTPException(409,'已放行批次不能补录采样')
    try:
        with conn() as c:
            with c.cursor() as cur:
                cur.execute('INSERT INTO moisture_samples(lot_id,point_code,moisture_pct,instrument_id) VALUES(%s,%s,%s,%s)',(lot_id,d.point_code,d.moisture_pct,d.instrument_id))
                cur.execute("UPDATE bamboo_lots SET status='SAMPLED' WHERE id=%s AND status='RECEIVED'",(lot_id,))
    except Exception as e:
        if 'duplicate key' in str(e).lower(): raise HTTPException(409,'同一测点已采样')
        raise
    return detail(lot_id)

@app.post('/lots/{lot_id}/analyze')
def analyze(lot_id:int,u=Depends(roles('operator','supervisor'))):
    d=detail(lot_id); n=len(d['samples'])
    if n<MIN_SAMPLE_POINTS: raise HTTPException(409,f'采样点不足，至少需要 {MIN_SAMPLE_POINTS} 个测点，当前 {n} 个')
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('INSERT INTO analysis_jobs(lot_id,input_summary) VALUES(%s,%s) RETURNING id',(lot_id,json.dumps({'sample_count':n,'points':[s['point_code'] for s in d['samples']]})))
            jid=cur.fetchone()[0]
    put(jid); return {'job_id':jid,'status':'QUEUED'}

@app.get('/anomalies')
def anomalies(u=Depends(roles('operator','supervisor','viewer'))):
    rows=all_rows("SELECT a.*,l.lot_code,l.origin,l.target_moisture,l.weight_kg,k.id recheck_id,k.kiln_code,k.scheduled_start,k.scheduled_end,k.status recheck_status,k.post_moisture,k.result FROM anomaly_lots a JOIN bamboo_lots l ON l.id=a.lot_id LEFT JOIN LATERAL(SELECT * FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) k ON true ORDER BY a.detected_at DESC")
    return [dict(x) for x in rows]

@app.post('/anomalies/{aid}/schedule')
def schedule(aid:int,d:ScheduleIn,u=Depends(roles('operator','supervisor'))):
    a=one('SELECT * FROM anomaly_lots WHERE id=%s',(aid,))
    if not a: raise HTTPException(404,'异常批次不存在')
    conflict=one("SELECT id,anomaly_id,kiln_code,scheduled_start,scheduled_end FROM kiln_rechecks WHERE kiln_code=%s AND status IN('SCHEDULED','RESCHEDULED','IN_PROGRESS') AND scheduled_start<%s::timestamptz AND scheduled_end>%s::timestamptz",(d.kiln_code,d.scheduled_end,d.scheduled_start))
    status='RESCHEDULED' if conflict else 'SCHEDULED'
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('SELECT id FROM kiln_rechecks WHERE anomaly_id=%s ORDER BY id DESC LIMIT 1',(aid,)); prev=cur.fetchone()
            cur.execute('INSERT INTO kiln_rechecks(anomaly_id,kiln_code,scheduled_start,scheduled_end,status,previous_recheck_id,created_by,notes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',(aid,d.kiln_code,d.scheduled_start,d.scheduled_end,status,prev[0] if prev else None,u['id'],'窑位冲突，保留原排程记录并创建重排版本' if conflict else '首次排程'))
            rid=cur.fetchone()[0]; cur.execute("UPDATE anomaly_lots SET status='SCHEDULED' WHERE id=%s",(aid,))
    return {'id':rid,'status':status,'conflict_with':dict(conflict) if conflict else None}

@app.post('/anomalies/{aid}/recheck-result')
def recheck_result(aid:int,d:ResultIn,u=Depends(roles('operator','supervisor'))):
    a=one('SELECT * FROM anomaly_lots WHERE id=%s',(aid,)); r=one("SELECT * FROM kiln_rechecks WHERE anomaly_id=%s AND status IN('SCHEDULED','RESCHEDULED','IN_PROGRESS') ORDER BY id DESC LIMIT 1",(aid,))
    if not a: raise HTTPException(404,'异常批次不存在')
    if not r: raise HTTPException(409,'没有可完成的窑干复测排程')
    passed=d.result=='PASS'; status='CLEARED' if passed else 'REJECTED'
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE kiln_rechecks SET status='COMPLETED',post_moisture=%s,result=%s,notes=%s WHERE id=%s",(d.post_moisture,d.result,d.notes,r['id']))
            cur.execute("UPDATE anomaly_lots SET status=%s,resolved_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s",(status,passed,aid))
            cur.execute("UPDATE bamboo_lots SET status=%s WHERE id=%s",('CLEARED' if passed else 'QUARANTINED',a['lot_id']))
    return {'status':status,'recheck_id':r['id']}

@app.get('/releases')
def releases(u=Depends(roles('operator','supervisor','viewer'))):
    return [dict(x) for x in all_rows("SELECT r.*,l.lot_code,l.origin,a.status anomaly_status FROM release_decisions r JOIN bamboo_lots l ON l.id=r.lot_id LEFT JOIN anomaly_lots a ON a.id=r.anomaly_id ORDER BY r.decided_at DESC")]

@app.post('/releases/lots/{lot_id}')
def release(lot_id:int,d:ReleaseIn,u=Depends(roles('supervisor'))):
    l=one('SELECT * FROM bamboo_lots WHERE id=%s',(lot_id,)); a=one('SELECT * FROM anomaly_lots WHERE lot_id=%s',(lot_id,))
    if not l: raise HTTPException(404,'批次不存在')
    if a and a['status'] not in ('CLEARED','REJECTED'): raise HTTPException(409,'异常批次尚未完成窑干复测，不能出库')
    if d.decision=='RELEASE' and a and a['status']=='REJECTED': raise HTTPException(409,'复测未通过，不能放行')
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('INSERT INTO release_decisions(lot_id,anomaly_id,decision,reason,decided_by) VALUES(%s,%s,%s,%s,%s)',(lot_id,a['id'] if a else None,d.decision,d.reason,u['id']))
            cur.execute("UPDATE bamboo_lots SET status=%s WHERE id=%s",('RELEASED' if d.decision=='RELEASE' else 'QUARANTINED',lot_id))
    return {'lot_id':lot_id,'decision':d.decision}
