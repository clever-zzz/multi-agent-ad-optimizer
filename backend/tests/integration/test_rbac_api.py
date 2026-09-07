"""Role based access control enforced over HTTP.

Every row asserts the whole matrix for one endpoint so a permission change
shows up as a single readable failure instead of a scattered set.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ..conftest import ADMIN_EMAIL, ADMIN_PASSWORD, API, access_token, bearer

# (method, path, allowed roles)
ENDPOINT_MATRIX: list[tuple[str, str, frozenset[str]]] = [
    ("GET", "/campaigns", frozenset({"admin", "optimizer", "analyst", "viewer"})),
    ("POST", "/campaigns", frozenset({"admin", "optimizer"})),
    ("PATCH", "/campaigns/whatever", frozenset({"admin", "optimizer"})),
    ("DELETE", "/campaigns/whatever", frozenset({"admin", "optimizer"})),
    ("GET", "/runs", frozenset({"admin", "optimizer", "analyst", "viewer"})),
    ("POST", "/runs", frozenset({"admin", "optimizer"})),
    ("GET", "/actions", frozenset({"admin", "optimizer", "analyst", "viewer"})),
    ("POST", "/actions/whatever/approve", frozenset({"admin", "optimizer"})),
    ("POST", "/actions/whatever/execute", frozenset({"admin", "optimizer"})),
    ("GET", "/alerts", frozenset({"admin", "optimizer", "analyst", "viewer"})),
    ("POST", "/alerts/whatever/acknowledge", frozenset({"admin", "optimizer", "analyst"})),
    ("GET", "/analytics/overview", frozenset({"admin", "optimizer", "analyst", "viewer"})),
    ("GET", "/creatives", frozenset({"admin", "optimizer", "analyst", "viewer"})),
    ("GET", "/admin/system", frozenset({"admin"})),
    ("GET", "/admin/audit", frozenset({"admin"})),
    ("GET", "/admin/ab-tests", frozenset({"admin"})),
    ("POST", "/admin/prune", frozenset({"admin"})),
]

ALL_ROLES = ("admin", "optimizer", "analyst", "viewer")

# Bodies that satisfy schema validation so a 403 is about permission, not payload.
BODIES: dict[str, dict[str, Any]] = {
    "POST /campaigns": {
        "name": "RBAC Probe",
        "platform": "mock",
        "daily_budget": 100.0,
    },
    "PATCH /campaigns/whatever": {"name": "RBAC Probe Renamed"},
    # Points at a campaign that does not exist so the permission check is what
    # decides the outcome and no optimisation run is actually dispatched.
    "POST /runs": {"background": True, "campaign_ids": ["campaign_rbac_probe"]},
    "POST /actions/whatever/approve": {},
    "POST /actions/whatever/execute": {},
    "POST /alerts/whatever/acknowledge": {},
    "POST /admin/prune": {},
}


@pytest.fixture
async def role_headers(client: httpx.AsyncClient) -> dict[str, dict[str, str]]:
    """Headers for one account per role, all in the same database."""
    headers: dict[str, dict[str, str]] = {
        "admin": bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
    }
    admin = headers["admin"]
    for role in ("optimizer", "analyst", "viewer"):
        email = role + ".rbac@adoptimizer.dev"
        created = await client.post(
            API + "/auth/users",
            json={"email": email, "password": "Rbac!Probe123", "role": role},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        headers[role] = bearer(await access_token(client, email, "Rbac!Probe123"))
    return headers


@pytest.mark.parametrize(("method", "path", "allowed"), ENDPOINT_MATRIX)
async def test_role_matrix(
    client: httpx.AsyncClient,
    role_headers: dict[str, dict[str, str]],
    method: str,
    path: str,
    allowed: frozenset[str],
) -> None:
    body = BODIES.get(method + " " + path)
    for role in ALL_ROLES:
        response = await client.request(method, API + path, json=body, headers=role_headers[role])
        if role in allowed:
            # Permitted: anything except an authorisation failure. A 404 for a
            # made-up id still proves the permission check passed.
            assert response.status_code != 403, role + " was denied " + path
            assert response.status_code != 401, role + " was unauthenticated on " + path
        else:
            assert response.status_code == 403, (
                role + " should not reach " + path + " but got " + str(response.status_code)
            )
            assert response.json()["code"] == "permission_denied"


async def test_anonymous_access_is_rejected_everywhere(client: httpx.AsyncClient) -> None:
    for method, path, _allowed in ENDPOINT_MATRIX:
        response = await client.request(method, API + path, json=BODIES.get(method + " " + path))
        assert response.status_code == 401, path


async def test_probes_reach_past_authorisation(client: httpx.AsyncClient, role_headers) -> None:
    """Guard against the matrix passing because every request 404s early."""
    admin = role_headers["admin"]
    assert (await client.get(API + "/campaigns", headers=admin)).status_code == 200
    assert (await client.get(API + "/admin/system", headers=admin)).status_code == 200
    assert (await client.get(API + "/campaigns/does-not-exist", headers=admin)).status_code == 404
