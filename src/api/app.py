"""FastAPI 应用工厂。"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routes import router


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    app = FastAPI(
        title="DataPilot",
        description="面向自助查数的智能数据分析 Agent",
        version="1.0.0",
    )

    # CORS —— 允许前端调用此 API
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
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
