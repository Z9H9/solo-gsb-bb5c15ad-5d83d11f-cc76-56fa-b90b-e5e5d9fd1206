"""FastAPI application: HTTP language negotiation interpreter."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .negotiation import AcceptLanguageError, negotiate
from .schemas import (
    HealthResponse,
    NegotiationRequest,
    NegotiationResponse,
)
app = FastAPI(
    title="HTTP 语言协商解释 API",
    description=(
        "解析 RFC 9110 Accept-Language（q 权重、q=0 排除、通配符、重复范围），"
        "按 RFC 4647 lookup 逐级截断匹配服务端支持的 BCP 47 标签，"
        "标准 lookup 无可用候选时可沿可选的 locale_fallbacks 有向链回退，"
        "返回选中语言以及每个候选的接受/淘汰轨迹。"
    ),
    version=__version__,
)


@app.exception_handler(AcceptLanguageError)
async def accept_language_error_handler(
    request: Request, exc: AcceptLanguageError
) -> JSONResponse:
    # Same 422 envelope shape FastAPI uses for Pydantic validation errors.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "loc": ["body", "accept_language"],
                    "msg": str(exc),
                    "type": "accept_language.syntax_error",
                }
            ]
        },
    )


# Pydantic request validation uses FastAPI's built-in 422 handler,
# which returns the standard {"detail": [...]} envelope.


@app.get("/health", response_model=HealthResponse, tags=["健康检查"])
async def health() -> HealthResponse:
    """存活/就绪探针。"""
    return HealthResponse(status="ok", service="language-negotiator")


@app.post(
    "/api/v1/negotiate",
    response_model=NegotiationResponse,
    tags=["语言协商"],
    summary="解释 Accept-Language 并选出服务端语言",
)
async def negotiate_language(
    payload: NegotiationRequest,
) -> JSONResponse:
    result = negotiate(
        header=payload.accept_language,
        supported=payload.supported_languages,
        default=payload.default_language,
        locale_fallbacks=payload.locale_fallbacks,
    )
    response = NegotiationResponse.model_validate(result)
    if response.locale_fallback is None:
        # Drop rather than emit null: when locale_fallbacks did not
        # participate the response keeps the exact pre-feature key set.
        payload_body = response.model_dump(exclude={"locale_fallback"})
    else:
        payload_body = response.model_dump()
    return JSONResponse(content=payload_body)


@app.get("/", tags=["元信息"])
async def root() -> dict[str, str]:
    return {
        "service": "language-negotiator",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
        "negotiate": "POST /api/v1/negotiate",
    }
