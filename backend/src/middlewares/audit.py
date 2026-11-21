from starlette.middleware.base import BaseHTTPMiddleware
from ..utils.db import conn
class AuditMiddleware(BaseHTTPMiddleware):
 async def dispatch(self,request,call_next):
  r=await call_next(request)
  if request.url.path!='/health':
   try:
    with conn() as c:
     with c.cursor() as cur: cur.execute('INSERT INTO audit_logs(action,path,method,status_code) VALUES(%s,%s,%s,%s)',('http',request.url.path,request.method,r.status_code))
   except Exception: pass
  return r
