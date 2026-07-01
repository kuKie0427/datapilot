#!/usr/bin/env bash
# DataPilot —— 快速启动脚本
set -euo pipefail

echo "=== DataPilot Startup ==="

# 1. 检查 Python 版本
PYTHON="${PYTHON:-python3}"
echo "Using: $($PYTHON --version)"

# 2. Create venv if not exists
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
fi

# 3. 安装依赖（优先使用 uv sync 基于 uv.lock 锁定版本，回退到 pip）
if command -v uv &> /dev/null; then
    echo "Installing dependencies with uv sync..."
    uv sync
else
    echo "uv not found, falling back to pip install..."
    ./"$VENV_DIR"/bin/pip install -q -r requirements.txt
fi

# 4. 检查 API key
if [ -z "${LLM_API_KEY:-${DEEPSEEK_API_KEY:-}}" ]; then
    echo "⚠️  Warning: LLM_API_KEY not set. Set it with:"
    echo "   export LLM_API_KEY=\"sk-...\""
fi

# 5. 构建 RAG 索引（可选，首次运行）
if [ ! -f "rag_index.pkl" ]; then
    echo "Building RAG index (first run, may take a few minutes)..."
    ./"$VENV_DIR"/bin/python -c "from src.rag_store import RAGStore; RAGStore().build(max_examples=7000)" || true
fi

# 6. 启动服务
PORT="${PORT:-8000}"
echo ""
echo "=== Starting DataPilot on http://localhost:$PORT ==="
echo "   API docs:  http://localhost:$PORT/docs"
echo "   Frontend:  open DataPilot-Frontend-v1.html in browser"
echo ""
./"$VENV_DIR"/bin/uvicorn src.api.app:app --host 0.0.0.0 --port "$PORT" --reload
