import hashlib,json,numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM
SEED=1075
def vector(vals):
    a=np.array(vals,dtype=float); return [float(a.mean()),float(a.std()),float(a.min()),float(a.max())]
def detect(refs,vals):
    if len(refs)<3: raise ValueError('参考批次不足，至少需要 3 个正常批次')
    scaler=StandardScaler(); rs=scaler.fit_transform(np.array(refs,float)); x=scaler.transform(np.array(vector(vals)).reshape(1,-1))
    model=OneClassSVM(kernel='rbf',gamma='scale',nu=.2); model.fit(rs)
    pred=int(model.predict(x)[0]); score=float(model.decision_function(x)[0]); dist=float(np.linalg.norm(x[0])); abnormal=pred==-1 or dist>=2.25
    out={'seed':SEED,'reference_count':len(refs),'vector':vector(vals),'prediction':pred,'decision_score':round(score,6),'standardized_distance':round(dist,6),'is_anomaly':abnormal}
    out['checksum']=hashlib.sha256(json.dumps(out,sort_keys=True).encode()).hexdigest()[:16]; return out
