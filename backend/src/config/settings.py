import os
DATABASE_URL=os.getenv('DATABASE_URL','postgresql://bamboo:bamboo_dev_password@db:5432/bamboo')
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
JWT_SECRET=os.getenv('JWT_SECRET','gb1075-demo-secret')
MIN_SAMPLE_POINTS=4
