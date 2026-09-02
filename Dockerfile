FROM python:3.12-slim
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
 && groupadd -r -g 10001 appuser && useradd -r -u 10001 -g appuser appuser \
 && mkdir -p /data \
 && chown appuser:appuser /data
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py config.py database.py config_manager.py monitor.py http_server.py security.py ./
COPY templates ./templates
USER appuser
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "-c", "import os, urllib.request; p=os.environ.get('PORT'); path=os.environ.get('ENV_FILE', '/app/.env');\ntry:\n    if not p:\n        with open(path, encoding='utf-8') as f:\n            for line in f:\n                if line.strip().startswith('PORT='):\n                    p=line.strip().split('=', 1)[1]; break\nexcept OSError:\n    pass\np=int(p or 8090); r=urllib.request.urlopen(f'http://localhost:{p}/', timeout=5); raise SystemExit(0 if r.status == 200 else 1)"]
CMD ["python", "app.py"]
