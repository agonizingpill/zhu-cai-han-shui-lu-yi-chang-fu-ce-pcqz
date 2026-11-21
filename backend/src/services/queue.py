import os,redis,json
client=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
QUEUE='bamboo:moisture-analysis'
def put(job_id): client.rpush(QUEUE,json.dumps({'job_id':job_id}))
