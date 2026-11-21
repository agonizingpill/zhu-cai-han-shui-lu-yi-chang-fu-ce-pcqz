import time
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import HTTPException
class RateLimitMiddleware(BaseHTTPMiddleware):
 def __init__(self,app): super().__init__(app); self.h=defaultdict(list)
 async def dispatch(self,request,call_next):
  k=request.client.host if request.client else 'x'; now=time.time(); self.h[k]=[t for t in self.h[k] if now-t<60]
  if len(self.h[k])>=120: raise HTTPException(429,'请求过于频繁')
  self.h[k].append(now); return await call_next(request)
