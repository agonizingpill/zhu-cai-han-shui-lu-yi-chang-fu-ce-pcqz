import os,json,time,hashlib,traceback,psycopg2,redis,numpy as np
from datetime import datetime,timezone
from psycopg2.extras import RealDictCursor,Json
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM
DB=os.getenv('DATABASE_URL','postgresql://bamboo:bamboo_dev_password@db:5432/bamboo'); R=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
Q='bamboo:moisture-analysis'
def db():
 for _ in range(30):
  try:return psycopg2.connect(DB)
  except Exception:time.sleep(1)
 raise RuntimeError('db unavailable')
def vec(v):
 a=np.array(v,float); return [float(a.mean()),float(a.std()),float(a.min()),float(a.max())]
def detect(refs,vals):
 scaler=StandardScaler(); rs=scaler.fit_transform(np.array(refs,float)); x=scaler.transform(np.array(vec(vals)).reshape(1,-1)); m=OneClassSVM(kernel='rbf',gamma='scale',nu=.2);m.fit(rs);pred=int(m.predict(x)[0]);score=float(m.decision_function(x)[0]);dist=float(np.linalg.norm(x[0]));out={'seed':1075,'reference_count':len(refs),'vector':vec(vals),'prediction':pred,'decision_score':round(score,6),'standardized_distance':round(dist,6),'is_anomaly':pred==-1 or dist>=2.25};out['checksum']=hashlib.sha256(json.dumps(out,sort_keys=True).encode()).hexdigest()[:16];return out
def heartbeat():open('/shared/worker.heartbeat','w').write(datetime.now(timezone.utc).isoformat())
def process(jid):
 c=db(); c.autocommit=True; cur=c.cursor(cursor_factory=RealDictCursor); cur.execute('SELECT * FROM analysis_jobs WHERE id=%s',(jid,)); job=cur.fetchone()
 if not job or job['status']=='SUCCEEDED':c.close();return
 cur.execute("UPDATE analysis_jobs SET status='RUNNING' WHERE id=%s",(jid,));cur.execute('SELECT moisture_pct FROM moisture_samples WHERE lot_id=%s ORDER BY point_code',(job['lot_id'],));vals=[float(x['moisture_pct']) for x in cur.fetchall()];cur.execute("SELECT l.id,array_agg(s.moisture_pct ORDER BY s.point_code) vals FROM bamboo_lots l JOIN moisture_samples s ON s.lot_id=l.id LEFT JOIN anomaly_lots a ON a.lot_id=l.id WHERE l.id<>%s AND a.id IS NULL GROUP BY l.id HAVING count(s.id)>=4",(job['lot_id'],));refs=[vec([float(v) for v in x['vals']]) for x in cur.fetchall()];cur.close();c.close()
 try:
  out=detect(refs,vals); c=db();c.autocommit=True;cur=c.cursor();cur.execute("UPDATE analysis_jobs SET status='SUCCEEDED',result=%s,finished_at=now() WHERE id=%s",(Json(out),jid));cur.execute('SELECT id FROM anomaly_lots WHERE lot_id=%s',(job['lot_id'],));a=cur.fetchone()
  if out['is_anomaly']:
   if a:cur.execute("UPDATE anomaly_lots SET status='OPEN',reason='OneClassSVM识别为高湿异常，需窑干复测',decision_score=%s,standardized_distance=%s,model_checksum=%s WHERE id=%s",(out['decision_score'],out['standardized_distance'],out['checksum'],a[0]))
   else:cur.execute("INSERT INTO anomaly_lots(lot_id,status,reason,decision_score,standardized_distance,model_checksum) VALUES(%s,'OPEN','OneClassSVM识别为高湿异常，需窑干复测',%s,%s,%s)",(job['lot_id'],out['decision_score'],out['standardized_distance'],out['checksum']))
   cur.execute("UPDATE bamboo_lots SET status='QUARANTINED' WHERE id=%s",(job['lot_id'],))
  else:cur.execute("UPDATE bamboo_lots SET status='CLEARED' WHERE id=%s",(job['lot_id'],))
  c.close()
 except Exception as e:
  c=db();c.autocommit=True;c.cursor().execute("UPDATE analysis_jobs SET status='FAILED',error=%s,finished_at=now() WHERE id=%s",(str(e),jid));c.close();traceback.print_exc()
heartbeat()
while True:
 heartbeat(); item=R.blpop(Q,timeout=5)
 if item:
  try:process(json.loads(item[1])['job_id'])
  except Exception:traceback.print_exc()
