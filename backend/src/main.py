import json
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from psycopg2.extras import Json
from .utils.db import conn, one, all_rows
from .utils.security import hp, token, roles
from .config.settings import MIN_SAMPLE_POINTS
from .services.queue import put
from .services.moisture_ml import detect
from .middlewares.audit import AuditMiddleware
from .middlewares.rate_limit import RateLimitMiddleware

app=FastAPI(title='Bamboo Moisture Recheck',version='1.1.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])
app.add_middleware(RateLimitMiddleware); app.add_middleware(AuditMiddleware)
SCHEMA=(Path(__file__).parent/'models/schema.sql').read_text()
RECHECK_TERMINAL=('COMPLETED','FAILED')

class Login(BaseModel): username:str; password:str
class LotIn(BaseModel): lot_code:str; origin:str; harvest_date:str; species:str='毛竹'; weight_kg:float=Field(gt=0); target_moisture:float=Field(gt=0,lt=100)
class SampleIn(BaseModel): point_code:str; moisture_pct:float=Field(ge=0,le=100); instrument_id:str
class ScheduleIn(BaseModel): kiln_code:str; scheduled_start:str; scheduled_end:str
class RecheckReadingIn(BaseModel): point_code:str; moisture_pct:float=Field(ge=0,le=100); instrument_id:str='MT-07'
class RecheckResultIn(BaseModel): recheck_id:int|None=None; readings:list[RecheckReadingIn]; notes:str=''
class ReleaseIn(BaseModel): decision:str; reason:str

def detail(lot_id):
    l=one('SELECT * FROM bamboo_lots WHERE id=%s',(lot_id,))
    if not l: raise HTTPException(404,'竹材批次不存在')
    ss=all_rows('SELECT * FROM moisture_samples WHERE lot_id=%s ORDER BY point_code',(lot_id,)); a=one('SELECT * FROM anomaly_lots WHERE lot_id=%s',(lot_id,))
    return {'lot':dict(l),'samples':[dict(x) for x in ss],'anomaly':dict(a) if a else None}

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
    rows=all_rows("""SELECT a.*,l.lot_code,l.origin,l.target_moisture,l.weight_kg,
        (SELECT z.id FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) recheck_id,
        (SELECT z.kiln_code FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) kiln_code,
        (SELECT z.scheduled_start FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) scheduled_start,
        (SELECT z.scheduled_end FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) scheduled_end,
        (SELECT z.status FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) recheck_status,
        (SELECT z.post_moisture FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) post_moisture,
        (SELECT z.result FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) result,
        (SELECT z.is_current FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) is_current,
        (SELECT z.judgement FROM kiln_rechecks z WHERE z.anomaly_id=a.id ORDER BY z.id DESC LIMIT 1) judgement,
        (SELECT count(*) FROM kiln_rechecks z WHERE z.anomaly_id=a.id) recheck_rounds
        FROM anomaly_lots a JOIN bamboo_lots l ON l.id=a.lot_id
        ORDER BY a.detected_at DESC""")
    return [dict(x) for x in rows]

@app.get('/anomalies/{aid}/rechecks')
def anomaly_rechecks(aid:int,u=Depends(roles('operator','supervisor','viewer'))):
    a=one('SELECT a.*,l.lot_code,l.target_moisture FROM anomaly_lots a JOIN bamboo_lots l ON l.id=a.lot_id WHERE a.id=%s',(aid,))
    if not a: raise HTTPException(404,'异常批次不存在')
    rechecks=all_rows('SELECT * FROM kiln_rechecks WHERE anomaly_id=%s ORDER BY id',(aid,))
    out=[]
    for r in rechecks:
        rd=all_rows('SELECT point_code,moisture_pct,instrument_id,recorded_at FROM recheck_readings WHERE recheck_id=%s ORDER BY point_code',(r['id'],))
        item=dict(r); item['readings']=[dict(x) for x in rd]; out.append(item)
    return {'anomaly':dict(a),'intake_points':a['baseline_points'] or [],'rechecks':out}

@app.post('/anomalies/{aid}/schedule')
def schedule(aid:int,d:ScheduleIn,u=Depends(roles('operator','supervisor'))):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('SELECT * FROM anomaly_lots WHERE id=%s FOR UPDATE',(aid,)); a=cur.fetchone()
            if not a: raise HTTPException(404,'异常批次不存在')
            cur.execute("SELECT id,anomaly_id,kiln_code,scheduled_start,scheduled_end FROM kiln_rechecks WHERE kiln_code=%s AND status IN('SCHEDULED','RESCHEDULED','IN_PROGRESS') AND scheduled_start<%s::timestamptz AND scheduled_end>%s::timestamptz",(d.kiln_code,d.scheduled_end,d.scheduled_start))
            conflict=cur.fetchone()
            status='RESCHEDULED' if conflict else 'SCHEDULED'
            cur.execute('SELECT id FROM kiln_rechecks WHERE anomaly_id=%s ORDER BY id DESC LIMIT 1',(aid,)); prev=cur.fetchone()
            cur.execute('UPDATE kiln_rechecks SET is_current=FALSE WHERE anomaly_id=%s AND is_current=TRUE',(aid,))
            cur.execute('INSERT INTO kiln_rechecks(anomaly_id,kiln_code,scheduled_start,scheduled_end,status,is_current,previous_recheck_id,created_by,notes) VALUES(%s,%s,%s,%s,%s,TRUE,%s,%s,%s) RETURNING id',(aid,d.kiln_code,d.scheduled_start,d.scheduled_end,status,prev[0] if prev else None,u['id'],'窑位冲突，保留原排程记录并创建重排版本' if conflict else '首次排程'))
            rid=cur.fetchone()[0]
            cur.execute("UPDATE anomaly_lots SET status='SCHEDULED',resolved_at=NULL WHERE id=%s",(aid,))
    return {'id':rid,'status':status,'conflict_with':dict(conflict) if conflict else None}

@app.post('/anomalies/{aid}/recheck-result')
def recheck_result(aid:int,d:RecheckResultIn,u=Depends(roles('operator','supervisor'))):
    # 同一窑次的判定一次性出结论；整段事务加行锁，重排与判定并发时按提交顺序串行化
    pending_error=None
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('SELECT * FROM anomaly_lots WHERE id=%s FOR UPDATE',(aid,)); a=cur.fetchone()
            if not a: raise HTTPException(404,'异常批次不存在')
            cur.execute('SELECT * FROM bamboo_lots WHERE id=%s FOR UPDATE',(a['lot_id'],)); lot=cur.fetchone()
            if d.recheck_id is not None:
                cur.execute('SELECT * FROM kiln_rechecks WHERE id=%s AND anomaly_id=%s FOR UPDATE',(d.recheck_id,aid)); r=cur.fetchone()
                if not r: raise HTTPException(404,'窑次排程不存在或不属于该异常批次')
            else:
                cur.execute("SELECT * FROM kiln_rechecks WHERE anomaly_id=%s AND is_current=TRUE ORDER BY id DESC LIMIT 1 FOR UPDATE",(aid,)); r=cur.fetchone()
                if not r: raise HTTPException(409,'该异常批次没有可回写的窑次排程')
            # 同一窑次重试不重复出判定（连接退出即提交，报错在事务外抛出，保留已落库状态）
            if r['status'] in RECHECK_TERMINAL:
                pending_error=HTTPException(409,f"窑次 {r['kiln_code']} 已出判定（{r['status']}），同一窑次不重复判定；需要重试请新建窑次")
            else:
                # 复测按入库测点补齐读数：点位集合必须与触发隔离时的入库测点完全一致
                intake_points=a['baseline_points']
                if not intake_points or not a['detection_baseline']:
                    cur.execute("UPDATE kiln_rechecks SET status='FAILED',notes=%s,judgement=%s,judgement_at=now() WHERE id=%s",('找不到触发隔离时的检测基准，无法重算，继续隔离',Json({'passed':False,'reason':'baseline_missing'}),r['id']))
                    pending_error=HTTPException(409,'找不到触发隔离时的检测基准，无法重算，批次继续隔离')
                else:
                    incoming={x.point_code:float(x.moisture_pct) for x in d.readings}
                    missing=[p for p in intake_points if p not in incoming]
                    extra=[p for p in incoming if p not in intake_points]
                    if missing or extra:
                        reason='复测读数未按入库测点补齐：缺少 %s%s' % ('、'.join(missing) if missing else '无', (f'；多余测点 {"、".join(extra)}' if extra else ''))
                        cur.execute("UPDATE kiln_rechecks SET status='FAILED',notes=%s,judgement=%s,judgement_at=now() WHERE id=%s",(reason,Json({'passed':False,'reason':'points_incomplete','missing':missing,'extra':extra}),r['id']))
                        pending_error=HTTPException(409,reason+'；批次继续隔离')
                    else:
                        # 沿用触发隔离时的检测基准重算
                        baseline=a['detection_baseline']; refs=baseline['references']
                        vals=[incoming[p] for p in intake_points]
                        try:
                            out=detect(refs,vals)
                        except ValueError as e:
                            cur.execute("UPDATE kiln_rechecks SET status='FAILED',notes=%s,judgement=%s,judgement_at=now() WHERE id=%s",(f'检测基准失效：{e}，继续隔离',Json({'passed':False,'reason':'baseline_invalid'}),r['id']))
                            pending_error=HTTPException(409,f'检测基准失效：{e}，批次继续隔离')
            if pending_error is None:
                model_normal=not out['is_anomaly']
                target=float(lot['target_moisture'])
                over_target={p:v for p,v in incoming.items() if v>target}
                # 模型判正常且各点不超目标含水率才算合格
                passed=model_normal and not over_target
                for x in d.readings:
                    cur.execute('INSERT INTO recheck_readings(recheck_id,point_code,moisture_pct,instrument_id,recorded_by) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(recheck_id,point_code) DO UPDATE SET moisture_pct=EXCLUDED.moisture_pct,instrument_id=EXCLUDED.instrument_id,recorded_by=EXCLUDED.recorded_by,recorded_at=now()',(r['id'],x.point_code,x.moisture_pct,x.instrument_id,u['id']))
                post_moisture=round(sum(vals)/len(vals),2)
                judgement={'passed':passed,'model_normal':model_normal,'prediction':out['prediction'],'decision_score':out['decision_score'],'standardized_distance':out['standardized_distance'],'model_checksum':out['checksum'],'threshold_distance':2.25,'target_moisture':target,'over_target':over_target,'reason':('模型判正常且全部测点不超目标含水率' if passed else ('模型仍判异常' if not model_normal else f'测点超出目标含水率 {target}%：'+'、'.join(sorted(over_target)))),'baseline_reused':True,'reference_count':out['reference_count']}
                result='PASS' if passed else 'FAIL'
                cur.execute("UPDATE kiln_rechecks SET status='COMPLETED',post_moisture=%s,result=%s,notes=%s,judgement=%s,judgement_at=now() WHERE id=%s",(post_moisture,result,(d.notes or '')+'｜'+judgement['reason'],Json(judgement),r['id']))
                is_current=r['is_current']
                if is_current:
                    # 只有当前窑次的结论能驱动隔离/放行状态
                    if passed:
                        cur.execute("UPDATE anomaly_lots SET status='CLEARED',resolved_at=now() WHERE id=%s",(aid,))
                        cur.execute("UPDATE bamboo_lots SET status='CLEARED' WHERE id=%s",(a['lot_id'],))
                    else:
                        cur.execute("UPDATE anomaly_lots SET status='REJECTED',resolved_at=NULL WHERE id=%s",(aid,))
                        cur.execute("UPDATE bamboo_lots SET status='QUARANTINED' WHERE id=%s",(a['lot_id'],))
    # 事务已提交后再抛业务错误，保证 FAILED/已判定状态可追溯
    if pending_error is not None: raise pending_error
    return {'recheck_id':r['id'],'is_current':is_current,'result':result,'passed':passed,'judgement':judgement,'message':('当前窑次判定完成' if is_current else '该记录归属原窑次，只归档不改变当前隔离/放行状态，不能据此出库')}

@app.get('/releases')
def releases(u=Depends(roles('operator','supervisor','viewer'))):
    return [dict(x) for x in all_rows("SELECT r.*,l.lot_code,l.origin,a.status anomaly_status FROM release_decisions r JOIN bamboo_lots l ON l.id=r.lot_id LEFT JOIN anomaly_lots a ON a.id=r.anomaly_id ORDER BY r.decided_at DESC")]

@app.post('/releases/lots/{lot_id}')
def release(lot_id:int,d:ReleaseIn,u=Depends(roles('supervisor'))):
    with conn() as c:
        with c.cursor() as cur:
            cur.execute('SELECT * FROM bamboo_lots WHERE id=%s FOR UPDATE',(lot_id,)); l=cur.fetchone()
            if not l: raise HTTPException(404,'批次不存在')
            cur.execute('SELECT * FROM anomaly_lots WHERE lot_id=%s FOR UPDATE',(lot_id,)); a=cur.fetchone()
            if a:
                # 主管只认当前窑次的合格结论：必须存在 is_current 的 COMPLETED/PASS 窑次
                cur.execute("SELECT id FROM kiln_rechecks WHERE anomaly_id=%s AND is_current=TRUE AND status='COMPLETED' AND result='PASS' LIMIT 1",(a['id'],)); current_pass=cur.fetchone()
                if not current_pass: raise HTTPException(409,'当前窑次没有合格结论（晚到的原窑次记录不可用于出库），不能放行')
            cur.execute('INSERT INTO release_decisions(lot_id,anomaly_id,decision,reason,decided_by) VALUES(%s,%s,%s,%s,%s)',(lot_id,a['id'] if a else None,d.decision,d.reason,u['id']))
            cur.execute("UPDATE bamboo_lots SET status=%s WHERE id=%s",('RELEASED' if d.decision=='RELEASE' else 'QUARANTINED',lot_id))
    return {'lot_id':lot_id,'decision':d.decision}
