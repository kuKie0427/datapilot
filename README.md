# DataPilot

面向非技术业务人员的自然语言查数 Agent。用户用中文提问，系统自动完成"意图解析 → RAG 检索 → Schema 感知生成 → 执行校验"全链路，最终返回数据结果或可视化图表。

## 架构

```
用户提问
  │
  ▼
┌─────────────────────────────────────────────┐
│  FastAPI Service Layer                       │
│  POST /api/v1/query  ·  SSE /query/stream   │
└──────────────────┬──────────────────────────┘
                   │
  ┌────────────────▼──────────────────┐
  │  LangGraph Four-Stage Pipeline    │
  │                                   │
  │  1. Intent Parsing (意图解析)      │
  │     ↓                             │
  │  2. RAG Retrieval (向量检索)       │
  │     sentence-embedding · Top-3    │
  │     ↓                             │
  │  3. Schema-Aware Generation        │
  │     schema + few-shot → LLM       │
  │     ↓                             │
  │  4. Execution & Validation         │
  │     ┌─ success → return            │
  │     └─ failure → correction loop   │
  │        (error-driven rewrite +     │
  │         multi-candidate voting)    │
  └──────────────────┬────────────────┘
                     │
  ┌──────────────────▼────────────────┐
  │  Tool Calling + Adapter Pattern    │
  │                                    │
  │  ┌─────────────┐ ┌──────┐ ┌─────────┐ │
  │  │ SQL Adapter │ │ CSV  │ │ API Adp │ │
  │  │SQLite/      │ │Pan-  │ │RESTful  │ │
  │  │MySQL        │ │das   │ │         │ │
  │  └─────────────┘ └──────┘ └─────────┘ │
  │                                    │
  │  ┌────────────┐  ┌──────────────┐  │
  │  │ Chart Tool │  │ Sandbox Exec │  │
  │  │ (ECharts)  │  │ (Docker)     │  │
  │  └────────────┘  └──────────────┘  │
  └───────────────────────────────────┘
```

## 核心能力

| 能力 | 说明 |
|------|------|
| **四阶段查询管线** | LangGraph 状态机：意图 → RAG → Schema 感知生成 → 执行校验 |
| **错误驱动自校正** | 执行失败时裁剪错误栈，驱动 LLM 重写 + 多候选采样投票 |
| **声明式沙箱** | LLM 生成的 Python 代码在 Docker 隔离容器中运行，无网络、限内存/CPU |
| **多数据源适配器** | SQL(SQLite/MySQL) / CSV / API 统一接口，新增数据源仅需实现 2 个方法 |
| **Tool Calling** | 主 Agent 通过 function calling 调用 SQL 执行、图表生成、沙箱分析 |
| **图表自动生成** | 根据意图选择 KPI / 柱状 / 折线 / 饼图 / 表格，输出 ECharts JSON |

## 快速开始

```bash
# 1. 设置 API Key
export LLM_API_KEY="sk-..."

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动服务
./start.sh
# 或手动启动:
uvicorn src.api.app:app --reload --port 8000

# 4. 打开前端
# 在浏览器中打开 DataPilot-Frontend-v1.html

# 5. API 文档
# http://localhost:8000/docs
```

## Docker 部署

```bash
# 启动全部服务（API + MySQL + 沙箱）
docker-compose up -d

# 仅构建 API 镜像
docker build -t datapilot .
docker run -p 8000:8000 -v /var/run/docker.sock:/var/run/docker.sock datapilot
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/login` | 登录获取 JWT |
| POST | `/api/v1/query` | 提交自然语言查询，返回完整结果 |
| POST | `/api/v1/query/stream` | SSE 流式返回查询过程 |
| GET | `/api/v1/sources` | 列出所有数据源 |
| GET | `/api/v1/sources/{id}/schema` | 获取数据源 Schema |
| POST | `/api/v1/sources/register` | 运行时注册新数据源 |
| POST | `/api/v1/sources/csv/upload` | 上传 CSV 文件作为数据源 |
| GET | `/api/v1/health` | 健康检查 |

## 项目结构

```
datapilot/
├── src/
│   ├── api/                    # FastAPI 服务层
│   │   ├── app.py              # 应用工厂
│   │   ├── routes.py           # API 路由
│   │   └── schemas.py          # Pydantic 模型
│   ├── auth/                   # JWT 认证 + RBAC 角色权限模型 + 行级过滤 + 列脱敏
│   ├── agent/                  # LangGraph 四阶段管线
│   │   ├── graph.py            # 状态机构建
│   │   ├── nodes.py            # 节点实现
│   │   └── state.py            # 状态定义
│   ├── glossary/               # 业务术语字典（YAML 配置）
│   ├── semantic/               # 语义层（指标定义 + Plan 编译为 SQL）
│   ├── session/                # 多轮对话会话管理
│   ├── datasources/            # 多数据源适配器
│   │   ├── base.py             # 抽象适配器
│   │   ├── sql_source.py       # SQL (SQLite/MySQL)
│   │   ├── csv_source.py       # CSV
│   │   ├── api_source.py       # REST API
│   │   └── registry.py         # 数据源注册表
│   ├── security/               # SQL 安全校验（白名单 + 强制 LIMIT + 行级过滤注入）
│   ├── sandbox/                # Docker 沙箱引擎
│   │   └── executor.py         # 隔离执行 + 错误栈裁剪
│   ├── tools/                  # Tool Calling
│   │   ├── chart_tool.py       # 图表生成 (ECharts)
│   │   └── registry.py         # 工具注册表
│   ├── generator.py            # 原始 SQL 生成引擎
│   ├── rag_store.py            # 向量检索索引
│   ├── evaluate.py             # 评估运行器
│   └── config.py               # 集中配置
├── datasets/                   # Spider 评测数据集
├── examples/                   # 使用示例
├── uploads/                    # CSV 上传目录
├── DataPilot-Frontend-v1.html  # 前端原型
├── Dockerfile                  # API 服务镜像
├── docker-compose.yml          # 全栈编排
├── start.sh                    # 快速启动脚本
└── requirements.txt
```

## 评测

在 Spider 分层测试集上：

| 指标 | 整体 | Simple | Moderate | Challenging |
|------|------|--------|----------|-------------|
| Execution Accuracy | 83% | 84% | 82% | 66% |

```bash
# Exact Match 模式
python -m src.evaluate

# Execution Accuracy 模式
bash datasets/download_databases.sh
python -m src.evaluate --execution
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEBUG` | `false` | 生产环境设为 false；本地开发可设 true |
| `ALLOW_NO_PASSWORD_LOGIN` | `false` | 仅在 DEBUG=true 时可设 true，完全跳过密码校验（仅本地联调） |
| `JWT_SECRET` | `please-change-this-...` | JWT 签名密钥，生产环境必须为强随机字符串（>=32 字符） |
| `JWT_TTL` | `3600` | JWT 有效期（秒） |
| `CORS_ORIGINS` | （空） | 逗号分隔的前端域名白名单；留空则允许所有源但不携带凭证 |
| `LLM_PROVIDER` | `openai` | LLM 提供方 |
| `LLM_API_KEY` | — | LLM API 密钥 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | LLM API 地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `LLM_TEMPERATURE` | `0.0` | LLM 采样温度 |
| `LLM_MAX_TOKENS` | `2000` | LLM 最大生成 token 数 |
| `CONSISTENCY_N` | `3` | 自一致性采样次数 |
| `CONSISTENCY_TEMP` | `0.3` | 自一致性采样温度 |
| `MAX_RETRIES` | `2` | 错误驱动重试的最大次数 |
| `SQLITE_PATH` | `datasets/spider_databases/database` | 默认 SQLite 数据库目录 |
| `MYSQL_HOST` | `localhost` | MySQL 主机 |
| `MYSQL_PORT` | `3306` | MySQL 端口 |
| `MYSQL_USER` | `root` | MySQL 用户名 |
| `MYSQL_PASSWORD` | （空） | MySQL 密码 |
| `MYSQL_DATABASE` | — | MySQL 数据库 |
| `API_SOURCES` | `[]` | API 数据源（JSON 字符串，可选） |
| `SANDBOX_ENABLED` | `true` | 是否启用 Docker 沙箱 |
| `SANDBOX_IMAGE` | `datapilot-sandbox:latest` | 沙箱镜像 |
| `SANDBOX_TIMEOUT` | `30` | 沙箱超时（秒） |
| `SANDBOX_MEMORY` | `256m` | 沙箱内存限制 |
| `SANDBOX_CPU` | `0.5` | 沙箱 CPU 限制 |
