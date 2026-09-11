"""边界族 1：非法输入与 Schema 校验失败（422）。

覆盖：非法文件格式（.exe）、非法 UUID、Query 参数越界（page ming lex）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class Test422Validation:
    def test_upload_unsupported_exe(self, client: TestClient):
        """上传不支持的 .exe 文件格式 -> 422。请求体匹配 UploadBody。"""
        resp = client.post(
            "/api/v1/contracts/upload",
            json={"fileKey": "contracts/test.docx", "fileName": "malware.exe", "fileType": "DOCX"},
        )
        assert resp.status_code == 422, resp.text

    def test_upload_missing_filename(self, client: TestClient):
        """缺失必填 fileName -> 422。其余字段匹配 UploadBody。"""
        resp = client.post(
            "/api/v1/contracts/upload",
            json={"fileKey": "contracts/test.docx", "fileType": "DOCX"},
        )
        assert resp.status_code == 422, resp.text

    def test_invalid_contract_uuid(self, client: TestClient):
        """非法的 contract_id（非 UUID）-> 422。"""
        resp = client.get("/api/v1/contracts/not-a-uuid")
        assert resp.status_code == 422, resp.text

    def test_invalid_task_uuid(self, client: TestClient):
        """非法的 task_id（非 UUID）-> 422。"""
        resp = client.get("/api/v1/tasks/not-a-uuid")
        assert resp.status_code == 422, resp.text

    def test_page_below_min(self, client: TestClient):
        """page=0 突破 ge=1 下限 -> 422。"""
        resp = client.get("/api/v1/contracts", params={"page": 0})
        assert resp.status_code == 422, resp.text

    def test_page_size_above_max(self, client: TestClient):
        """page_size=200 突破 le=100 上限 -> 422。"""
        resp = client.get("/api/v1/contracts", params={"page_size": 200})
        assert resp.status_code == 422, resp.text

    def test_validation_response_shape(self, client: TestClient):
        """422 响应应携带结构化校验错误 detail（数组形式）。"""
        resp = client.get("/api/v1/contracts", params={"page": 0})
        body = resp.json()
        assert isinstance(body.get("detail"), list)
        err = body["detail"][0]
        assert err["type"] == "greater_than_equal"  # page >= 1 约束被违反
        assert err["loc"] == ["query", "page"]