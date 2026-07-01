"""FastAPI 应用工厂。"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routes import router


def _check_security_config():
    """启动时安全配置检查。

    生产模式（DEBUG=false）下若使用默认 JWT 密钥则拒绝启动，
    避免攻击者用公开弱密钥伪造任意 token。
    """
    debug_mode = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")
    jwt_secret = os.getenv("JWT_SECRET", "")
    if not debug_mode and (not jwt_secret or jwt_secret == "datapilot-dev-secret-change-me"):
        raise RuntimeError(
            "JWT_SECRET must be set to a strong random value in production. "
            "Set DEBUG=true for local development."
        )


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    _check_security_config()

    app = FastAPI(
        title="DataPilot",
        description=(
            "面向自助查数的智能数据分析 Agent。\n\n"
            "认证：JWT Bearer（POST /api/v1/auth/login 获取 token，"
            "后续请求 Authorization: Bearer <token>）。\n"
            "权限：RBAC 角色模型，支持行级过滤与列脱敏。\n"
            "流式查询：POST /api/v1/query/stream 返回 SSE 事件流。"
        ),
        version="1.0.0",
    )

    # CORS —— 从环境变量读取白名单，避免通配源 + 凭证的反模式
    # 配置示例：CORS_ORIGINS=https://a.example.com,https://b.example.com
    cors_env = os.getenv("CORS_ORIGINS", "").strip()
    if cors_env:
        origins = [o.strip() for o in cors_env.split(",") if o.strip()]
        allow_credentials = True
    else:
        # 未配置时回退到通配源，但禁止携带凭证（CORS 规范要求）
        origins = ["*"]
        allow_credentials = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 挂载路由
    app.include_router(router, prefix="/api/v1")

    # 根端点
    @app.get("/")
    async def root():
        return {
            "name": "DataPilot",
            "version": "1.0.0",
            "docs": "/docs",
            "api": "/api/v1",
        }

    return app


app = create_app()
