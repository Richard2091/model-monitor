FROM python:3.12-slim
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py config.py database.py monitor.py http_server.py ./
COPY templates ./templates
EXPOSE 8090
CMD ["python", "app.py"]
