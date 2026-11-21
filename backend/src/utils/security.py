import hashlib,hmac
from datetime import datetime,timedelta,timezone
from fastapi import Depends,HTTPException
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from jose import jwt,JWTError
from .db import one
from ..config.settings import JWT_SECRET
bearer=HTTPBearer(auto_error=False)
def hp(p): return hashlib.sha256(p.encode()).hexdigest()
def token(u): return jwt.encode({'sub':str(u['id']),'role':u['role'],'name':u['display_name'],'exp':datetime.now(timezone.utc)+timedelta(hours=12)},JWT_SECRET,algorithm='HS256')
def user(creds:HTTPAuthorizationCredentials=Depends(bearer)):
    if not creds: raise HTTPException(401,'需要登录')
    try: uid=int(jwt.decode(creds.credentials,JWT_SECRET,algorithms=['HS256'])['sub'])
    except (JWTError,KeyError,ValueError): raise HTTPException(401,'登录已失效')
    u=one('SELECT id,username,display_name,role FROM users WHERE id=%s',(uid,))
    if not u: raise HTTPException(401,'用户不存在')
    return dict(u)
def roles(*allowed):
    def dep(u=Depends(user)):
        if u['role'] not in allowed: raise HTTPException(403,f'角色 {u["role"]} 无权执行此动作')
        return u
    return dep
