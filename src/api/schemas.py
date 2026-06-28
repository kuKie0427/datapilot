"""API 请求/响应的 Pydantic 模型。"""

from pydantic import BaseModel, Field
from typing import Optional, Any
from enum import Enum


class QueryRequest(BaseModel):
    """自然语言查询请求。"""
    question: str = Field(..., description="Natural language question in Chinese or English")
    source_id: str = Field(default="default", description="Data source ID")
    db_id: Optional[str] = Field(default=None, description="Database ID (for Spider eval)")
    session_id: Optional[str] = Field(default=None, description="会话 ID，用于多轮对话")


class QueryStageInfo(BaseModel):
    stage: str
    status: str
    detail: str = ""
    timestamp: float = 0.0


class QueryResponse(BaseModel):
    """包含结果和元数据的完整查询响应。"""
    success: bool
    question: str
    intent: str = "unknown"
    output_type: str = "table"
    sql: str = ""
    result: Optional[dict] = None
    chart_spec: Optional[dict] = None
    steps: list[QueryStageInfo] = Field(default_factory=list)
    retry_count: int = 0
    error: str = ""
    total_time_ms: float = 0.0
    session_id: Optional[str] = None
    rewritten_question: Optional[str] = None


class SourceInfo(BaseModel):
    source_id: str
    source_type: str
    connected: bool = False


class SourceListResponse(BaseModel):
    sources: list[SourceInfo]


class SourceSchemaResponse(BaseModel):
    source_id: str
    source_type: str
    schema_text: str
    tables: list[dict] = []


class HealthResponse(BaseModel):
    status: str
    llm_configured: bool
    rag_loaded: bool
    sources: list[SourceInfo]


class RegisterSourceRequest(BaseModel):
    """在运行时注册新的数据源。"""
    source_id: str
    source_type: str  # sql | csv | api
    connection_string: Optional[str] = None
    csv_path: Optional[str] = None
    base_url: Optional[str] = None
    endpoints: Optional[list[dict]] = None
    auth: Optional[dict] = None
    swagger_url: Optional[str] = None  # API 数据源的 OpenAPI 文档地址


class LoginRequest(BaseModel):
    """登录请求。"""
    user_id: str
    tenant_id: Optional[str] = None
    password: Optional[str] = None  # 开发模式不校验


class TokenResponse(BaseModel):
    """登录成功返回的 JWT。"""
    access_token: str
    token_type: str = "Bearer"
    user_id: str
    roles: list[str] = []


class ToastType(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    INFO = "info"
