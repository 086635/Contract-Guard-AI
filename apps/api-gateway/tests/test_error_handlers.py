"""C51 全局错误包络测试。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.errors import register_error_handlers
from app.api.middlewares import IdempotencyMiddleware


class RequiredBody(BaseModel):
    name: str


def _test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)
    register_error_handlers(app)

    @app.get("/__test/not-found")
    async def not_found() -> None:
        raise HTTPException(status_code=404, detail="contract not found")

    @app.get("/__test/conflict")
    async def conflict() -> None:
        raise HTTPException(status_code=409, detail="version conflict")

    @app.post("/__test/validate")
    async def validate(body: RequiredBody) -> dict[str, str]:
        return {"name": body.name}

    @app.get("/__test/boom")
    async def boom() -> None:
        raise RuntimeError("database password leaked")

    @app.get("/__test/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_404_api_error_shape() -> None:
    client = TestClient(_test_app())
    response = client.get("/__test/not-found")

    assert response.status_code == 404
    assert response.json() == {
        "code": "ERR_CONTRACT_NOT_FOUND",
        "message": "资源不存在",
        "detail": "contract not found",
        "traceId": response.headers["X-Trace-Id"],
    }


def test_422_issues_shape() -> None:
    client = TestClient(_test_app())
    response = client.post("/__test/validate", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "ERR_VALIDATION"
    assert body["message"] == "参数校验失败"
    assert body["detail"] == "request validation failed"
    assert body["traceId"] == response.headers["X-Trace-Id"]
    assert body["issues"] == [
        {
            "path": "name",
            "code": "missing",
            "message": "Field required",
        }
    ]


def test_trace_id_header_and_body_match() -> None:
    client = TestClient(_test_app())
    response = client.get("/__test/conflict")

    assert response.status_code == 409
    assert response.json()["code"] == "ERR_DATA_CONFLICT"
    assert response.json()["traceId"] == response.headers["X-Trace-Id"]


def test_internal_error_redacted() -> None:
    client = TestClient(_test_app(), raise_server_exceptions=False)
    response = client.get("/__test/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "ERR_INTERNAL"
    assert body["message"] == "服务器内部错误"
    assert body["detail"] == "internal server error"
    assert "password" not in response.text
    assert body["traceId"] == response.headers["X-Trace-Id"]


def test_success_response_not_wrapped_by_error_handler() -> None:
    client = TestClient(_test_app())
    response = client.get("/__test/ok")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}