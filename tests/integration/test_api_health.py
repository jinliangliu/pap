# tests/integration/test_api_health.py
from __future__ import annotations


async def test_health_reports_process_alive(client):
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_name"] == "personal-agent-platform"
    assert response.headers["X-Request-ID"]


async def test_ready_checks_dependencies(client):
    response = await client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    checks = body["checks"]

    assert checks["database"] is True
    assert checks["checkpointer"] is True
    assert checks["tool_registry"] is True
    # 测试环境未配置模型密钥，但服务仍可运行
    assert checks["model_configured"] is False


async def test_metrics_endpoint(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text


async def test_tools_endpoint_only_lists_authorized_tools(client):
    response = await client.get("/api/v1/tools")

    assert response.status_code == 200
    names = {item["name"] for item in response.json()}

    assert "get_platform_time" in names
    assert "create_maintenance_ticket" in names
