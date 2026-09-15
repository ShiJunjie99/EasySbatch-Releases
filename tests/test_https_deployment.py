"""M10-B2 deployment boundaries without opening a real listener."""

from dataclasses import replace
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.ssh_poc import IdentityProbeResult
from sbatch_agent.web import WebConfig, create_app


ROOT = Path(__file__).parents[1]
PUBLIC_URL = "https://cluster.example.edu:18443"


class FakeContext:
    def __init__(self, username):
        uid = 1001 if username == "alice" else 1002
        self.identity = IdentityProbeResult(
            username=username, uid=uid, gid=uid, groups=(username,),
            home=f"/home/{username}", pwd=f"/home/{username}",
            hostname="example-cluster.cluster.local",
        )
        self.connected = True

    def close(self):
        self.connected = False

    def verify_identity(self):
        return self.identity


class FakeAuthenticator:
    def __init__(self):
        self.calls = []

    def authenticate(self, credentials):
        self.calls.append(credentials.username)
        return FakeContext(credentials.username)


@pytest.fixture
def lan_config(tmp_path):
    return WebConfig(
        tmp_path / "data/jobs.sqlite3", tmp_path / "runs",
        authentication_enabled=True, session_cookie_secure=True,
        deployment_mode="lan_https", public_base_url=PUBLIC_URL,
    )


def csrf(client):
    page = client.get("/login")
    assert page.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]


def test_lan_https_config_requires_auth_secure_cookie_and_exact_ip_url(lan_config):
    assert lan_config.trusted_hosts == ("localhost", "127.0.0.1", "cluster.example.edu")
    for changes in (
        {"authentication_enabled": False},
        {"session_cookie_secure": False},
        {"public_base_url": "http://cluster.example.edu:18443"},
        {"public_base_url": "https://other.example.edu:18443"},
        {"public_base_url": "https://cluster.example.edu"},
        {"public_base_url": "https://cluster.example.edu:18443/login"},
        {"public_base_url": "https://user@cluster.example.edu:18443"},
        {"public_base_url": "https://cluster.example.edu:18443/?next=x"},
    ):
        with pytest.raises(ValueError):
            replace(lan_config, **changes)


def test_loopback_and_lan_trusted_hosts_are_separate(tmp_path):
    loopback = WebConfig(tmp_path / "jobs.sqlite3", tmp_path / "runs")
    assert loopback.trusted_hosts == ("localhost", "127.0.0.1")
    with pytest.raises(ValueError):
        replace(loopback, public_base_url=PUBLIC_URL)


def test_https_cookie_host_csrf_and_http_downgrade_guards(lan_config):
    authenticator = FakeAuthenticator()
    app = create_app(lan_config, profiles=StaticProfiles(), ssh_authenticator=authenticator)
    with TestClient(app, base_url=PUBLIC_URL) as client:
        first_page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', first_page.text)[1]
        cookie = first_page.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert "domain=" not in cookie

        rejected = client.post(
            "/login",
            data={"csrf_token": token, "username": "bob", "password": "test-only"},
            headers={"Origin": "http://cluster.example.edu:18443"},
        )
        assert rejected.status_code == 403 and authenticator.calls == []

        accepted = client.post(
            "/login",
            data={"csrf_token": csrf(client), "username": "bob", "password": "test-only"},
            headers={"Origin": PUBLIC_URL}, follow_redirects=False,
        )
        assert accepted.status_code == 303 and authenticator.calls == ["bob"]


def test_trusted_host_rejects_arbitrary_host(lan_config):
    app = create_app(lan_config, profiles=StaticProfiles(), ssh_authenticator=FakeAuthenticator())
    with TestClient(app, base_url=PUBLIC_URL) as client:
        assert client.get("/login").status_code == 200
        assert client.get("/login", headers={"Host": "evil.example"}).status_code == 400


def test_proxy_template_is_https_only_and_does_not_log_secrets():
    text = (ROOT / "config/m10b2-httpd.example.conf").read_text(encoding="utf-8")
    assert "Listen cluster.example.edu:18443" in text
    assert "ProxyPass / http://127.0.0.1:8000/" in text
    assert 'RequestHeader set X-Forwarded-Proto "https"' in text
    assert "SSLCertificateKeyFile" in text and "SSLProtocol -all +TLSv1.2 +TLSv1.3" in text
    assert "0.0.0.0" not in text and "*:18443" not in text
    assert "%U" in text and "%q" not in text and "%r" not in text
    assert "%{Cookie}" not in text and "%{Set-Cookie}" not in text
    assert "StrictHostKeyChecking=no" not in text
