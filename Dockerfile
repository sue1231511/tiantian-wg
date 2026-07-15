FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# 第一层：系统依赖（只在这行变了才重建，平时永远命中缓存）
RUN apt-get update && apt-get install -y \
    build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 第二层：Python 依赖（只有 requirements.txt 变了才重建）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 第三层：复制代码（每次部署只重建这一层，几秒钟）
COPY . .
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
