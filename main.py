"""DataPilot 开发服务器入口。

生产环境请使用 uvicorn src.api.app:app --host 0.0.0.0 --port 8000。
"""

import uvicorn


def main():
    uvicorn.run(
        "src.api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()
