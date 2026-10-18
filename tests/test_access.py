import re

from conftest import csrf, login


def test_roles_are_separated_on_server(client):
    response = login(client, "amministratore", "password-admin-test-molto-lunga")
    assert response.status_code == 302
    assert response.location.endswith("/admin/")
    assert client.get("/clients").status_code == 302
    assert client.get("/admin/users").status_code == 200

    client.post("/logout", data={"_csrf": csrf(client)})
    assert login(client).status_code == 302
    assert client.get("/admin/").status_code == 403
    assert client.get("/app").status_code == 200


def test_csrf_is_required_for_mutations(user_client):
    response = user_client.post("/clients/new", data={"name": "Malfattore"})
    assert response.status_code == 400


def test_security_headers_and_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_content_security_policy_allows_nonce_protected_form_scripts(user_client):
    response = user_client.get("/receipts/new")
    policy = response.headers["Content-Security-Policy"]
    nonce = re.search(r'<script nonce="([^"]+)"', response.get_data(as_text=True)).group(1)
    assert f"script-src 'self' 'nonce-{nonce}'" in policy
