import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator

import anyio
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from auth import XSUAAAuthMiddleware
import tool as tool_registry
import tools  # noqa: F401 – 도구 등록 실행
import tools_weather  # noqa: F401 – Open Meteo 날씨 도구 등록

# ── 로깅 ──

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── MCP 서버 ──

mcp_app = Server("sap-mcp-server")


@mcp_app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in tool_registry.list_tools()
    ]


@mcp_app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    result = await anyio.to_thread.run_sync(
        lambda: tool_registry.call_tool(name, arguments)
    )
    if isinstance(result, str):
        return [TextContent(type="text", text=result)]
    if isinstance(result, dict):
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    if isinstance(result, list):
        return result
    return [TextContent(type="text", text=str(result))]


# ── Streamable HTTP Transport ──

session_manager = StreamableHTTPSessionManager(
    app=mcp_app,
    event_store=None,
    json_response=False,
    stateless=False,
)


class MCPRoute:
    """GET/POST /mcp 를 StreamableHTTPSessionManager로 위임하는 ASGI 핸들러."""

    async def __call__(self, scope, receive, send) -> None:
        await session_manager.handle_request(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[None]:
    async with session_manager.run():
        logger.info("StreamableHTTP session manager started")
        try:
            yield
        finally:
            logger.info("StreamableHTTP session manager shutting down")


async def health(_request: Request) -> JSONResponse:
    """CF 헬스 체크 엔드포인트."""
    return JSONResponse({"status": "ok"})


async def check_heatwave(request: Request) -> JSONResponse:
    """폭염 판단 엔드포인트(추가)"""
    body = await request.json()
    dates = body.get("dates", [])
    max_temperatures = body.get("max_temperatures", [])
    
    alerts = [
        {"date": dates[i], "temp": max_temperatures[i]}
        for i in range(len(dates))
        if max_temperatures[i] >= 30
    ]
    
    return JSONResponse({
        "alert": len(alerts) > 0,
        "dates": alerts
    })

async def check_rain(request: Request) -> JSONResponse:
    """강수 판단 엔드포인트(추가)"""
    body = await request.json()
    dates = body.get("dates", [])
    probabilities = body.get("probabilities", [])
    
    rainy_days = [
        {"date": dates[i], "probability": probabilities[i]}
        for i in range(len(dates))
        if probabilities[i] >= 60
    ]
    
    return JSONResponse({
        "alert": len(rainy_days) > 0,
        "dates": rainy_days
    })


# ── Starlette 앱 ──

app = Starlette(
    routes=[
        Route("/mcp", endpoint=MCPRoute()),   # GET + POST 모두 처리
        Route("/health", endpoint=health, methods=["GET"]),
    ],
    lifespan=lifespan,
)

app.add_middleware(XSUAAAuthMiddleware)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
