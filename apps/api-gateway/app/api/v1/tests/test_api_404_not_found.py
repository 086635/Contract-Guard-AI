"""边界族 2：资源不存在（404）。

覆盖：未知 contract_id / task_id / config_id 在多个端点下的语义化 404。
"""
from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

UNKNOWN = str(uuid4())


class Test404NotFound:
    def test_unknown_contract(self, client: TestClient):
        resp = client.get(f"/api/v1/contracts/{UNKNOWN}")
        assert resp.status_code == 404

    def test_unknown_contract_risks(self, client: TestClient):
        resp = client.get(f"/api/v1/contracts/{UNKNOWN}/risks")
        assert resp.status_code == 404

    def test_unknown_contract_elements(self, client: TestClient):
        resp = client.get(f"/api/v1/contracts/{UNKNOWN}/elements")
        assert resp.status_code == 404

    def test_unknown_contract_conflicts(self, client: TestClient):
        resp = client.get(f"/api/v1/contracts/{UNKNOWN}/conflicts")
        assert resp.status_code == 404

    def test_unknown_contract_seals(self, client: TestClient):
        resp = client.get(f"/api/v1/contracts/{UNKNOWN}/seals")
        assert resp.status_code == 404

    def test_unknown_task(self, client: TestClient):
        resp = client.get(f"/api/v1/tasks/{UNKNOWN}")
        assert resp.status_code == 404

    def test_unknown_task_stream(self, client: TestClient):
        resp = client.get(f"/api/v1/tasks/{UNKNOWN}/stream")
        assert resp.status_code == 404

    def test_unknown_rule(self, client: TestClient):
        resp = client.get("/api/v1/settings/rules/does-not-exist")
        assert resp.status_code == 404

    def test_unknown_report_contract(self, client: TestClient):
        resp = client.get(f"/api/v1/report/{UNKNOWN}")
        assert resp.status_code == 404

    def test_404_is_error_not_success_code(self, client: TestClient):
        """404 不应误用成功码，也不应返回 200/空 data。"""
        resp = client.get(f"/api/v1/contracts/{UNKNOWN}")
        assert resp.status_code == 404
        assert resp.status_code != 200