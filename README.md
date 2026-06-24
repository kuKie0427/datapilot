# DataPilot

面向自助查数场景的智能数据分析 Agent。用户用自然语言提问，系统自动完成"理解意图 → 检索相似案例 → 感知表结构 → 生成 SQL → 执行验证"全链路，最终返回查询结果。

## 架构

```
question
  |
  +-- 1. Schema Indexing (tables.json with FK relationships)
  +-- 2. RAG Retrieval (sentence-embedding over 7k examples, top-3)
  |
  +-- 3. LLM Generation (DeepSeek v4 Flash with thinking mode)
  |     |
  |     +-- success -> return SQL
  |     +-- failure -> multi-candidate sampling + execution voting
  |
  +-- 4. SQL output
```

## 效果

在 Spider 100 条分层测试集上（25 simple / 40 moderate / 35 challenging，覆盖 16 个数据库）：

| 指标 | 整体 | Simple | Moderate | Challenging |
|------|------|--------|----------|-------------|
| Execution Accuracy | 78% | 84% | 82% | 66% |

## 快速开始

```bash
# 1. 设置 API Key
export DEEPSEEK_API_KEY="sk-..."

# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行评测（Exact Match 模式）
python -m src.evaluate

# 4. 运行评测（Execution Accuracy 模式，需要下载 Spider 数据库）
bash datasets/download_databases.sh
python -m src.evaluate --execution
```

## 项目结构

```
datapilot/
+-- src/
|   +-- generator.py       # SQL 生成引擎（RAG + Schema + 自校正）
|   +-- rag_store.py        # 向量检索索引
|   +-- evaluate.py         # 评估运行器
|   +-- __init__.py
+-- datasets/
|   +-- spider_eval_100.json  # Spider 100 条分层测试集
|   +-- download_databases.sh # Spider 数据库下载脚本
+-- examples/
|   +-- basic_usage.py
+-- requirements.txt
+-- .gitignore
+-- README.md
```
