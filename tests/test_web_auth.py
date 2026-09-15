"""M10-B1 Web/SSH binding tests; all authentication transports are fake."""

from dataclasses import replace
import logging
import os
import pwd
import re

from fastapi.testclient import TestClient
import pytest

from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.ssh_poc import IdentityProbeResult, SSHProbeError
from sbatch_agent.web import WebConfig, create_app
from sbatch_agent.web_sessions import COOKIE_NAME, SessionManager


TEST_SECRET = 'M10B1_TEST_SECRET_DO_NOT_LOG'


class FakeContext:
    def __init__(self, username, uid):
        self.identity = IdentityProbeResult(
            username, uid, uid, (username,), '/home/' + username,
            '/home/' + username, 'example-cluster',
        )
        self.connected = True
        self.closed = False
        self.verify_calls = 0
        self.mismatch = False

    def verify_identity(self):
        if not self.connected:
            raise SSHProbeError('SSH_CONTEXT_CLOSED')
        self.verify_calls += 1
        if self.mismatch:
            return IdentityProbeResult(
                'other_user', 9999, 9999, ('other_user',), '/home/other_user',
                '/home/other_user', 'example-cluster',
            )
        return self.identity

    def close(self):
        self.closed = True
        self.connected = False


class FakeAuthenticator:
    def __init__(self):
        self.calls = []
        self.contexts = []
        self.failure = None

    def authenticate(self, credentials):
        # Inspect once and retain neither the model nor its credential value.
        assert credentials.password.get_secret_value() in {TEST_SECRET, 'valid-secret'}
        self.calls.append(credentials.username)
        if self.failure:
            raise SSHProbeError(self.failure)
        uid = 1001 if credentials.username == 'alice' else 1002
        context = FakeContext(credentials.username, uid)
        self.contexts.append(context)
        return context


@pytest.fixture
def auth_config(tmp_path):
    return WebConfig(
        tmp_path / 'data' / 'jobs.sqlite3', tmp_path / 'runs',
        authentication_enabled=True,
    )


@pytest.fixture
def fake_auth():
    return FakeAuthenticator()


def csrf(client, path='/login'):
    response = client.get(path)
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def login(client, username, *, password='valid-secret'):
    token = csrf(client)
    old_cookie = client.cookies.get(COOKIE_NAME)
    response = client.post(
        '/login', data={'csrf_token': token, 'username': username, 'password': password},
        follow_redirects=False,
    )
    return response, old_cookie


def create_auth_app(config, authenticator, *, manager=None):
    return create_app(
        config, profiles=StaticProfiles(), ssh_authenticator=authenticator,
        session_manager=manager,
    )


def test_app_enables_authentication_audit_info_level(auth_config, fake_auth):
    logger = logging.getLogger('sbatch_agent.authentication_audit')
    previous = logger.level
    try:
        logger.setLevel(logging.WARNING)
        create_auth_app(auth_config, fake_auth)
        assert logger.level == logging.INFO
    finally:
        logger.setLevel(previous)


def test_login_success_rotates_opaque_cookie_and_binds_server_context(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        csrf(client)
        old = client.cookies.get(COOKIE_NAME)
        response, _ = login(client, 'alice')
        new = client.cookies.get(COOKIE_NAME)
        assert response.status_code == 303 and response.headers['location'] == '/session'
        assert old != new and len(new) >= 43
        assert 'alice' not in new and 'valid-secret' not in new
        assert 'secure' not in response.headers.get('set-cookie', '').lower()
        page = client.get('/session')
        assert page.status_code == 200
        assert 'alice' in page.text and '/home/alice' in page.text
        record, status = app.state.session_manager.resolve(new, touch=False)
        assert status == 'active' and record.ssh_context is fake_auth.contexts[0]
        assert not hasattr(record, 'password') and 'valid-secret' not in repr(record)


@pytest.mark.parametrize('code,message', [
    ('SSH_AUTH_FAILED', '用户名或 SSH 密码不正确。'),
    ('SSH_CONNECTION_FAILED', '暂时无法连接集群，请稍后再试。'),
    ('SSH_HOST_KEY_FAILED', '无法验证集群服务器身份，请联系管理员。'),
    ('SSH_IDENTITY_MISMATCH', '集群身份校验失败，本次登录已终止。'),
])
def test_login_failure_is_safe_and_fail_closed(auth_config, fake_auth, code, message):
    fake_auth.failure = code
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response, old = login(client, 'bob', password=TEST_SECRET)
        assert response.status_code in {401, 403, 503}
        assert message in response.text
        assert TEST_SECRET not in response.text and 'Traceback' not in response.text
        assert client.cookies.get(COOKIE_NAME) == old
        record, _ = app.state.session_manager.resolve(old, touch=False)
        assert record is not None and not record.authenticated
        assert fake_auth.calls == ['bob']


def test_returned_identity_mismatch_closes_context_and_creates_no_session(auth_config):
    class WrongIdentityAuthenticator(FakeAuthenticator):
        def authenticate(self, credentials):
            self.calls.append(credentials.username)
            context = FakeContext('alice', 1001)
            self.contexts.append(context)
            return context
    fake = WrongIdentityAuthenticator()
    app = create_auth_app(auth_config, fake)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response, cookie = login(client, 'bob')
        assert response.status_code == 403
        assert '集群身份校验失败' in response.text and fake.contexts[0].closed
        record, _ = app.state.session_manager.resolve(cookie, touch=False)
        assert record is not None and not record.authenticated


def test_two_cookie_jars_alternate_without_identity_bleed(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with (TestClient(app, base_url='http://127.0.0.1') as a,
          TestClient(app, base_url='http://127.0.0.1') as b):
        assert login(a, 'alice')[0].status_code == 303
        assert login(b, 'bob')[0].status_code == 303
        for client, own, other in (
            (a, 'alice', 'bob'), (b, 'bob', 'alice'),
            (a, 'alice', 'bob'), (b, 'bob', 'alice'),
        ):
            page = client.get('/session')
            assert page.status_code == 200
            assert own in page.text and ('/home/' + other) not in page.text
        assert fake_auth.calls == ['alice', 'bob']


def test_authenticated_login_post_cannot_replace_bound_context(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        assert login(client, 'alice')[0].status_code == 303
        original_cookie = client.cookies.get(COOKIE_NAME)
        response = client.post(
            '/login',
            data={'username': 'bob', 'password': TEST_SECRET},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers['location'] == '/session'
        assert client.cookies.get(COOKIE_NAME) == original_cookie
        assert fake_auth.calls == ['alice']
        page = client.get('/session')
        assert '/home/alice' in page.text and '/home/bob' not in page.text


def test_cookie_tampering_and_process_restart_fail_closed(auth_config, fake_auth):
    first = create_auth_app(auth_config, fake_auth)
    with TestClient(first, base_url='http://127.0.0.1') as client:
        login(client, 'alice')
        valid = client.cookies.get(COOKIE_NAME)
        client.cookies.set(COOKIE_NAME, valid[:-1] + ('A' if valid[-1] != 'A' else 'B'))
        response = client.get('/session', follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/login'
    second = create_auth_app(auth_config, FakeAuthenticator())
    with TestClient(second, base_url='http://127.0.0.1') as restarted:
        restarted.cookies.set(COOKIE_NAME, valid)
        response = restarted.get('/session', follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/login'


def test_verify_rejects_username_field_and_never_switches_identity(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, 'bob')
        token = csrf(client, '/session')
        rejected = client.post(
            '/session/verify', data={'csrf_token': token, 'username': 'alice'},
            follow_redirects=False,
        )
        assert rejected.status_code == 400
        assert fake_auth.contexts[0].verify_calls == 0
        assert '/home/bob' in client.get('/session').text
        accepted = client.post(
            '/session/verify', data={'csrf_token': csrf(client, '/session')},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert fake_auth.contexts[0].verify_calls == 1
        assert '/home/bob' in client.get('/session?username=alice').text


def test_verify_identity_mismatch_invalidates_bound_session(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, 'bob')
        fake_auth.contexts[0].mismatch = True
        response = client.post(
            '/session/verify', data={'csrf_token': csrf(client, '/session')},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers['location'] == '/login?reason=disconnected'
        assert fake_auth.contexts[0].closed


def test_logout_csrf_and_isolation(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with (TestClient(app, base_url='http://127.0.0.1') as a,
          TestClient(app, base_url='http://127.0.0.1') as b):
        login(a, 'alice')
        login(b, 'bob')
        rejected = a.post('/logout', data={'csrf_token': 'wrong'}, follow_redirects=False)
        assert rejected.status_code == 403 and not fake_auth.contexts[0].closed
        response = a.post(
            '/logout', data={'csrf_token': csrf(a, '/session')}, follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers['location'] == '/login'
        assert fake_auth.contexts[0].closed
        assert a.get('/session', follow_redirects=False).headers['location'] == '/login'
        assert '/home/bob' in b.get('/session').text
        assert not fake_auth.contexts[1].closed


def test_idle_timeout_closes_only_expired_context(auth_config, fake_auth):
    now = [100.0]
    manager = SessionManager(idle_timeout_seconds=60, clock=lambda: now[0])
    app = create_auth_app(auth_config, fake_auth, manager=manager)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, 'alice')
        now[0] += 61
        response = client.get('/session', follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/login?reason=expired'
        assert fake_auth.contexts[0].closed


def test_disconnected_context_invalidates_session(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, 'alice')
        fake_auth.contexts[0].connected = False
        response = client.get('/session', follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/login?reason=disconnected'
        assert fake_auth.contexts[0].closed


def test_shutdown_closes_all_bound_contexts(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, 'alice')
        assert not fake_auth.contexts[0].closed
    assert fake_auth.contexts[0].closed and app.state.session_manager.active_count == 0


def test_secret_absent_from_response_logs_session_and_cookie(auth_config, fake_auth, caplog):
    caplog.set_level(logging.INFO)
    fake_auth.failure = 'SSH_AUTH_FAILED'
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response, cookie = login(client, 'bob', password=TEST_SECRET)
        record, _ = app.state.session_manager.resolve(cookie, touch=False)
        combined = response.text + caplog.text + repr(record) + repr(app.state.session_manager)
        assert TEST_SECRET not in combined
        assert TEST_SECRET not in response.headers.get('set-cookie', '')


def test_non_process_user_is_blocked_from_all_legacy_routes(auth_config, fake_auth):
    process_user = pwd.getpwuid(os.geteuid()).pw_name
    other_user = 'bob' if process_user != 'bob' else 'alice'
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, other_user)
        for path in ('/', '/new', '/jobs', '/cluster', '/ui/folders'):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers['location'] == '/session?legacy=blocked'
        for path in ('/new', '/new/prepare', '/new/analyze', '/jobs/example/submit'):
            response = client.post(
                path, data={'csrf_token': csrf(client, '/session')}, follow_redirects=False,
            )
            assert response.status_code == 403
            assert '当前账号暂不可使用' in response.text


def test_process_owner_retains_legacy_routes(auth_config, fake_auth):
    process_user = pwd.getpwuid(os.geteuid()).pw_name
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, process_user)
        assert client.get('/new').status_code == 200
        assert client.get('/jobs').status_code == 200


def test_unauthenticated_redirect_has_no_open_next(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response = client.get(
            '/session?next=https://evil.example', follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers['location'] == '/login'
        page = client.get('/login?next=https://evil.example')
        assert 'evil.example' not in page.text


def test_cookie_security_flags_and_secure_deployment_option(auth_config, fake_auth):
    secure_config = replace(auth_config, session_cookie_secure=True)
    app = create_auth_app(secure_config, fake_auth)
    with TestClient(app, base_url='https://localhost') as client:
        header = client.get('/login').headers['set-cookie'].lower()
        assert 'httponly' in header and 'samesite=strict' in header
        assert 'path=/' in header and 'secure' in header


def test_deployment_target_and_authentication_configuration_validation(auth_config):
    assert replace(auth_config, ssh_host='other.example.edu', ssh_port=2222).ssh_port == 2222
    with pytest.raises(ValueError):
        replace(auth_config, ssh_host='bad host')
    with pytest.raises(ValueError):
        replace(auth_config, ssh_port=0)
    with pytest.raises(ValueError):
        replace(auth_config, session_idle_timeout_seconds=10)
    assert auth_config.ssh_host == 'cluster.example.edu' and auth_config.ssh_port == 22


def test_username_validation_happens_without_ssh_attempt(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        response, _ = login(client, 'user;id', password=TEST_SECRET)
        assert response.status_code == 401
        assert fake_auth.calls == []
        assert '用户名或 SSH 密码不正确。' in response.text


def test_login_csrf_and_cross_origin_are_rejected_before_auth(auth_config, fake_auth):
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        for data, headers in (
            ({'csrf_token': 'wrong', 'username': 'bob', 'password': TEST_SECRET}, {}),
            ({'csrf_token': csrf(client), 'username': 'bob', 'password': TEST_SECRET},
             {'Origin': 'https://evil.example'}),
        ):
            assert client.post('/login', data=data, headers=headers).status_code == 403
        assert fake_auth.calls == []


def test_audit_never_records_raw_cookie_or_password(auth_config, fake_auth, caplog):
    caplog.set_level(logging.INFO)
    app = create_auth_app(auth_config, fake_auth)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        login(client, 'alice', password=TEST_SECRET)
        raw_cookie = client.cookies.get(COOKIE_NAME)
        assert raw_cookie not in caplog.text and TEST_SECRET not in caplog.text
        assert 'LOGIN_SUCCESS' in caplog.text and 'alice' in caplog.text
