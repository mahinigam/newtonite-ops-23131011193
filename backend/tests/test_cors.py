"""The browser-facing CORS contract.

The Docker stack serves the SPA and the API from one origin, so these headers are not
needed there. The Vite development setup and any split-origin deployment do need them:
a preflight has to allow every method and request header the client actually uses, and a
script can only read a response header that is explicitly exposed.
"""

from __future__ import annotations

from httpx import AsyncClient

ORIGIN = "http://localhost:5173"


async def test_preflight_allows_the_member_management_method_and_headers(
    client: AsyncClient,
) -> None:
    response = await client.options(
        "/api/v1/teams/00000000-0000-0000-0000-000000000000/members/"
        "00000000-0000-0000-0000-000000000000",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type,if-match,idempotency-key",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "PUT" in response.headers["access-control-allow-methods"]
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "content-type" in allowed_headers
    assert "if-match" in allowed_headers
    assert "idempotency-key" in allowed_headers


async def test_simple_response_exposes_the_headers_the_client_reads(
    client: AsyncClient,
) -> None:
    response = await client.get("/healthz", headers={"Origin": ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    exposed = response.headers["access-control-expose-headers"]
    assert "ETag" in exposed
    assert "Location" in exposed
    assert "Idempotent-Replay" in exposed
