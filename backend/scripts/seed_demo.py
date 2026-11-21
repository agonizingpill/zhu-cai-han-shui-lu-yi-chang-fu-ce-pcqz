import os,sys,time,json
sys.path.insert(0,'/app')
import numpy as np
from src.utils.db import conn
from src.utils.security import hp
from src.services.moisture_ml import vector
from psycopg2.extras import Json
if os.getenv('SEED_DEMO','true').lower()!='true': raise SystemExit(0)
schema=open('/app/src/models/schema.sql').read()
for attempt in range(30):
 try:
  with conn() as c:
   with c.cursor() as cur:
    cur.execute(schema)
    for u,n,r in [('operator','林竹','operator'),('supervisor','周窑','supervisor'),('viewer','沈库','viewer')]: cur.execute('INSERT INTO users(username,display_name,role,password_hash) VALUES(%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING',(u,n,r,hp('Bamboo#1075')))
    lots=[('BZ-601-20261118','浙江安吉','2026-11-18',860,14),('BZ-602-20261119','浙江安吉','2026-11-19',910,14),('BZ-603-20261119','福建建瓯','2026-11-19',780,15),('BZ-604-20261120','江西奉新','2026-11-20',1020,14),('BZ-605-20261120','湖南桃江','2026-11-20',640,15),('BZ-606-20261120','浙江龙泉','2026-11-20',720,14)]
    for code,origin,day,kg,target in lots: cur.execute("INSERT INTO bamboo_lots(lot_code,origin,harvest_date,species,weight_kg,target_moisture,status,operator_id) VALUES(%s,%s,%s,'毛竹',%s,%s,'RECEIVED',1) ON CONFLICT(lot_code) DO NOTHING",(code,origin,day,kg,target))
    vals={'BZ-601-20261118':[10.8,11.2,10.9,11.0,11.1,10.7],'BZ-602-20261119':[11.3,11.0,11.2,11.1,10.9,11.2],'BZ-603-20261119':[12.2,12.0,12.4,12.1,12.3,12.0],'BZ-604-20261120':[21.0,22.8,23.5,24.1,22.2,23.0],'BZ-605-20261120':[20.2,21.1],'BZ-606-20261120':[19.4,20.1,21.5,20.7,19.9,20.6]}
    lot_points={}
    for code,vs in vals.items():
     cur.execute('SELECT id FROM bamboo_lots WHERE lot_code=%s',(code,)); lid=cur.fetchone()[0]
     points=[]
     for i,v in enumerate(vs,1):
      pc=f'P-{i:02d}'; points.append(pc)
      cur.execute("INSERT INTO moisture_samples(lot_id,point_code,moisture_pct,instrument_id) VALUES(%s,%s,%s,'MT-07') ON CONFLICT(lot_id,point_code) DO NOTHING",(lid,pc,v))
     cur.execute("UPDATE bamboo_lots SET status='SAMPLED' WHERE id=%s",(lid,)); lot_points[lid]=points
    # 以无异常且测点齐全的批次重建触发隔离时的检测基准
    cur.execute("""SELECT l.id,array_agg(s.moisture_pct ORDER BY s.point_code) vals FROM bamboo_lots l
     JOIN moisture_samples s ON s.lot_id=l.id LEFT JOIN anomaly_lots a ON a.lot_id=l.id
     WHERE l.lot_code IN ('BZ-601-20261118','BZ-602-20261119','BZ-603-20261119')
     GROUP BY l.id HAVING count(s.id)>=4""")
    refs=[vector([float(v) for v in r['vals']]) for r in cur.fetchall()]
    baseline=Json({'references':refs,'reference_count':len(refs),'threshold_distance':2.25,'model':'OneClassSVM(kernel=rbf,gamma=scale,nu=0.2)','seed':1075})
    for code,reason in [('BZ-604-20261120','seed异常批次，等待窑干复测'),('BZ-606-20261120','待模型确认的高湿批次')]:
     cur.execute('SELECT id FROM bamboo_lots WHERE lot_code=%s',(code,)); lid=cur.fetchone()[0]
     cur.execute("INSERT INTO anomaly_lots(lot_id,status,reason,detection_baseline,baseline_points,baseline_captured_at) VALUES(%s,'OPEN',%s,%s,%s,now()) ON CONFLICT(lot_id) DO UPDATE SET detection_baseline=COALESCE(anomaly_lots.detection_baseline,EXCLUDED.detection_baseline),baseline_points=COALESCE(anomaly_lots.baseline_points,EXCLUDED.baseline_points),baseline_captured_at=COALESCE(anomaly_lots.baseline_captured_at,now())",(lid,reason,baseline,Json(lot_points[lid])))
     cur.execute('SELECT id FROM anomaly_lots WHERE lot_id=%s',(lid,)); aid=cur.fetchone()[0]
     cur.execute("UPDATE bamboo_lots SET status='QUARANTINED' WHERE id=%s",(lid,))
     # BZ-606：原窑次重排后留下一条晚到的归档判定，新窑次仍在排程；晚到记录不能出库
     if code=='BZ-606-20261120':
      cur.execute("SELECT 1 FROM kiln_rechecks WHERE anomaly_id=%s",(aid,))
      if not cur.fetchone():
       cur.execute("INSERT INTO kiln_rechecks(anomaly_id,kiln_code,scheduled_start,scheduled_end,status,is_current,notes,created_by) VALUES(%s,'K-01','2026-11-20T18:00+08','2026-11-20T20:00+08','RESCHEDULED',FALSE,'窑位冲突，保留原排程记录并创建重排版本',1)",(aid,))
       cur.execute("SELECT id FROM kiln_rechecks WHERE anomaly_id=%s ORDER BY id LIMIT 1",(aid,)); old_id=cur.fetchone()[0]
       old_readings=[18.8,19.6,20.4,19.9,19.2,20.1]
       for pc,v in zip(lot_points[lid],old_readings): cur.execute("INSERT INTO recheck_readings(recheck_id,point_code,moisture_pct,instrument_id,recorded_by) VALUES(%s,%s,%s,'MT-07',1) ON CONFLICT DO NOTHING",(old_id,pc,v))
       old_judgement={'passed':False,'model_normal':False,'prediction':-1,'decision_score':-0.18,'standardized_distance':2.61,'model_checksum':'seed-old-round','threshold_distance':2.25,'target_moisture':14.0,'over_target':dict(zip(lot_points[lid],old_readings)),'reason':'原窑次晚到记录：模型仍判异常且测点超目标含水率，仅归档','baseline_reused':True,'reference_count':len(refs)}
       cur.execute("UPDATE kiln_rechecks SET status='COMPLETED',result='FAIL',post_moisture=%s,judgement=%s,judgement_at=now(),notes='换窑后晚到的复测记录，只归原窑次，不能拿来出库' WHERE id=%s",(round(float(np.mean(old_readings)),2),Json(old_judgement),old_id))
       cur.execute("INSERT INTO kiln_rechecks(anomaly_id,kiln_code,scheduled_start,scheduled_end,status,is_current,previous_recheck_id,notes,created_by) VALUES(%s,'K-03','2026-11-21T13:00+08','2026-11-21T15:00+08','SCHEDULED',TRUE,%s,'窑位冲突，保留原排程记录并创建重排版本',1)",(aid,old_id))
       cur.execute("UPDATE anomaly_lots SET status='SCHEDULED',resolved_at=NULL WHERE id=%s",(aid,))
     else:
      cur.execute("SELECT 1 FROM kiln_rechecks WHERE anomaly_id=%s",(aid,))
      if not cur.fetchone():
       cur.execute("INSERT INTO kiln_rechecks(anomaly_id,kiln_code,scheduled_start,scheduled_end,status,is_current,notes,created_by) VALUES(%s,'K-02','2026-11-21T13:00+08','2026-11-21T15:00+08','SCHEDULED',TRUE,'首次排程',1)",(aid,))
       cur.execute("UPDATE anomaly_lots SET status='SCHEDULED',resolved_at=NULL WHERE id=%s",(aid,))
  print('seed_demo: idempotent seed complete'); break
 except Exception:
  if attempt==29: raise
  time.sleep(1)
