"""Errors in the contract shape: ``{"error": {"code", "message", "details"}}``.

Handlers never log request bodies or exception text; only the error code and the path.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("kalan.api")

_STATUS_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    413: "file_too_large",
    415: "unsupported_media_type",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def error_response(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message,
                                                               "details": details or {}}})


def _validation_details(exc: RequestValidationError) -> dict[str, Any]:
    # Location and message only; the offending input value is not echoed back.
    return {"errors": [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        log.info("%s %s -> %d %s", request.method, request.url.path, exc.status, exc.code)
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "validation_error", "The request is not valid.", _validation_details(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else code
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("%s %s -> 500 %s", request.method, request.url.path, type(exc).__name__)
        return error_response(500, "internal_error", "Something went wrong inside kalan.")
