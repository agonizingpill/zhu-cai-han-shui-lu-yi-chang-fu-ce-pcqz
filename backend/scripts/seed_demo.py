import os,sys,time
sys.path.insert(0,'/app')
from src.utils.db import conn
from src.utils.security import hp
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
    for code,vs in vals.items():
     cur.execute('SELECT id FROM bamboo_lots WHERE lot_code=%s',(code,)); lid=cur.fetchone()[0]
     for i,v in enumerate(vs,1): cur.execute("INSERT INTO moisture_samples(lot_id,point_code,moisture_pct,instrument_id) VALUES(%s,%s,%s,'MT-07') ON CONFLICT(lot_id,point_code) DO NOTHING",(lid,f'P-{i:02d}',v))
     cur.execute("UPDATE bamboo_lots SET status='SAMPLED' WHERE id=%s",(lid,))
    for code,reason in [('BZ-604-20261120','seed异常批次，等待窑干复测'),('BZ-606-20261120','待模型确认的高湿批次')]:
     cur.execute('SELECT id FROM bamboo_lots WHERE lot_code=%s',(code,)); lid=cur.fetchone()[0]
     cur.execute("INSERT INTO anomaly_lots(lot_id,status,reason) VALUES(%s,'OPEN',%s) ON CONFLICT(lot_id) DO NOTHING",(lid,reason)); cur.execute("UPDATE bamboo_lots SET status='QUARANTINED' WHERE id=%s",(lid,))
  print('seed_demo: idempotent seed complete'); break
 except Exception:
  if attempt==29: raise
  time.sleep(1)
