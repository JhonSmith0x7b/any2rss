FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium
COPY any2rss ./any2rss
ENV ANY2RSS_DB=/data/any2rss.sqlite3 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "any2rss.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
