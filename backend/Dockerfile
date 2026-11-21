FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY scripts ./scripts
CMD ["sh","-c","python scripts/seed_demo.py && uvicorn src.main:app --host 0.0.0.0 --port 8000"]
