# RAG 智能助手 - 生产镜像
# 使用 python:3.12-slim, 安装依赖后以 uvicorn 启动
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/rag

# 先复制依赖清单以充分利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app ./app
COPY static ./static
COPY .env.example ./.env.example

# 数据目录(挂载卷, 映射宿主机持久化)
RUN mkdir -p /srv/rag/data

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]