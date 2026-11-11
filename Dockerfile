# 语法: docker compose up --build
# 测试: docker compose --profile test run --rm tests
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY tools ./tools

# 演示邮件夹具构建阶段：生成 6 封覆盖多附件/无 CSV/坏行/重复场景的邮件
FROM base AS mailbox-seed
COPY demo ./demo
RUN mkdir -p /mailbox && python -m tools.make_fixtures --out /mailbox

FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY demo ./demo
CMD ["pytest", "-q"]

FROM base AS runtime
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
