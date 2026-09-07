"""End-to-end authentication flows over HTTP."""

from __future__ import annotations

import json

import httpx
import pytest

from ..conftest import ADMIN_EMAIL, ADMIN_PASSWORD, API, access_token, bearer, login

AUTH = API + "/auth"
NEW_PASSWORD = "Rotat3d!Passw0rd"


def problem(response: httpx.Response) -> dict[str, object]:
    """Parse an RFC 9457 style problem document."""
    body = response.json()
    assert body["status"] == response.status_code
    assert body["type"].startswith("https://")
    assert body["title"]
    assert body["detail"]
    assert body["code"]
    return body


class TestLogin:
    async def test_bootstrap_admin_can_sign_in(self, client: httpx.AsyncClient) -> None:
        response = await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 30 * 60
        assert body["access_token"] and body["refresh_token"]
        assert body["user"]["email"] == ADMIN_EMAIL
        assert body["user"]["role"] == "admin"
        assert body["user"]["must_change_password"] is True

    async def test_password_hash_is_never_returned(self, client: httpx.AsyncClient) -> None:
        body = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        assert "hashed_password" not in body["user"]
        assert "password" not in body["user"]
        assert "argon2" not in json.dumps(body, default=str)

    async def test_wrong_password_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await login(client, ADMIN_EMAIL, "Wr0ng!Password")
        assert response.status_code == 401
        assert problem(response)["code"] == "unauthenticated"

    async def test_unknown_email_gets_the_identical_error(self, client: httpx.AsyncClient) -> None:
        """No user enumeration: same status and same wording as a wrong password."""
        known = await login(client, ADMIN_EMAIL, "Wr0ng!Password")
        unknown = await login(client, "nobody@adoptimizer.dev", "Wr0ng!Password")
        assert known.status_code == unknown.status_code == 401
        assert known.json()["detail"] == unknown.json()["detail"]

    async def test_malformed_email_is_a_validation_error(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            AUTH + "/login", json={"email": "not-an-email", "password": "x"}
        )
        assert response.status_code == 422

    async def test_empty_password_is_a_validation_error(self, client: httpx.AsyncClient) -> None:
        response = await client.post(AUTH + "/login", json={"email": ADMIN_EMAIL, "password": ""})
        assert response.status_code == 422

    async def test_account_locks_after_repeated_failures(self, client: httpx.AsyncClient) -> None:
        for _ in range(5):
            assert (await login(client, ADMIN_EMAIL, "Wr0ng!Password")).status_code == 401
        locked = await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert locked.status_code == 401
        assert "locked" in locked.json()["detail"].lower()

    async def test_successful_login_resets_the_failure_counter(
        self, client: httpx.AsyncClient
    ) -> None:
        for _ in range(4):
            await login(client, ADMIN_EMAIL, "Wr0ng!Password")
        assert (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).status_code == 200
        for _ in range(4):
            await login(client, ADMIN_EMAIL, "Wr0ng!Password")
        assert (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).status_code == 200


class TestProtectedRoutes:
    async def test_me_returns_the_principal(self, client: httpx.AsyncClient) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.get(AUTH + "/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["email"] == ADMIN_EMAIL

    async def test_me_requires_a_token(self, client: httpx.AsyncClient) -> None:
        response = await client.get(AUTH + "/me")
        assert response.status_code == 401
        body = problem(response)
        assert body["code"] == "unauthenticated"
        assert "WWW-Authenticate" in response.headers

    async def test_garbage_token_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.get(AUTH + "/me", headers=bearer("not.a.jwt"))
        assert response.status_code == 401

    async def test_malformed_authorization_header_is_rejected(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(AUTH + "/me", headers={"Authorization": "Token abc"})
        assert response.status_code == 401

    async def test_refresh_token_cannot_be_used_as_a_bearer_token(
        self, client: httpx.AsyncClient
    ) -> None:
        tokens = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        response = await client.get(AUTH + "/me", headers=bearer(tokens["refresh_token"]))
        assert response.status_code == 401


class TestTokenRotation:
    async def test_refresh_issues_a_new_pair(self, client: httpx.AsyncClient) -> None:
        tokens = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        response = await client.post(
            AUTH + "/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert response.status_code == 200
        rotated = response.json()
        assert rotated["access_token"] != tokens["access_token"]
        assert rotated["refresh_token"] != tokens["refresh_token"]
        assert rotated["user"]["email"] == ADMIN_EMAIL

    async def test_a_rotated_refresh_token_cannot_be_replayed(
        self, client: httpx.AsyncClient
    ) -> None:
        """Rotation must revoke the presented token or a stolen one works forever."""
        tokens = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        first = await client.post(
            AUTH + "/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert first.status_code == 200
        replay = await client.post(
            AUTH + "/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert replay.status_code == 401

    async def test_logout_revokes_the_session(self, client: httpx.AsyncClient) -> None:
        tokens = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        assert (
            await client.post(AUTH + "/logout", json={"refresh_token": tokens["refresh_token"]})
        ).status_code == 204
        response = await client.post(
            AUTH + "/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_logout_with_an_unknown_token_is_idempotent(
        self, client: httpx.AsyncClient
    ) -> None:
        forged = "x" * 60
        response = await client.post(AUTH + "/logout", json={"refresh_token": forged})
        assert response.status_code in {204, 401}

    async def test_short_refresh_token_is_a_validation_error(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(AUTH + "/refresh", json={"refresh_token": "short"})
        assert response.status_code == 422


class TestPasswordChange:
    async def test_change_password_rotates_credentials(self, client: httpx.AsyncClient) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.post(
            AUTH + "/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
            headers=headers,
        )
        assert response.status_code == 204

        assert (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).status_code == 401
        after = await login(client, ADMIN_EMAIL, NEW_PASSWORD)
        assert after.status_code == 200
        assert after.json()["user"]["must_change_password"] is False

    async def test_change_password_revokes_existing_sessions(
        self, client: httpx.AsyncClient
    ) -> None:
        tokens = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        await client.post(
            AUTH + "/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
            headers=bearer(tokens["access_token"]),
        )
        response = await client.post(
            AUTH + "/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_wrong_current_password_is_rejected(self, client: httpx.AsyncClient) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.post(
            AUTH + "/change-password",
            json={"current_password": "Wr0ng!Password", "new_password": NEW_PASSWORD},
            headers=headers,
        )
        assert response.status_code == 401

    @pytest.mark.parametrize(
        ("weak", "expected"),
        [
            ("NoDigitsHere!", "must contain a digit"),
            ("1234567890!", "must contain a letter"),
            ("LettersOnly1234", "must contain a symbol"),
        ],
    )
    async def test_weak_new_password_is_refused_by_the_policy(
        self, client: httpx.AsyncClient, weak: str, expected: str
    ) -> None:
        """These all clear the schema's length gate, so the service policy decides."""
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.post(
            AUTH + "/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": weak},
            headers=headers,
        )
        assert response.status_code == 422
        body = problem(response)
        assert body["code"] == "validation_failed"
        assert expected in str(body["detail"])

    async def test_short_new_password_is_refused_by_the_schema(
        self, client: httpx.AsyncClient
    ) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.post(
            AUTH + "/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": "Sh0rt!"},
            headers=headers,
        )
        assert response.status_code == 422
        assert problem(response)["code"] == "validation_failed"

    async def test_reusing_the_same_password_is_refused(self, client: httpx.AsyncClient) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.post(
            AUTH + "/change-password",
            json={"current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD},
            headers=headers,
        )
        assert response.status_code == 422
        assert "differ" in response.json()["detail"].lower()

    async def test_logout_everywhere_revokes_all_sessions(self, client: httpx.AsyncClient) -> None:
        first = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        second = (await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)).json()
        response = await client.post(
            AUTH + "/logout-everywhere", headers=bearer(first["access_token"])
        )
        assert response.status_code == 204
        for tokens in (first, second):
            assert (
                await client.post(
                    AUTH + "/refresh", json={"refresh_token": tokens["refresh_token"]}
                )
            ).status_code == 401


class TestUserAdministration:
    async def test_admin_creates_an_operator(self, client: httpx.AsyncClient) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.post(
            AUTH + "/users",
            json={
                "email": "optimizer@adoptimizer.dev",
                "password": "Operat0r!Passw0rd",
                "role": "optimizer",
                "full_name": "Ops Person",
            },
            headers=headers,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["role"] == "optimizer"
        assert body["is_active"] is True
        assert (
            await login(client, "optimizer@adoptimizer.dev", "Operat0r!Passw0rd")
        ).status_code == 200

    async def test_duplicate_email_conflicts(self, client: httpx.AsyncClient) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        payload = {
            "email": "dupe@adoptimizer.dev",
            "password": "Operat0r!Passw0rd",
            "role": "viewer",
        }
        assert (
            await client.post(AUTH + "/users", json=payload, headers=headers)
        ).status_code == 201
        response = await client.post(AUTH + "/users", json=payload, headers=headers)
        assert response.status_code == 409
        assert problem(response)["code"] == "conflict"

    async def test_weak_password_is_refused_at_creation(self, client: httpx.AsyncClient) -> None:
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = await client.post(
            AUTH + "/users",
            json={"email": "weak@adoptimizer.dev", "password": "weakpassword", "role": "viewer"},
            headers=headers,
        )
        assert response.status_code == 422

    async def test_non_admin_cannot_create_users(
        self, client: httpx.AsyncClient, make_user
    ) -> None:
        analyst = await make_user("analyst")
        response = await client.post(
            AUTH + "/users",
            json={
                "email": "x@adoptimizer.dev",
                "password": "Operat0r!Passw0rd",
                "role": "viewer",
            },
            headers=analyst["headers"],
        )
        assert response.status_code == 403
        assert problem(response)["code"] == "permission_denied"

    async def test_admin_lists_accounts(self, client: httpx.AsyncClient, make_user) -> None:
        await make_user("viewer")
        headers = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        body = (await client.get(AUTH + "/users", headers=headers)).json()
        emails = {user["email"] for user in body}
        assert ADMIN_EMAIL in emails
        assert "viewer.tester@adoptimizer.dev" in emails

    async def test_non_admin_cannot_list_accounts(
        self, client: httpx.AsyncClient, make_user
    ) -> None:
        viewer = await make_user("viewer")
        assert (await client.get(AUTH + "/users", headers=viewer["headers"])).status_code == 403

    async def test_admin_can_deactivate_an_account(
        self, client: httpx.AsyncClient, make_user
    ) -> None:
        viewer = await make_user("viewer")
        admin = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        listed = (await client.get(AUTH + "/users", headers=admin)).json()
        user_id = next(u["id"] for u in listed if u["email"] == viewer["email"])

        response = await client.patch(
            API + "/admin/users/" + user_id, json={"is_active": False}, headers=admin
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        blocked = await login(client, viewer["email"], viewer["password"])
        assert blocked.status_code == 401
        assert "deactivated" in blocked.json()["detail"].lower()

    async def test_deactivating_revokes_live_sessions(
        self, client: httpx.AsyncClient, make_user
    ) -> None:
        viewer = await make_user("viewer")
        admin = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        listed = (await client.get(AUTH + "/users", headers=admin)).json()
        user_id = next(u["id"] for u in listed if u["email"] == viewer["email"])
        assert (await client.get(AUTH + "/me", headers=viewer["headers"])).status_code == 200

        await client.patch(
            API + "/admin/users/" + user_id, json={"is_active": False}, headers=admin
        )
        # The access token is stateless, so the session check in get_current_claims
        # is what makes deactivation take effect before the token expires.
        blocked = await client.get(AUTH + "/me", headers=viewer["headers"])
        assert blocked.status_code == 401
        assert problem(blocked)["code"] == "unauthenticated"

    async def test_admin_can_change_a_role(self, client: httpx.AsyncClient, make_user) -> None:
        viewer = await make_user("viewer")
        admin = bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        listed = (await client.get(AUTH + "/users", headers=admin)).json()
        user_id = next(u["id"] for u in listed if u["email"] == viewer["email"])

        # A viewer cannot trigger runs; after promotion to optimizer it can.
        assert (
            await client.post(API + "/runs", json={}, headers=viewer["headers"])
        ).status_code == 403

        response = await client.patch(
            API + "/admin/users/" + user_id, json={"role": "optimizer"}, headers=admin
        )
        assert response.status_code == 200
        assert response.json()["role"] == "optimizer"

        promoted = bearer(await access_token(client, viewer["email"], viewer["password"]))
        assert (await client.post(API + "/runs", json={}, headers=promoted)).status_code != 403

    async def test_last_admin_cannot_deactivate_itself(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        me = (await client.get(AUTH + "/me", headers=admin_headers)).json()
        response = await client.patch(
            API + "/admin/users/" + me["id"], json={"is_active": False}, headers=admin_headers
        )
        assert response.status_code == 409
        assert "last active administrator" in response.json()["detail"]
        assert (await client.get(AUTH + "/me", headers=admin_headers)).status_code == 200

    async def test_last_admin_cannot_demote_itself(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        me = (await client.get(AUTH + "/me", headers=admin_headers)).json()
        response = await client.patch(
            API + "/admin/users/" + me["id"], json={"role": "viewer"}, headers=admin_headers
        )
        assert response.status_code == 409

    async def test_empty_user_patch_is_a_validation_error(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        me = (await client.get(AUTH + "/me", headers=admin_headers)).json()
        response = await client.patch(
            API + "/admin/users/" + me["id"], json={}, headers=admin_headers
        )
        assert response.status_code == 422

    async def test_unknown_user_returns_404(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.patch(
            API + "/admin/users/run_missing", json={"is_active": False}, headers=admin_headers
        )
        assert response.status_code == 404

    async def test_non_admin_cannot_patch_users(self, client: httpx.AsyncClient, make_user) -> None:
        analyst = await make_user("analyst")
        response = await client.patch(
            API + "/admin/users/whoever", json={"is_active": False}, headers=analyst["headers"]
        )
        assert response.status_code == 403
