"""HTTP authentication, request identifiers, and structured request logging."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from web2api.auth import AuthConfig, request_is_authorized
from web2api.logging_utils import (
    REQUEST_ID_HEADER,
    build_request_id,
    log_event,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)


def register_request_middleware(app: FastAPI, *, auth_config: AuthConfig) -> None:
    """Install authentication and request logging middleware."""

    @app.middleware("http")
    async def request_logging_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = build_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = set_request_id(request_id)
        request.state.request_id = request_id
        started_at = perf_counter()
        log_event(
            logger,
            logging.INFO,
            "request.started",
            method=request.method,
            path=request.url.path,
        )
        auth_state = getattr(request.app.state, "auth_config", auth_config)
        try:
            if auth_state.admin_is_disabled(request.url.path):
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                response = JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "Recipe administration is disabled until "
                            "WEB2API_ADMIN_TOKEN or WEB2API_ACCESS_TOKEN is configured."
                        ),
                    },
                )
                response.headers[REQUEST_ID_HEADER] = request_id
                log_event(
                    logger,
                    logging.WARNING,
                    "request.admin_disabled",
                    method=request.method,
                    path=request.url.path,
                    response_time_ms=elapsed_ms,
                )
                return response
            if auth_state.requires_auth(request.url.path) and not request_is_authorized(
                request.headers,
                auth_state,
                path=request.url.path,
            ):
                elapsed_ms = int((perf_counter() - started_at) * 1000)
                response = JSONResponse(
                    status_code=401,
                    content={
                        "detail": "Unauthorized. Provide Authorization: Bearer <token>.",
                    },
                    headers={"WWW-Authenticate": 'Bearer realm="web2api"'},
                )
                response.headers[REQUEST_ID_HEADER] = request_id
                log_event(
                    logger,
                    logging.WARNING,
                    "request.unauthorized",
                    method=request.method,
                    path=request.url.path,
                    response_time_ms=elapsed_ms,
                )
                return response
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            log_event(
                logger,
                logging.ERROR,
                "request.failed",
                method=request.method,
                path=request.url.path,
                response_time_ms=elapsed_ms,
                error=str(exc),
                exc_info=exc,
            )
            raise
        else:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            response.headers[REQUEST_ID_HEADER] = request_id
            log_event(
                logger,
                logging.INFO,
                "request.completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                response_time_ms=elapsed_ms,
            )
            return response
        finally:
            reset_request_id(token)
