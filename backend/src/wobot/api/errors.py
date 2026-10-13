import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """An error returned to clients as `{"error": {"code", "message"}}`."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _envelope(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return _envelope(exc.status_code, exc.code, exc.message)


async def handle_invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422 lists Pydantic's errors as `detail`; clients read one envelope."""
    fields = sorted({".".join(str(p) for p in e["loc"][1:]) for e in exc.errors() if e["loc"]})
    where = f": {', '.join(f for f in fields if f)}" if any(fields) else ""
    return _envelope(422, "INVALID_INPUT", f"The request is not valid{where}.")


async def handle_database_unavailable(_request: Request, exc: Exception) -> JSONResponse:
    """A connection or query that failed is said to have failed, never shown as nothing
    found: the client may try again."""
    logger.warning("database unavailable: %s", type(exc).__name__)
    return _envelope(503, "DATABASE_UNAVAILABLE", "The service is busy. Try again.")
