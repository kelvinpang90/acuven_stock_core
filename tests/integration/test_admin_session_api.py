"""后台管理员的登录、取当前会话与退出接口（docs/ADMIN_AUTH.md 第 5 节，A1–A8）。

在真 MySQL 上跑；接口挂在后台开关为真的生产应用上：照 test_production_routes，
在 instance_marked 里以 CORE_ENV=production 建好即退出。
测试客户端用 https 的 base URL（5.2）。
来源用 X-Real-IP 给随机的 10.x.y.z（A3 用文档保留网段 203.0.113.0/24），
用户名带随机后缀，各测试的登录尝试互不计数。
提交的数据在 finally 里按外键先后删掉（同 test_admin_auth_service），
会话结束时 migrated_engine 才能照常降到 base。
"""

import re
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.api import admin_session
from app.config import Settings
from app.domain.environment import PRODUCTION
from app.domain.login_throttle import normalize_source, source_key
from app.main import create_app
from app.services.admin_auth import csrf_token_for, disable_account, utc_now
from tests.integration.conftest import TEST_DATABASE_URL, instance_marked
from tests.integration.test_admin_auth_service import (
    PASSWORD,
    WRONG_PASSWORD,
    World,
    attempt_count,
    cleanup,
    last_attempt,
    new_username,
    resolve,
    sessions_of,
)
from tests.integration.test_api_auth import error_of
from tests.integration.test_reservations_api import make_client

SESSION = "/api/v1/admin/session"
COOKIE = "__Host-core_admin_session"
BASE_URL = "https://testserver"
# TestClient 的连接对端地址。
PEER = "testclient"
EXPIRES_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
COOKIE_ATTRIBUTES = {"httponly": "", "path": "/", "samesite": "Strict", "secure": ""}
USERNAME_MARKER = "kelvinsubmitted"
PASSWORD_MARKER = "password-marker-9f3c"


@pytest.fixture
def engine(migrated_engine: Engine) -> Engine:
    return migrated_engine


def production_app(engine: Engine, admin_enabled: bool):
    settings = Settings(
        env=PRODUCTION,
        git_sha="d" * 40,
        database_url=TEST_DATABASE_URL,
        admin_enabled=admin_enabled,
    )
    with instance_marked(engine, PRODUCTION):
        return create_app(settings)


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    app = production_app(engine, admin_enabled=True)
    try:
        yield TestClient(app, base_url=BASE_URL)
    finally:
        app.state.engine.dispose()


@pytest.fixture
def world(engine: Engine) -> Iterator[World]:
    built = World(engine)
    try:
        built.tenant_id = built.tenant()
        built.register(PEER)
        yield built
    finally:
        cleanup(engine, built)


# --- 请求与观察 ---


def login(
    client: TestClient,
    source: str | None,
    username: str,
    password: str = PASSWORD,
    *,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
):
    sent = {} if source is None else {"X-Real-IP": source}
    if cookie is not None:
        sent["Cookie"] = f"{COOKIE}={cookie}"
    sent |= headers or {}
    body = {"username": username, "password": password}
    response = client.post(SESSION, json=body, headers=sent)
    # 每次请求自己带 cookie，不靠客户端记下的。
    client.cookies.clear()
    return response


def with_cookie(token: str, **extra: str) -> dict[str, str]:
    return {"Cookie": f"{COOKIE}={token}"} | extra


def get_session(client: TestClient, token: str):
    return client.get(SESSION, headers=with_cookie(token))


def logout(client: TestClient, token: str, csrf: str | None = None):
    headers = with_cookie(token) if csrf is None else with_cookie(token, **{"X-CSRF-Token": csrf})
    response = client.delete(SESSION, headers=headers)
    client.cookies.clear()
    return response


def assert_no_store(response) -> None:
    assert response.headers.get_list("cache-control") == ["no-store"]


def detail_of(response, status: int) -> str:
    assert response.status_code == status, response.text
    assert_no_store(response)
    body = response.json()
    assert set(body) == {"detail"}
    return body["detail"]


def set_cookie(response) -> tuple[str, str, dict[str, str]]:
    """唯一一个 Set-Cookie 的名字、值与属性（属性名小写）。"""
    [header] = response.headers.get_list("set-cookie")
    first, *parts = [part.strip() for part in header.split(";")]
    name, _, value = first.partition("=")
    attributes = {}
    for part in parts:
        key, _, attribute = part.partition("=")
        attributes[key.lower()] = attribute
    return name, value, attributes


def logged_in(response) -> tuple[dict, str]:
    """200 的登录：返回响应体与 cookie 里的会话令牌。"""
    assert response.status_code == 200, response.text
    assert_no_store(response)
    name, token, attributes = set_cookie(response)
    assert name == COOKIE
    assert attributes == COOKIE_ATTRIBUTES | {"max-age": "43200"}
    return response.json(), token


def parse_expires_at(value: str) -> datetime:
    assert EXPIRES_AT.fullmatch(value)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def database_error(*args, **kwargs):
    raise OperationalError("SELECT 1", {}, Exception("simulated"))


# --- A1：登录成功 ---


def test_a1_login_sets_the_cookie_and_records_success(client, engine, world):
    account = world.account()
    source = world.source()
    before = utc_now()
    body, token = logged_in(login(client, source, account.username.upper()))

    assert set(body) == {"username", "expires_at", "csrf_token"}
    assert body["username"] == account.username
    assert body["csrf_token"] == csrf_token_for(token)
    expires_at = parse_expires_at(body["expires_at"])

    [session] = sessions_of(engine, account)
    assert session["expires_at"] == expires_at
    assert session["created_at"] >= before
    assert session["revoked_at"] is None
    attempt = last_attempt(engine)
    assert attempt["outcome"] == "success"
    assert attempt["admin_account_id"] == account.account_id
    assert attempt["source_key"] == source_key(normalize_source(source))

    # 拿 cookie 取当前会话：同样的三个字段。
    current = get_session(client, token)
    assert current.status_code == 200
    assert_no_store(current)
    assert current.json() == body


# --- A2：三种凭据失败不可区分 ---


def test_a2_the_three_credential_failures_are_identical(client, engine, world):
    account = world.account()
    disabled = world.account()
    with engine.connect() as conn:
        assert disable_account(conn, username=disabled.username, now=utc_now())

    responses = [
        login(client, world.source(), account.username, WRONG_PASSWORD),
        login(client, world.source(), new_username()),
        login(client, world.source(), disabled.username),
    ]
    for response in responses:
        assert detail_of(response, 401) == "invalid_credentials"
        assert "set-cookie" not in response.headers
    assert len({response.content for response in responses}) == 1
    assert sessions_of(engine, account) == []
    assert sessions_of(engine, disabled) == []


# --- A3：按来源与用户名锁定 ---


def test_a3_ten_failures_lock_the_source_and_another_source_logs_in(client, engine, world):
    account = world.account()
    locked_source = world.register("203.0.113.7")
    other_source = world.register("203.0.113.8")
    for _ in range(10):
        response = login(client, locked_source, account.username, WRONG_PASSWORD)
        assert detail_of(response, 401) == "invalid_credentials"

    response = login(client, locked_source, account.username)
    assert detail_of(response, 429) == "login_locked"
    assert last_attempt(engine)["outcome"] == "locked"
    assert sessions_of(engine, account) == []

    logged_in(login(client, other_source, account.username))
    assert len(sessions_of(engine, account)) == 1


# --- A4：退出要 CSRF 令牌 ---


def test_a4_logout_needs_the_csrf_token(client, engine, world):
    account = world.account()
    body, token = logged_in(login(client, world.source(), account.username))

    for csrf in (None, "", body["csrf_token"].upper(), csrf_token_for(token + "x")):
        assert detail_of(logout(client, token, csrf), 403) == "csrf_failed"
    assert get_session(client, token).status_code == 200

    response = logout(client, token, body["csrf_token"])
    assert response.status_code == 204
    assert_no_store(response)
    assert response.content == b""
    name, value, attributes = set_cookie(response)
    assert name == COOKIE
    assert value in ("", '""')
    assert attributes == COOKIE_ATTRIBUTES | {"max-age": "0"}

    assert detail_of(get_session(client, token), 401) == "not_authenticated"
    [session] = sessions_of(engine, account)
    assert session["revoked_at"] is not None
    # 已退出的会话再退出：401。
    assert detail_of(logout(client, token, body["csrf_token"]), 401) == "not_authenticated"


def test_get_and_delete_without_a_valid_cookie_are_401(client, world):
    for response in (
        client.get(SESSION),
        client.get(SESSION, headers=with_cookie("no-such-token")),
        client.delete(SESSION),
        client.delete(SESSION, headers=with_cookie("no-such-token", **{"X-CSRF-Token": "x"})),
    ):
        assert detail_of(response, 401) == "not_authenticated"


# --- A5：跨站与媒体类型 ---


def test_a5_cross_site_and_non_json_requests_are_rejected_without_attempts(client, engine, world):
    account = world.account()
    content = f'{{"username": "{account.username}", "password": "{PASSWORD}"}}'.encode()
    before = attempt_count(engine)

    plain = client.post(SESSION, content=content, headers={"Content-Type": "text/plain"})
    assert detail_of(plain, 415) == "unsupported_media_type"
    missing = client.post(SESSION, content=content)
    assert detail_of(missing, 415) == "unsupported_media_type"
    cross_site = {"Sec-Fetch-Site": "cross-site"}
    cross = login(client, world.source(), account.username, headers=cross_site)
    assert detail_of(cross, 403) == "cross_site_request"

    assert attempt_count(engine) == before
    assert sessions_of(engine, account) == []
    # 同站与媒体类型的大小写、参数都不影响。
    same_site = {
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "Application/JSON; charset=utf-8",
    }
    logged_in(login(client, world.source(), account.username, headers=same_site))


# --- A6：请求体 ---


def padded_body(
    size: int, username: str = USERNAME_MARKER, password: str = PASSWORD_MARKER
) -> bytes:
    """合格的用户名与密码，另用一个多出来的字段补到恰好 size 字节。"""
    prefix = f'{{"username": "{username}", "password": "{password}", "pad": "'.encode()
    suffix = b'"}'
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(padded_body(4097), id="4097-bytes"),
        pytest.param(iter([padded_body(4097)]), id="4097-bytes-without-content-length"),
        pytest.param(b"[" * 2000 + b"]" * 2000, id="nested-2000"),
        pytest.param(f'{{"username": "{USERNAME_MARKER}"}}'.encode(), id="missing-password"),
        pytest.param(f'{{"password": "{PASSWORD_MARKER}"}}'.encode(), id="missing-username"),
        pytest.param(f'{{"username": "{USERNAME_MARKER}", "password": '.encode(), id="not-json"),
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(
            f'{{"username": "{USERNAME_MARKER}", "password": 12345678901234}}'.encode(),
            id="password-not-a-string",
        ),
        pytest.param(
            f'{{"username": "", "password": "{PASSWORD_MARKER}"}}'.encode(), id="empty-username"
        ),
        pytest.param(
            f'{{"username": "{"k" * 65}", "password": "{PASSWORD_MARKER}"}}'.encode(),
            id="username-65",
        ),
        pytest.param(
            f'{{"username": "{USERNAME_MARKER}", "password": "{"p" * 257}"}}'.encode(),
            id="password-257",
        ),
        pytest.param(b'\xff{"username": "a", "password": "b"}', id="not-utf8"),
    ],
)
def test_a6_invalid_bodies_are_422_without_echo_or_attempts(client, engine, world, content):
    before = attempt_count(engine)
    response = client.post(
        SESSION,
        content=content,
        headers={"Content-Type": "application/json", "X-Real-IP": world.source()},
    )
    assert detail_of(response, 422) == "invalid_request"
    for marker in (USERNAME_MARKER, PASSWORD_MARKER, "k" * 65, "p" * 257):
        assert marker not in response.text
    assert attempt_count(engine) == before


def test_a_4096_byte_body_is_accepted(client, engine, world):
    account = world.account()
    for username, password, status in (
        (new_username(), PASSWORD, 401),
        (account.username, PASSWORD, 200),
    ):
        content = padded_body(4096, username, password)
        assert len(content) == 4096
        before = attempt_count(engine)
        response = client.post(
            SESSION,
            content=content,
            headers={"Content-Type": "application/json", "X-Real-IP": world.source()},
        )
        client.cookies.clear()
        assert response.status_code == status, response.text
        assert_no_store(response)
        assert attempt_count(engine) == before + 1


def test_extra_fields_are_ignored(client, world):
    account = world.account()
    response = client.post(
        SESSION,
        json={"username": account.username, "password": PASSWORD, "remember": True},
        headers={"X-Real-IP": world.source()},
    )
    logged_in(response)


# --- A7：带着旧会话再登录 ---


def test_a7_logging_in_again_revokes_the_previous_session(client, engine, world):
    account = world.account()
    _, old = logged_in(login(client, world.source(), account.username))
    _, new = logged_in(login(client, world.source(), account.username, cookie=old))
    assert new != old

    assert resolve(engine, old, utc_now()) is None
    assert resolve(engine, new, utc_now()) is not None
    assert detail_of(get_session(client, old), 401) == "not_authenticated"
    assert get_session(client, new).status_code == 200
    sessions = sessions_of(engine, account)
    assert len(sessions) == 2
    assert sessions[0]["revoked_at"] is not None
    assert sessions[1]["revoked_at"] is None


def test_a7_a_failing_revocation_does_not_change_the_answer(client, engine, world, monkeypatch):
    account = world.account()
    _, old = logged_in(login(client, world.source(), account.username))
    monkeypatch.setattr(admin_session, "_revoke_previous", database_error)
    _, new = logged_in(login(client, world.source(), account.username, cookie=old))
    # 旧会话留到自然到期，新会话照常有效。
    assert resolve(engine, old, utc_now()) is not None
    assert resolve(engine, new, utc_now()) is not None


# --- A8：进行中的登录满 10 个 ---


def test_a8_ten_logins_in_progress_is_503_login_busy(client, engine, world):
    account = world.account()
    before = attempt_count(engine)
    original = admin_session.logins_in_progress
    admin_session.logins_in_progress = 10
    try:
        response = login(client, world.source(), account.username)
        assert detail_of(response, 503) == "login_busy"
        assert "set-cookie" not in response.headers
        assert attempt_count(engine) == before
    finally:
        admin_session.logins_in_progress = original
    logged_in(login(client, world.source(), account.username))
    assert admin_session.logins_in_progress == original


# --- 来源地址（5.3） ---


def peer_key() -> str:
    return source_key(normalize_source(PEER))


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([("X-Real-IP", "203.0.113.20"), ("X-Real-IP", "203.0.113.21")], id="twice"),
        pytest.param([("X-Real-IP", "not-an-address")], id="invalid"),
        pytest.param([("X-Real-IP", "203.0.113.300")], id="out-of-range"),
        pytest.param([("X-Forwarded-For", "203.0.113.22")], id="forwarded-for-only"),
        pytest.param([], id="missing"),
    ],
)
def test_the_peer_address_is_used_when_x_real_ip_is_unusable(client, engine, world, headers):
    for name, value in headers:
        if name == "X-Real-IP":
            world.register(value)
    response = client.post(
        SESSION, json={"username": new_username(), "password": PASSWORD}, headers=headers
    )
    assert detail_of(response, 401) == "invalid_credentials"
    assert last_attempt(engine)["source_key"] == peer_key()


def test_x_forwarded_for_does_not_change_the_source(client, engine, world):
    source = world.source()
    world.register("203.0.113.23")
    headers = {"X-Real-IP": source, "X-Forwarded-For": "203.0.113.23"}
    response = client.post(
        SESSION, json={"username": new_username(), "password": PASSWORD}, headers=headers
    )
    assert detail_of(response, 401) == "invalid_credentials"
    assert last_attempt(engine)["source_key"] == source_key(normalize_source(source))


def test_an_ipv6_x_real_ip_is_used(client, engine, world):
    source = world.register("2001:db8:7:9::5")
    response = login(client, source, new_username())
    assert detail_of(response, 401) == "invalid_credentials"
    assert last_attempt(engine)["source_key"] == source_key(normalize_source(source))


# --- 数据库出错 ---


def test_a_database_error_during_login_is_503(client, engine, world, monkeypatch):
    account = world.account()
    monkeypatch.setattr(admin_session, "authenticate", database_error)
    response = login(client, world.source(), account.username)
    assert detail_of(response, 503) == "database_unreachable"
    assert "simulated" not in response.text
    assert "set-cookie" not in response.headers


def test_a_database_error_during_logout_is_503(client, engine, world, monkeypatch):
    account = world.account()
    body, token = logged_in(login(client, world.source(), account.username))
    monkeypatch.setattr(admin_session, "revoke_session", database_error)
    response = logout(client, token, body["csrf_token"])
    assert detail_of(response, 503) == "database_unreachable"
    assert "set-cookie" not in response.headers
    monkeypatch.undo()
    assert get_session(client, token).status_code == 200


def test_session_responses_carry_the_frame_headers(client):
    response = client.get(SESSION)
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["X-Frame-Options"] == "DENY"


# --- 后台开关为假与 Demo ---


def assert_paths_do_not_exist(client: TestClient) -> None:
    for method in ("POST", "GET", "DELETE"):
        response = client.request(
            method, SESSION, json={"username": "kelvin", "password": PASSWORD}
        )
        assert error_of(response, 404)["code"] == "NOT_FOUND", method


def test_the_paths_do_not_exist_when_the_admin_switch_is_off(engine):
    app = production_app(engine, admin_enabled=False)
    try:
        assert_paths_do_not_exist(TestClient(app, base_url=BASE_URL))
    finally:
        app.state.engine.dispose()


def test_the_paths_do_not_exist_on_the_demo_instance(engine):
    demo = make_client()
    try:
        assert_paths_do_not_exist(demo)
    finally:
        demo.app.state.engine.dispose()
