from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class ApiError(Exception):
    def __init__(self, code, status, message, details=None):
        self.code, self.status, self.message, self.details = code, status, message, details
        super().__init__(message)


def invalid(field, message):
    raise ApiError(
        "VALIDATION_ERROR",
        400,
        "Invalid request",
        {
            "fields": [{"field": field, "message": message}],
        },
    )


def response(error):
    payload = {"error_code": error.code, "message": error.message}
    if error.details is not None:
        payload["details"] = error.details
    headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
    return JSONResponse(payload, status_code=error.status, headers=headers)


def install_handlers(app):
    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = [{"field": ".".join(map(str, err["loc"])), "message": err["msg"]} for err in exc.errors()]
        return response(ApiError("VALIDATION_ERROR", 400, "Invalid request", {"fields": fields}))

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        codes = {404: "ROUTE_NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return response(
            ApiError(codes.get(exc.status_code, "VALIDATION_ERROR"), exc.status_code, str(exc.detail))
        )
