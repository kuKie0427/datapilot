"""DataPilot 中央配置。"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMConfig:
    """LLM 服务商配置。"""
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "openai"))
    api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", os.getenv("DEEPSEEK_API_KEY", "")))
    base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "deepseek-chat"))
    temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.0")))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_TOKENS", "2000")))

    # 自一致性采样
    consistency_n: int = field(default_factory=lambda: int(os.getenv("CONSISTENCY_N", "3")))
    consistency_temperature: float = field(default_factory=lambda: float(os.getenv("CONSISTENCY_TEMP", "0.3")))

    # 错误驱动重试的最大次数
    max_retries: int = field(default_factory=lambda: int(os.getenv("MAX_RETRIES", "2")))


@dataclass
class RAGConfig:
    """RAG 向量存储配置。"""
    embedding_model: str = "all-MiniLM-L6-v2"
    max_examples: int = 7000
    top_k: int = 3
    index_cache: Path = PROJECT_ROOT / "rag_index.pkl"


@dataclass
class SandboxConfig:
    """Docker 沙箱配置。"""
    enabled: bool = field(default_factory=lambda: os.getenv("SANDBOX_ENABLED", "true").lower() == "true")
    docker_image: str = field(default_factory=lambda: os.getenv("SANDBOX_IMAGE", "python:3.11-slim"))
    timeout: int = field(default_factory=lambda: int(os.getenv("SANDBOX_TIMEOUT", "30")))
    memory_limit: str = field(default_factory=lambda: os.getenv("SANDBOX_MEMORY", "256m"))
    cpu_limit: float = field(default_factory=lambda: float(os.getenv("SANDBOX_CPU", "0.5")))
    network_disabled: bool = True
    work_dir: Path = PROJECT_ROOT / ".sandbox"


@dataclass
class DataSourceConfig:
    """数据源注册表 —— 以 source id 为键。"""
    # 默认 SQLite (Spider) 数据源
    default_sqlite_path: str = field(default_factory=lambda: os.getenv(
        "SQLITE_PATH", str(PROJECT_ROOT / "datasets" / "spider_databases" / "database")
    ))
    # MySQL
    mysql_host: str = field(default_factory=lambda: os.getenv("MYSQL_HOST", "localhost"))
    mysql_port: int = field(default_factory=lambda: int(os.getenv("MYSQL_PORT", "3306")))
    mysql_user: str = field(default_factory=lambda: os.getenv("MYSQL_USER", "root"))
    mysql_password: str = field(default_factory=lambda: os.getenv("MYSQL_PASSWORD", ""))
    mysql_database: str = field(default_factory=lambda: os.getenv("MYSQL_DATABASE", ""))

    # CSV 上传目录
    csv_upload_dir: Path = PROJECT_ROOT / "uploads"

    # API 数据源（环境变量中的 JSON 字符串）
    api_sources_json: str = field(default_factory=lambda: os.getenv("API_SOURCES", "[]"))


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    datasource: DataSourceConfig = field(default_factory=DataSourceConfig)
    debug: bool = field(default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true")


config = Config()
