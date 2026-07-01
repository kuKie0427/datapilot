"""FastAPI 路由处理器。"""

import json
import asyncio
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import StreamingResponse
from .schemas import (
    QueryRequest, QueryResponse, QueryStageInfo,
    SourceInfo, SourceListResponse, SourceSchemaResponse,
    HealthResponse, RegisterSourceRequest, LoginRequest, TokenResponse,
)
from ..agent import run_pipeline
from ..datasources import DataSourceRegistry, get_shared_registry
from ..config import config
from ..rag_store import RAGStore
from ..auth import (
    get_current_user,
    create_access_token,
    RBACManager,
    User,
    require_permission,
)

router = APIRouter()

# RAG 单例（registry 已由 datasources.get_shared_registry 统一管理）
_rag: RAGStore = None


def _get_registry() -> DataSourceRegistry:
    """获取共享的数据源注册表单例。

    与 agent/nodes.py 使用同一实例，保证运行时注册的数据源在查询时可见。
    """
    return get_shared_registry()


def _get_rag() -> RAGStore:
    global _rag
    if _rag is None:
        _rag = RAGStore()
    return _rag


@router.get("/health", response_model=HealthResponse)
async def health():
    """健康检查 —— 报告 LLM、RAG 和数据源状态。"""
    registry = _get_registry()
    connections = await registry.test_all()
    sources = [
        SourceInfo(
            source_id=s["source_id"],
            source_type=s["source_type"],
            connected=connections.get(s["source_id"], False),
        )
        for s in registry.list_sources()
    ]
    return HealthResponse(
        status="ok",
        llm_configured=bool(config.llm.api_key),
        rag_loaded=len(_get_rag().examples) > 0,
        sources=sources,
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    """登录获取 JWT。

    密码校验策略：
    - 生产环境（DEBUG=false）：必须校验密码哈希（bcrypt），用户表需预置 password_hash
    - 开发模式（DEBUG=true）：用户名 dev-admin / 任意密码 即可获取 admin 角色 token
      （仅用于本地联调，禁止在生产部署时开启）
    """
    import os
    from ..auth.store import get_auth_store
    debug_mode = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")
    allow_no_password = os.getenv("ALLOW_NO_PASSWORD_LOGIN", "false").lower() in ("1", "true", "yes")

    store = get_auth_store()
    user = await store.get_user(req.user_id, tenant_id=req.tenant_id or "default")
    if not user:
        # 用户不存在与密码错误返回相同提示，避免账号枚举
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # 密码校验：非开发模式必须校验
    if not (debug_mode and allow_no_password):
        if not req.password:
            raise HTTPException(status_code=401, detail="Password required")
        # 获取存储的密码哈希；无哈希的用户禁止登录（防止 dev-admin 在生产被滥用）
        stored_hash = getattr(user, "password_hash", "") or ""
        if not stored_hash:
            raise HTTPException(status_code=401, detail="User has no password set; contact admin")
        if not _verify_password(req.password, stored_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "roles": user.roles,
        "name": user.name,
    })
    return TokenResponse(access_token=token, token_type="Bearer", user_id=user.id, roles=user.roles)


def _verify_password(plain: str, hashed: str) -> bool:
    """校验密码哈希。支持 bcrypt 与开发用的明文（前缀 plain:）。

    plain: 前缀的明文哈希仅在 DEBUG 模式下接受，生产模式直接拒绝，
    避免默认存储的 dev-admin 凭据在生产环境被滥用。
    """
    import os
    import hmac as _hmac
    debug_mode = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

    if hashed.startswith("plain:"):
        # plain: 前缀仅开发模式可用；生产部署应清除所有 plain: 前缀的哈希
        if not debug_mode:
            return False
        return _hmac.compare_digest(hashed.encode(), f"plain:{plain}".encode())
    try:
        import bcrypt
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ImportError:
        # bcrypt 未装时无法安全校验 bcrypt 哈希，直接拒绝（不退化为不安全的明文比较）
        return False


@router.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest, user: User = Depends(get_current_user)):
    """提交自然语言查询并获取结果。

    需要认证：用户必须对 source_id 有 query 权限。
    支持 session_id 实现多轮对话。
    运行完整的四阶段流水线：
    查询改写 → 意图识别 → RAG → 语义层 → 模式感知生成 → 执行 → （纠错）
    """
    # RBAC 校验：用户能否查询该数据源
    await RBACManager.enforce_query(user, req.source_id)

    # 多轮对话：获取或创建会话
    from ..session import get_session_manager
    mgr = get_session_manager()
    session = mgr.get_or_create(req.session_id, user_id=user.id if user else "")
    session_id = session.session_id

    # 把用户上下文带入 pipeline，供行级过滤与列脱敏使用
    result = await run_pipeline(
        question=req.question,
        source_id=req.source_id,
        db_id=req.db_id,
        user=user,
        session_id=session_id,
    )

    # 查询完成后更新会话状态
    mgr.update_after_query(
        session_id=session_id,
        question=req.question,
        sql=result.get("generated_sql", ""),
        source_id=req.source_id,
        result_columns=(result.get("query_result") or {}).get("columns", []),
        answer=result.get("generated_sql", ""),
    )

    steps = [QueryStageInfo(**s) for s in result.get("steps", [])]

    return QueryResponse(
        success=result.get("success", False),
        question=req.question,
        intent=result.get("intent", "unknown"),
        output_type=result.get("output_type", "table"),
        sql=result.get("generated_sql", ""),
        result=result.get("query_result"),
        chart_spec=result.get("chart_spec"),
        steps=steps,
        retry_count=result.get("retry_count", 0),
        error=result.get("error", "") or result.get("execution_error", ""),
        total_time_ms=result.get("total_time_ms", 0.0),
        session_id=session_id,
        rewritten_question=result.get("rewritten_question"),
    )


@router.post("/query/stream")
async def query_stream(req: QueryRequest, user: User = Depends(get_current_user)):
    """通过 Server-Sent Events (SSE) 流式推送查询进度。

    需要认证。随着流水线推进逐步发送更新，
    最后发送最终结果。支持多轮对话 session_id。
    """
    await RBACManager.enforce_query(user, req.source_id)

    from ..session import get_session_manager
    mgr = get_session_manager()
    session = mgr.get_or_create(req.session_id, user_id=user.id if user else "")
    session_id = session.session_id

    async def event_generator():
        result = await run_pipeline(
            question=req.question,
            source_id=req.source_id,
            db_id=req.db_id,
            user=user,
            session_id=session_id,
        )
        mgr.update_after_query(
            session_id=session_id,
            question=req.question,
            sql=result.get("generated_sql", ""),
            source_id=req.source_id,
            result_columns=(result.get("query_result") or {}).get("columns", []),
            answer=result.get("generated_sql", ""),
        )

        # 每完成一步即发送
        for step in result.get("steps", []):
            yield f"data: {json.dumps(step, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.01)

        # 发送最终结果（字段需与前端 DataPilot-Frontend-v1.html 对齐）
        final = {
            "type": "final",
            "success": result.get("success", False),
            "sql": result.get("generated_sql", ""),
            "result": result.get("query_result"),
            "chart_spec": result.get("chart_spec"),
            "error": result.get("error", "") or result.get("execution_error", ""),
            "total_time_ms": result.get("total_time_ms", 0.0),
            "session_id": session_id,
            "output_type": result.get("output_type", "table"),
            "intent": result.get("intent", "unknown"),
            "rewritten_question": result.get("rewritten_question"),
        }
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sources", response_model=SourceListResponse)
async def list_sources(user: User = Depends(get_current_user)):
    """列出所有已注册的数据源（需登录）。"""
    registry = _get_registry()
    connections = await registry.test_all()
    sources = [
        SourceInfo(
            source_id=s["source_id"],
            source_type=s["source_type"],
            connected=connections.get(s["source_id"], False),
        )
        for s in registry.list_sources()
    ]
    return SourceListResponse(sources=sources)


@router.get("/sources/{source_id}/schema", response_model=SourceSchemaResponse)
async def get_source_schema(source_id: str, user: User = Depends(get_current_user)):
    """获取指定数据源的模式（需要 schema 权限）。"""
    await RBACManager.enforce_schema(user, source_id)
    registry = _get_registry()
    schema = await registry.get_schema(source_id)
    if not schema:
        raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")
    return SourceSchemaResponse(
        source_id=schema.source_id,
        source_type=schema.source_type,
        schema_text=schema.to_prompt_text(),
        tables=[
            {
                "name": t.name,
                "columns": [{"name": c.name, "dtype": c.dtype} for c in t.columns],
                "foreign_keys": t.foreign_keys,
                "row_count": t.row_count,
            }
            for t in schema.tables
        ],
    )


@router.post("/sources/register", response_model=SourceInfo)
async def register_source(
    req: RegisterSourceRequest,
    user: User = Depends(require_permission("datasource:*", "admin")),
):
    """在运行时注册新的数据源（需要 admin 权限）。"""
    registry = _get_registry()

    if req.source_type == "sql":
        from ..datasources import SQLAdapter
        adapter = SQLAdapter(
            source_id=req.source_id,
            connection_string=req.connection_string,
        )
    elif req.source_type == "csv":
        from ..datasources import CSVAdapter
        if not req.csv_path:
            raise HTTPException(status_code=400, detail="csv_path required for CSV source")
        adapter = CSVAdapter(
            source_id=req.source_id,
            csv_path=req.csv_path,
        )
    elif req.source_type == "api":
        from ..datasources import APIAdapter
        if not req.base_url:
            raise HTTPException(status_code=400, detail="base_url required for API source")
        adapter = APIAdapter(
            source_id=req.source_id,
            base_url=req.base_url,
            endpoints=req.endpoints or [],
            auth=req.auth,
            swagger_url=req.swagger_url,
        )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source type: {req.source_type}")

    registry.register(adapter)
    connected = await adapter.test_connection()
    return SourceInfo(
        source_id=req.source_id,
        source_type=req.source_type,
        connected=connected,
    )


@router.post("/sources/csv/upload", response_model=SourceInfo)
async def upload_csv(
    file: UploadFile = File(...),
    user: User = Depends(require_permission("datasource:*", "admin")),
):
    """上传 CSV 文件并注册为数据源（需要 admin 权限）。"""
    import os
    upload_dir = config.datasource.csv_upload_dir
    os.makedirs(upload_dir, exist_ok=True)

    # 防路径遍历：仅取文件名部分，丢弃任何目录片段
    filename = os.path.basename(file.filename or "")
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # 校验扩展名
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are allowed")

    # 限制文件大小（100MB），防止 DoS
    max_size = 100 * 1024 * 1024
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="File too large (max 100MB)")

    filepath = os.path.join(upload_dir, filename)
    # 最终路径校验：确保仍在 upload_dir 内
    if not os.path.abspath(filepath).startswith(os.path.abspath(upload_dir) + os.sep):
        raise HTTPException(status_code=400, detail="Invalid file path")

    with open(filepath, "wb") as f:
        f.write(content)

    source_id = f"csv_{os.path.splitext(filename)[0]}"
    from ..datasources import CSVAdapter
    adapter = CSVAdapter(source_id=source_id, csv_path=filepath)
    _get_registry().register(adapter)

    return SourceInfo(source_id=source_id, source_type="csv", connected=True)
