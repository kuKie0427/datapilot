FROM python:3.11-slim

WORKDIR /app

# sentence-transformers 的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ git curl && \
    rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用源码
COPY . .

# 暴露 API 端口
EXPOSE 8000

# 启动命令
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
