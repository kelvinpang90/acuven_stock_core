"""deploy/monitor.sh 的场景测试（契约 0.20 §8.5，CORE-TASK-065、066）。

用 bash 跑脚本；PATH 最前面放假的 docker、timeout 与 logger，都把收到的参数记进假工具的目录：
- 假 docker 的 ps 按场景列出容器；exec 把交给容器里 Python 的代码换成本机 Python 跑，
  只带 -e 按名字传入的变量，容器内回环的地址换成本机回环上的假 Core
  （就绪检查与不变量自查按场景回应，自查照真实接口按令牌鉴权）；
- 假 timeout 按场景让某一步超时（退出码 124，不真等），其余照常执行那条命令，并记下每一步的限时；
- 假 logger 只记参数。
心跳服务也是本机回环上记录请求的假服务（可按路径回错）；复核秒数设为 0。状态文件经
CORE_MONITOR_STATE_FILE 指到临时目录，默认先写成两个检查的记录都为空。每次运行都核对输出、日志、
请求体、状态文件与假 docker、假 timeout 收到的参数里没有令牌与心跳地址。
"""

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "monitor.sh"
CRON = ROOT / "deploy" / "monitor.cron"
RUNBOOK = ROOT / "docs" / "OPS_MONITORING.md"

TOKEN = f"csk_monitor_{uuid4().hex}"
WRONG_TOKEN = f"csk_wrong_{uuid4().hex}"
CHECKS = ("readyz", "invariants")
URL_KEYS = {
    "readyz": "CORE_HEALTHCHECK_READYZ_URL",
    "invariants": "CORE_HEALTHCHECK_INVARIANTS_URL",
}
PERIODIC_KEY = "CORE_HEALTHCHECK_PERIODIC_URL"
TASKS = ("reservation-expiry", "security-event-purge")
CONTAINER = "c0ffee5eed01"
LOG_TAG = "stock_core_monitor"
OK_BODY = "ok\n"
STATE_HEADER = "stock_core_monitor state 1"
NOT_WRITTEN = "state file could not be written"


def state(readyz: str = "none", invariants: str = "none") -> str:
    """状态文件的内容：每个检查的记录为 none（空）、unknown（未知）或逗号分隔的维度:级别。"""
    return f"{STATE_HEADER}\nreadyz {readyz}\ninvariants {invariants}\n"


FAKE_DOCKER = r"""
import json
import os
import subprocess
import sys

state = os.environ["FAKE_STATE_DIR"]
with open(os.path.join(state, "config.json")) as file:
    config = json.load(file)
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as file:
    file.write(json.dumps({"tool": "docker", "argv": args}) + "\n")
if args[:1] == ["ps"]:
    for container in config["containers"]:
        print(container)
    sys.exit(0)
if args[:1] != ["exec"]:
    sys.exit(2)
rest, env = args[1:], {}
while rest and rest[0].startswith("-"):
    if rest.pop(0) != "-e":
        sys.exit(125)
    name = rest.pop(0)
    if name in os.environ:
        env[name] = os.environ[name]
if len(rest) != 4 or rest[0] not in config["containers"] or rest[1:3] != ["python", "-c"]:
    sys.exit(126)
code = rest[3].replace("http://127.0.0.1:8000", config["core_url"])
sys.exit(subprocess.run([sys.executable, "-I", "-c", code], env=env).returncode)
"""

FAKE_TIMEOUT = r"""
import json
import os
import sys

state = os.environ["FAKE_STATE_DIR"]
with open(os.path.join(state, "config.json")) as file:
    config = json.load(file)
seconds, command = sys.argv[1], sys.argv[2:]
step = "other"
if command[:2] == ["docker", "ps"]:
    step = "ps"
elif command[:2] == ["docker", "exec"] and "/readyz" in command[-1]:
    step = "readyz"
elif command[:2] == ["docker", "exec"] and "/v1/ops/invariants" in command[-1]:
    step = "invariants"
with open(os.path.join(state, "calls.jsonl"), "a") as file:
    file.write(json.dumps({"tool": "timeout", "seconds": seconds, "step": step}) + "\n")
if step in config["timeouts"]:
    sys.exit(124)
os.execvp(command[0], command)
"""

FAKE_LOGGER = r"""
import json
import os
import sys

state = os.environ["FAKE_STATE_DIR"]
with open(os.path.join(state, "calls.jsonl"), "a") as file:
    file.write(json.dumps({"tool": "logger", "argv": sys.argv[1:]}) + "\n")
"""

Handle = Callable[[str, str, dict, bytes], tuple[int, bytes]]


class Handler(BaseHTTPRequestHandler):
    """把请求交给所属服务的 respond_with（见 serve）。"""

    def respond(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, payload = self.server.respond_with(
            self.command, self.path, dict(self.headers), body
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = respond

    def log_message(self, *args: object) -> None:
        pass


@contextmanager
def serve(handle: Handle) -> Iterator[str]:
    """在本机回环上起一个 HTTP 服务，返回它的地址；handle 收方法、路径、请求头与请求体。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.respond_with = handle
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def task(name: str, stale: bool) -> dict:
    return {
        "name": name,
        "alive": not stale,
        "last_success_at": "2026-11-21T08:00:00Z",
        "last_failure_at": None,
        "stale": stale,
    }


def readyz(status: int = 200, stale: tuple[str, ...] = (), periodic: bool = True) -> tuple:
    """就绪检查的回应：200，或 503（数据库不可达）；正文默认带 periodic。"""
    body: dict = {"status": "ok"}
    if status == 503:
        body = {"status": "unavailable", "reason": "database_unreachable"}
    if periodic:
        tasks = [task(name, name in stale) for name in TASKS]
        body["periodic"] = {"status": "stale" if stale else "ok", "tasks": tasks}
    return status, json.dumps(body).encode()


def invariants(violations: list[dict], count: int | None = None) -> tuple:
    """自查的回应：响应里至多 100 项，violation_count 默认是项数。"""
    total = len(violations) if count is None else count
    data = {
        "checked_at": "2026-11-21T08:00:00Z",
        "ok": total == 0,
        "violation_count": total,
        "violations": violations[:100],
    }
    return 200, json.dumps({"data": data, "request_id": "req_test"}).encode()


def error(status: int, code: str) -> tuple:
    body = {"error": {"code": code, "message": code, "retryable": False, "details": {}}}
    return status, json.dumps(body | {"request_id": "req_test"}).encode()


def balance_violation(check: str, n: int) -> dict:
    return {
        "check": check,
        "pool_id": f"pool_{n:03d}",
        "sku_id": f"sku_{n:03d}",
        "expected": 1,
        "actual": 2,
    }


def reservation_violation(check: str, n: int) -> dict:
    return {
        "check": check,
        "reservation_id": f"res_{n:03d}",
        "order_ref": f"ORDER-SECRET-{n}",
        "status": "committed",
        "line_refs": ["line-1"],
    }


class FakeCore:
    """容器里 core_api 的替身。每组回应按调用依次取，取到最后一个后一直用它。"""

    def __init__(self) -> None:
        self.readyz = [readyz()]
        self.invariants = [invariants([])]

    def handle(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, bytes]:
        if method == "GET" and path == "/readyz":
            return self.next(self.readyz)
        if method == "GET" and path == "/v1/ops/invariants":
            if headers.get("Authorization") != f"Bearer {TOKEN}":
                return error(401, "UNAUTHORIZED")
            return self.next(self.invariants)
        return error(404, "NOT_FOUND")

    @staticmethod
    def next(responses: list) -> tuple[int, bytes]:
        return responses.pop(0) if len(responses) > 1 else responses[0]


@dataclass
class Result:
    returncode: int
    output: str
    logs: list[str]
    calls: list[dict]
    pushes: dict[str, list[tuple[str, str]]]
    state: str | None

    def docker(self, command: str) -> list[list[str]]:
        calls = [call["argv"] for call in self.calls if call["tool"] == "docker"]
        return [argv for argv in calls if argv[0] == command]

    def execs(self, path: str) -> list[list[str]]:
        return [argv for argv in self.docker("exec") if path in argv[-1]]


class Monitor:
    def __init__(
        self,
        tmp_path: Path,
        core: FakeCore,
        core_url: str,
        heartbeat_url: str,
        pings: list,
        rejected: set[str],
    ):
        self.core = core
        self.core_url = core_url
        self.heartbeat_url = heartbeat_url
        self.pings = pings
        self.rejected = rejected
        self.bin = tmp_path / "bin"
        self.fakes = tmp_path / "fakes"
        self.home = tmp_path / "home"
        for directory in (self.bin, self.fakes, self.home):
            directory.mkdir()
        # 状态文件：默认经 CORE_MONITOR_STATE_FILE 指到这里，先写成两个检查的记录都为空。
        self.state_file = tmp_path / "records" / "state"
        self.state_file.parent.mkdir()
        self.state_file.write_text(state())
        self.state_override = True
        self.extra_env: dict[str, str] = {}
        fakes = {"docker": FAKE_DOCKER, "timeout": FAKE_TIMEOUT, "logger": FAKE_LOGGER}
        for name, source in fakes.items():
            fake = self.bin / name
            fake.write_text(f"#!{sys.executable} -I\n{source}")
            fake.chmod(0o755)
        self.env_file = tmp_path / "monitor.env"
        self.env_present = True
        self.env_text: str | None = None
        self.containers = [CONTAINER]
        self.timeouts: list[str] = []
        self.values: dict[str, str] = {"CORE_MONITOR_TOKEN": TOKEN}
        self.point_heartbeats_at(heartbeat_url)

    def point_heartbeats_at(self, base_url: str) -> None:
        self.urls = {check: f"{base_url}/ping/{uuid4()}" for check in CHECKS}
        self.values |= {URL_KEYS[check]: url for check, url in self.urls.items()}

    def reject(self, check: str, kind: str) -> None:
        """假心跳服务对这个检查的成功（ok）或失败（fail）那一推回 500。"""
        base = urlparse(self.urls[check]).path
        self.rejected.add(f"{base}/fail" if kind == "fail" else base)

    def secrets(self) -> list[str]:
        urls = [*self.urls.values()]
        urls += [value for value in self.values.values() if "://" in value]
        found = [TOKEN, WRONG_TOKEN, *self.values.values(), *urls]
        found += [urlparse(url).path.rsplit("/", 1)[-1] for url in urls]
        return [secret for secret in found if secret]

    def write_env(self, path: Path) -> None:
        text = self.env_text
        if text is None:
            text = "".join(f"{key}={value}\n" for key, value in self.values.items())
        path.write_text(text)

    def run(self, script: Path = SCRIPT, cwd: Path | None = None, override: bool = True) -> Result:
        config = {
            "containers": self.containers,
            "timeouts": self.timeouts,
            "core_url": self.core_url,
        }
        (self.fakes / "config.json").write_text(json.dumps(config))
        (self.fakes / "calls.jsonl").write_text("")
        self.pings.clear()
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE_STATE_DIR": str(self.fakes),
            "CORE_MONITOR_RECHECK_SECONDS": "0",
            **self.extra_env,
        }
        if self.state_override:
            env["CORE_MONITOR_STATE_FILE"] = str(self.state_file)
        if override:
            env["CORE_MONITOR_ENV_FILE"] = str(self.env_file)
            if self.env_present:
                self.write_env(self.env_file)
        completed = subprocess.run(
            ["bash", str(script)],
            cwd=cwd or self.home,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        lines = (self.fakes / "calls.jsonl").read_text().splitlines()
        calls = [json.loads(line) for line in lines]
        logs = []
        for call in calls:
            if call["tool"] == "logger":
                assert call["argv"][:3] == ["-t", LOG_TAG, "--"]
                logs.append(call["argv"][3])
        result = Result(
            completed.returncode,
            completed.stdout + completed.stderr,
            logs,
            calls,
            self.pushes(),
            self.state_file.read_text() if self.state_file.is_file() else None,
        )
        self.assert_clean(result)
        return result

    def pushes(self) -> dict[str, list[tuple[str, str]]]:
        """每个检查依次推到地址本身（ok）还是加 /fail（fail），与请求体；推到别的路径即失败。"""
        found: dict[str, list[tuple[str, str]]] = {}
        for method, path, body in self.pings:
            assert method == "POST"
            for check, url in self.urls.items():
                base = urlparse(url).path
                if path in (base, f"{base}/fail"):
                    kind = "fail" if path.endswith("/fail") else "ok"
                    found.setdefault(check, []).append((kind, body))
                    break
            else:
                pytest.fail(f"heartbeat to an unknown path {path}")
        return found

    def assert_clean(self, result: Result) -> None:
        texts = [result.output, *result.logs, result.state or ""]
        texts += [body for pushes in result.pushes.values() for _, body in pushes]
        texts += [json.dumps(call) for call in result.calls if call["tool"] != "logger"]
        for secret in self.secrets():
            for text in texts:
                assert secret not in text


@pytest.fixture
def monitor(tmp_path: Path) -> Iterator[Monitor]:
    core = FakeCore()
    pings: list[tuple[str, str, str]] = []
    rejected: set[str] = set()

    def record(method: str, path: str, headers: dict, body: bytes) -> tuple[int, bytes]:
        pings.append((method, path, body.decode()))
        return (500, b"error") if path in rejected else (200, b"OK")

    with serve(core.handle) as core_url, serve(record) as heartbeat_url:
        yield Monitor(tmp_path, core, core_url, heartbeat_url, pings, rejected)


def renotified(result: Result, check: str) -> bool:
    return f"re-notifying {check}: pushing success, then failure" in result.logs


def assert_ok(result: Result, *checks: str) -> None:
    for check in checks:
        assert result.pushes[check] == [("ok", OK_BODY)]


def assert_fail(result: Result, check: str, *lines: str, renotify: bool = False) -> None:
    """推到 /fail（重新通知时先推一次成功），请求体正好是这几行，每行也写进了系统日志。"""
    pushes = result.pushes[check]
    assert [kind for kind, _ in pushes] == (["ok", "fail"] if renotify else ["fail"])
    assert pushes[0][1] == OK_BODY or not renotify
    assert pushes[-1][1].splitlines() == list(lines)
    assert renotified(result, check) == renotify
    for line in lines:
        assert line in result.logs


def problem_logs(result: Result) -> list[str]:
    return [line for line in result.logs if re.match(r"P[12] ", line)]


def rounds(result: Result) -> int:
    return len(result.docker("ps"))


# 场景


def test_all_green(monitor: Monitor) -> None:
    result = monitor.run()
    assert result.returncode == 0
    assert_ok(result, *CHECKS)
    assert result.logs == []
    assert rounds(result) == 1
    assert result.state == state()

    (ps,) = result.docker("ps")
    for label in (
        "label=com.docker.compose.project=acuven_stock_core",
        "label=com.docker.compose.service=core_api",
        "label=com.docker.compose.oneoff=False",
    ):
        assert ps[ps.index(label) - 1] == "--filter"
    # 令牌只按名字传入；就绪检查不带它。
    (ready,) = result.execs("/readyz")
    assert ready[:4] == ["exec", CONTAINER, "python", "-c"]
    (check,) = result.execs("/v1/ops/invariants")
    assert check[:6] == ["exec", "-e", "CORE_MONITOR_TOKEN", CONTAINER, "python", "-c"]
    # 各步限时：外层由 timeout 给，容器里的请求另有自己的限时。
    limits = {call["step"]: call["seconds"] for call in result.calls if call["tool"] == "timeout"}
    assert limits == {"ps": "10", "readyz": "15", "invariants": "60"}
    assert '"/readyz", 10,' in ready[-1]
    assert '"/v1/ops/invariants", 45,' in check[-1]


@pytest.mark.parametrize("containers", [[], [CONTAINER, "c0ffee5eed02"]])
def test_container_count_is_not_one(monitor: Monitor, containers: list[str]) -> None:
    monitor.containers = containers
    result = monitor.run()
    assert result.returncode == 1
    count = len(containers)
    reason = f"not checked: found {count} running core_api containers, expected exactly 1"
    assert_fail(result, "readyz", f"P1 readyz: {reason}", f"P2 periodic: {reason}")
    assert_fail(result, "invariants", f"P2 invariants: {reason}")
    assert rounds(result) == 2
    assert result.docker("exec") == []
    assert result.state == state("readyz:P1,periodic:P2", "invariants:P2")


def test_finding_the_container_times_out(monitor: Monitor) -> None:
    monitor.timeouts = ["ps"]
    result = monitor.run()
    assert result.returncode == 1
    reason = "not checked: timed out finding the core_api container"
    assert_fail(result, "readyz", f"P1 readyz: {reason}", f"P2 periodic: {reason}")
    assert_fail(result, "invariants", f"P2 invariants: {reason}")
    assert result.state == state("readyz:P1,periodic:P2", "invariants:P2")


def test_readyz_times_out(monitor: Monitor) -> None:
    monitor.timeouts = ["readyz"]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(
        result,
        "readyz",
        "P1 readyz: not checked: timed out",
        "P2 periodic: not checked: timed out",
    )
    assert_ok(result, "invariants")
    assert rounds(result) == 2
    assert result.state == state("readyz:P1,periodic:P2")


def test_readyz_503_still_reads_periodic(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(503)]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P1 readyz: /readyz returned HTTP 503")
    assert_ok(result, "invariants")
    assert result.state == state("readyz:P1")


def test_readyz_without_periodic(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(periodic=False)]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P2 periodic: /readyz body has no periodic")
    assert_ok(result, "invariants")
    assert result.state == state("periodic:P2")


def test_periodic_stale_fails_the_readyz_check(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(stale=("security-event-purge",))]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P2 periodic: stale tasks: security-event-purge")
    assert_ok(result, "invariants")
    assert result.state == state("periodic:P2")


def test_both_readyz_dimensions_fail_in_one_round(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(503, stale=("reservation-expiry",))]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(
        result,
        "readyz",
        "P1 readyz: /readyz returned HTTP 503",
        "P2 periodic: stale tasks: reservation-expiry",
    )
    assert_ok(result, "invariants")
    assert result.state == state("readyz:P1,periodic:P2")


def test_recheck_leaves_only_one_dimension(monitor: Monitor) -> None:
    stale = ("security-event-purge",)
    monitor.core.readyz = [readyz(503, stale=stale), readyz(stale=stale)]
    result = monitor.run()
    assert result.returncode == 1
    assert rounds(result) == 2
    assert_fail(result, "readyz", "P2 periodic: stale tasks: security-event-purge")
    assert "P1 readyz: /readyz returned HTTP 503" not in result.logs
    assert result.state == state("periodic:P2")


def test_invariant_violations(monitor: Monitor) -> None:
    violations = [balance_violation("ledger_totals", n) for n in (1, 2, 3)]
    violations += [balance_violation("reserved_vs_orders", n) for n in (4, 5)]
    violations += [reservation_violation("order_commit_state", n) for n in (6, 7)]
    monitor.core.invariants = [invariants(violations)]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz")
    assert_fail(
        result,
        "invariants",
        "P1 invariants: violation_count=7",
        "P1 invariants: check ledger_totals: 3",
        "P1 invariants: check reserved_vs_orders: 2",
        "P1 invariants: check order_commit_state: 2",
        "P1 invariants: ledger_totals pool_id=pool_001 sku_id=sku_001",
        "P1 invariants: ledger_totals pool_id=pool_002 sku_id=sku_002",
        "P1 invariants: ledger_totals pool_id=pool_003 sku_id=sku_003",
        "P1 invariants: reserved_vs_orders pool_id=pool_004 sku_id=sku_004",
        "P1 invariants: reserved_vs_orders pool_id=pool_005 sku_id=sku_005",
    )
    assert result.state == state(invariants="invariants:P1")


def test_invariant_violations_show_reservation_ids_without_order_ref(monitor: Monitor) -> None:
    violations = [reservation_violation("merged_sku_lines", n) for n in (1, 2)]
    monitor.core.invariants = [invariants(violations)]
    result = monitor.run()
    assert_fail(
        result,
        "invariants",
        "P1 invariants: violation_count=2",
        "P1 invariants: check merged_sku_lines: 2",
        "P1 invariants: merged_sku_lines reservation_id=res_001",
        "P1 invariants: merged_sku_lines reservation_id=res_002",
    )
    assert "ORDER-SECRET" not in result.output + "".join(result.logs)


def test_more_than_100_violations(monitor: Monitor) -> None:
    violations = [balance_violation("ledger_totals", n) for n in range(1, 101)]
    monitor.core.invariants = [invariants(violations, count=250)]
    result = monitor.run()
    assert result.returncode == 1
    shown = [f"pool_id=pool_00{n} sku_id=sku_00{n}" for n in range(1, 6)]
    assert_fail(
        result,
        "invariants",
        "P1 invariants: violation_count=250 (counts by check cover only the first 100 returned)",
        "P1 invariants: check ledger_totals: 100",
        *(f"P1 invariants: ledger_totals {ids}" for ids in shown),
    )


@pytest.mark.parametrize(
    "response",
    [(200, b"not json"), (200, b'{"data": {"ok": "yes"}}'), (200, b"[]")],
    ids=["not-json", "wrong-shape", "not-an-object"],
)
def test_invariants_200_unreadable(monitor: Monitor, response: tuple) -> None:
    monitor.core.invariants = [response]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz")
    assert_fail(result, "invariants", "P2 invariants: not checked: response unreadable")


def test_invariants_401(monitor: Monitor) -> None:
    monitor.values["CORE_MONITOR_TOKEN"] = WRONG_TOKEN
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz")
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")
    # 首轮加复核：自查各请求一次。
    assert len(result.execs("/v1/ops/invariants")) == 2
    assert result.state == state(invariants="invariants:P2")


def test_invariants_503(monitor: Monitor) -> None:
    monitor.core.invariants = [error(503, "UNAVAILABLE")]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz")
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 503")


def test_invariants_time_out(monitor: Monitor) -> None:
    monitor.timeouts = ["invariants"]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz")
    assert_fail(result, "invariants", "P2 invariants: not checked: timed out")


def test_no_monitor_token(monitor: Monitor) -> None:
    del monitor.values["CORE_MONITOR_TOKEN"]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz")
    assert_fail(
        result, "invariants", "P2 invariants: not checked: CORE_MONITOR_TOKEN is not configured"
    )
    assert result.execs("/v1/ops/invariants") == []


def test_recheck_clears_a_first_round_failure(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(503), readyz()]
    monitor.core.invariants = [(200, b"not json"), invariants([])]
    result = monitor.run()
    assert result.returncode == 0
    assert_ok(result, *CHECKS)
    assert rounds(result) == 2
    assert problem_logs(result) == []


@pytest.mark.parametrize("missing", CHECKS)
def test_missing_heartbeat_url_notifies_nobody(monitor: Monitor, missing: str) -> None:
    del monitor.values[URL_KEYS[missing]]
    monitor.core.readyz = [readyz(stale=("security-event-purge",))]
    result = monitor.run()
    assert result.returncode == 1
    assert missing not in result.pushes
    (other,) = [check for check in CHECKS if check != missing]
    assert other in result.pushes
    (line,) = [line for line in result.logs if "will not notify anyone" in line]
    assert line.startswith(f"{URL_KEYS[missing]} ")
    assert "http" not in line
    # 没推的检查不改记录。
    expected = {"readyz": "periodic:P2", "invariants": "none"}
    expected[missing] = "none"
    assert result.state == state(**expected)


@pytest.mark.parametrize("containers", [[CONTAINER], []], ids=["green", "red"])
def test_unreachable_heartbeat_service_only_logs(monitor: Monitor, containers: list[str]) -> None:
    monitor.point_heartbeats_at(f"http://127.0.0.1:{unused_port()}")
    monitor.containers = containers
    result = monitor.run()
    assert result.returncode == (0 if containers else 1)
    assert result.pushes == {}
    for check in CHECKS:
        prefix = f"heartbeat for {check} not delivered"
        assert any(line.startswith(prefix) for line in result.logs)
    assert len(problem_logs(result)) == (0 if containers else 3)
    assert result.state == state()


def test_env_file_missing(monitor: Monitor) -> None:
    monitor.env_present = False
    result = monitor.run()
    assert result.returncode == 1
    assert result.pushes == {}
    assert result.execs("/v1/ops/invariants") == []
    assert "P2 invariants: not checked: CORE_MONITOR_TOKEN is not configured" in result.logs
    for key in URL_KEYS.values():
        assert any(line.startswith(f"{key} is not configured") for line in result.logs)
    assert result.state == state()


def test_default_env_is_the_checkout_root(monitor: Monitor, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    script = checkout / "deploy" / "monitor.sh"
    shutil.copyfile(SCRIPT, script)
    monitor.write_env(checkout / ".env")
    # 当前目录（cron 在运行账号的家目录里跑）与脚本所在目录里的 .env 都不该被读到。
    decoy = f"CORE_MONITOR_TOKEN={WRONG_TOKEN}\n"
    (monitor.home / ".env").write_text(decoy)
    (checkout / "deploy" / ".env").write_text(decoy)
    result = monitor.run(script=script, cwd=monitor.home, override=False)
    assert result.returncode == 0
    assert_ok(result, *CHECKS)


def test_env_values_with_quotes_whitespace_and_repeats(monitor: Monitor) -> None:
    urls = [monitor.values[URL_KEYS[check]] for check in CHECKS]
    monitor.env_text = "\n".join(
        [
            "# 巡检",
            f"CORE_MONITOR_TOKEN={WRONG_TOKEN}",
            "CORE_ENV=production",
            f'CORE_MONITOR_TOKEN="{TOKEN}"  ',
            f"CORE_HEALTHCHECK_READYZ_URL=  '{urls[0]}'",
            f"CORE_HEALTHCHECK_INVARIANTS_URL={urls[1]}\r",
        ]
    )
    result = monitor.run()
    assert result.returncode == 0
    assert_ok(result, *CHECKS)


def test_env_trailing_comment_is_part_of_the_value(monitor: Monitor) -> None:
    monitor.values["CORE_MONITOR_TOKEN"] = f"{TOKEN} # ops token"
    result = monitor.run()
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")


@pytest.mark.parametrize("stale", [(), ("security-event-purge",)], ids=["green", "stale"])
def test_periodic_url_in_env_is_not_read(monitor: Monitor, stale: tuple[str, ...]) -> None:
    # 0.20 取消的键留在 .env 里：不往它推（推到别的路径时 pushes() 即失败）。
    monitor.values[PERIODIC_KEY] = f"{monitor.heartbeat_url}/ping/{uuid4()}"
    monitor.core.readyz = [readyz(stale=stale)]
    result = monitor.run()
    assert result.returncode == (1 if stale else 0)
    assert set(result.pushes) == set(CHECKS)
    if stale:
        assert_fail(result, "readyz", "P2 periodic: stale tasks: security-event-purge")
    else:
        assert_ok(result, "readyz")
    assert_ok(result, "invariants")
    assert not any(PERIODIC_KEY in line for line in result.logs)


# 重新通知


def test_empty_record_pushes_only_the_failure(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(503)]
    monitor.values["CORE_MONITOR_TOKEN"] = WRONG_TOKEN
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P1 readyz: /readyz returned HTTP 503")
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")
    assert result.state == state("readyz:P1", "invariants:P2")


def test_new_dimension_renotifies(monitor: Monitor) -> None:
    # 安全事件清理 stale（P2）红着时 core_api 又出了 P1。
    monitor.state_file.write_text(state("periodic:P2"))
    monitor.core.readyz = [readyz(503, stale=("security-event-purge",))]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(
        result,
        "readyz",
        "P1 readyz: /readyz returned HTTP 503",
        "P2 periodic: stale tasks: security-event-purge",
        renotify=True,
    )
    assert_ok(result, "invariants")
    assert result.state == state("readyz:P1,periodic:P2")


def test_higher_level_renotifies(monitor: Monitor) -> None:
    monitor.state_file.write_text(state(invariants="invariants:P2"))
    monitor.core.invariants = [invariants([balance_violation("ledger_totals", 1)])]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz")
    assert_fail(
        result,
        "invariants",
        "P1 invariants: violation_count=1",
        "P1 invariants: check ledger_totals: 1",
        "P1 invariants: ledger_totals pool_id=pool_001 sku_id=sku_001",
        renotify=True,
    )
    assert result.state == state(invariants="invariants:P1")


def test_same_problems_persist_without_renotifying(monitor: Monitor) -> None:
    monitor.state_file.write_text(state("periodic:P2", "invariants:P2"))
    monitor.core.readyz = [readyz(stale=("reservation-expiry",))]
    monitor.values["CORE_MONITOR_TOKEN"] = WRONG_TOKEN
    for _ in range(2):
        result = monitor.run()
        assert result.returncode == 1
        assert_fail(result, "readyz", "P2 periodic: stale tasks: reservation-expiry")
        assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")
        assert result.state == state("periodic:P2", "invariants:P2")


def test_fewer_problems_push_only_the_failure(monitor: Monitor) -> None:
    monitor.state_file.write_text(state("readyz:P1,periodic:P2", "invariants:P1"))
    monitor.core.readyz = [readyz(stale=("security-event-purge",))]
    monitor.values["CORE_MONITOR_TOKEN"] = WRONG_TOKEN
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P2 periodic: stale tasks: security-event-purge")
    # 级别变低也不重新通知。
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")
    assert result.state == state("periodic:P2", "invariants:P2")


def test_all_green_clears_the_records(monitor: Monitor) -> None:
    monitor.state_file.write_text(state("readyz:P1,periodic:P2", "invariants:P1"))
    result = monitor.run()
    assert result.returncode == 0
    assert_ok(result, *CHECKS)
    assert result.state == state()


def test_renotify_across_rounds(monitor: Monitor) -> None:
    stale = ("security-event-purge",)
    stale_line = "P2 periodic: stale tasks: security-event-purge"

    monitor.core.readyz = [readyz(stale=stale)]
    result = monitor.run()
    assert_fail(result, "readyz", stale_line)
    assert result.state == state("periodic:P2")

    monitor.core.readyz = [readyz(503, stale=stale)]
    result = monitor.run()
    assert_fail(result, "readyz", "P1 readyz: /readyz returned HTTP 503", stale_line, renotify=True)
    assert result.state == state("readyz:P1,periodic:P2")

    result = monitor.run()
    assert_fail(result, "readyz", "P1 readyz: /readyz returned HTTP 503", stale_line)

    monitor.core.readyz = [readyz()]
    result = monitor.run()
    assert result.returncode == 0
    assert_ok(result, *CHECKS)
    assert result.state == state()


# 状态文件


def test_state_file_missing(monitor: Monitor) -> None:
    monitor.state_file.unlink()
    monitor.core.readyz = [readyz(stale=("reservation-expiry",))]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P2 periodic: stale tasks: reservation-expiry", renotify=True)
    assert_ok(result, "invariants")
    assert result.state == state("periodic:P2")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "garbage\n",
        "\x00\xff\n",
        "stock_core_monitor state 2\nreadyz none\ninvariants none\n",
        f"{STATE_HEADER}\nreadyz none\ninvariants none\nperiodic none\n",
        f"{STATE_HEADER}\n\nreadyz none\ninvariants none\n",
    ],
    ids=["empty", "garbage", "binary", "other-version", "unknown-line", "blank-line"],
)
def test_state_file_corrupt(monitor: Monitor, text: str) -> None:
    monitor.state_file.write_text(text, encoding="latin-1")
    monitor.core.readyz = [readyz(stale=("reservation-expiry",))]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P2 periodic: stale tasks: reservation-expiry", renotify=True)
    assert_ok(result, "invariants")
    assert result.state == state("periodic:P2")


@pytest.mark.parametrize(
    "readyz_line",
    [
        "readyz readyz:P3",
        "readyz invariants:P2",
        "readyz periodic:P2,periodic:P2",
        "readyz periodic:P2,",
        "readyz unknown",
        "readyz none\nreadyz none",
        None,
    ],
    ids=["bad-level", "other-check", "repeated", "trailing-comma", "unknown", "twice", "missing"],
)
def test_one_entry_unreadable(monitor: Monitor, readyz_line: str | None) -> None:
    lines = [STATE_HEADER, *([readyz_line] if readyz_line else []), "invariants none"]
    monitor.state_file.write_text("\n".join(lines) + "\n")
    monitor.core.readyz = [readyz(stale=("reservation-expiry",))]
    monitor.values["CORE_MONITOR_TOKEN"] = WRONG_TOKEN
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P2 periodic: stale tasks: reservation-expiry", renotify=True)
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")
    assert result.state == state("periodic:P2", "invariants:P2")


@pytest.mark.parametrize("failure", ["rejected", "unreachable"])
def test_undelivered_failure_keeps_the_record(monitor: Monitor, failure: str) -> None:
    monitor.state_file.write_text(state("periodic:P2"))
    monitor.core.readyz = [readyz(503, stale=("security-event-purge",))]
    lines = (
        "P1 readyz: /readyz returned HTTP 503",
        "P2 periodic: stale tasks: security-event-purge",
    )
    if failure == "rejected":
        monitor.reject("readyz", "fail")
    else:
        monitor.point_heartbeats_at(f"http://127.0.0.1:{unused_port()}")
    result = monitor.run()
    assert result.returncode == 1
    if failure == "rejected":
        assert_fail(result, "readyz", *lines, renotify=True)
        assert_ok(result, "invariants")
    else:
        assert result.pushes == {}
    assert any(
        line.startswith("heartbeat for readyz not delivered (failure") for line in result.logs
    )
    assert result.state == state("periodic:P2")

    # 下一轮送得到：照样重新通知，送到后才更新记录。
    monitor.rejected.clear()
    monitor.point_heartbeats_at(monitor.heartbeat_url)
    result = monitor.run()
    assert_fail(result, "readyz", *lines, renotify=True)
    assert result.state == state("readyz:P1,periodic:P2")


def test_undelivered_success_keeps_the_record(monitor: Monitor) -> None:
    monitor.state_file.write_text(state(invariants="invariants:P2"))
    monitor.reject("invariants", "ok")
    result = monitor.run()
    assert result.returncode == 0
    assert result.pushes["invariants"] == [("ok", OK_BODY)]
    assert result.state == state(invariants="invariants:P2")


@pytest.mark.parametrize("containers", [[CONTAINER], []], ids=["green", "red"])
def test_state_file_not_writable(monitor: Monitor, tmp_path: Path, containers: list[str]) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    monitor.state_file = blocker / "state"
    monitor.containers = containers
    result = monitor.run()
    assert result.returncode == (0 if containers else 1)
    # 读不出，所以不合格的检查先成功后失败；写不进去只在最后记一行。
    if containers:
        assert_ok(result, *CHECKS)
        assert len(result.logs) == 1
    else:
        reason = "not checked: found 0 running core_api containers, expected exactly 1"
        assert_fail(
            result, "readyz", f"P1 readyz: {reason}", f"P2 periodic: {reason}", renotify=True
        )
        assert_fail(result, "invariants", f"P2 invariants: {reason}", renotify=True)
    (line,) = [line for line in result.logs if line.startswith(NOT_WRITTEN)]
    assert line == result.logs[-1]
    assert result.state is None


def test_corrupt_state_and_one_failure_delivered(monitor: Monitor) -> None:
    monitor.state_file.write_text("garbage\n")
    monitor.core.readyz = [readyz(stale=("reservation-expiry",))]
    monitor.values["CORE_MONITOR_TOKEN"] = WRONG_TOKEN
    monitor.reject("invariants", "fail")
    stale_line = "P2 periodic: stale tasks: reservation-expiry"
    token_line = "P2 invariants: not checked: HTTP 401"
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", stale_line, renotify=True)
    assert_fail(result, "invariants", token_line, renotify=True)
    assert result.state == state("periodic:P2", "unknown")

    # 记为未知的检查下一轮照样按读不出处理。
    monitor.rejected.clear()
    result = monitor.run()
    assert_fail(result, "readyz", stale_line)
    assert_fail(result, "invariants", token_line, renotify=True)
    assert result.state == state("periodic:P2", "invariants:P2")


@pytest.mark.parametrize("xdg", [True, False], ids=["xdg-state-home", "home"])
def test_default_state_location(monitor: Monitor, tmp_path: Path, xdg: bool) -> None:
    monitor.state_override = False
    if xdg:
        monitor.extra_env["XDG_STATE_HOME"] = str(tmp_path / "xdg")
        base = tmp_path / "xdg"
    else:
        base = monitor.home / ".local" / "state"
    monitor.state_file = base / "stock_core_monitor" / "state"
    monitor.core.readyz = [readyz(stale=("reservation-expiry",))]
    result = monitor.run()
    assert result.returncode == 1
    # 第一次跑时还没有状态文件：先成功后失败。
    assert_fail(result, "readyz", "P2 periodic: stale tasks: reservation-expiry", renotify=True)
    assert result.state == state("periodic:P2")
    directory = monitor.state_file.parent
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(monitor.state_file.stat().st_mode) == 0o600
    assert sorted(path.name for path in directory.iterdir()) == ["state"]
    assert not (ROOT / "stock_core_monitor").exists()

    result = monitor.run()
    assert_fail(result, "readyz", "P2 periodic: stale tasks: reservation-expiry")


# 静态检查


def test_shellcheck() -> None:
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "shellcheck is not installed"
    checked = subprocess.run([shellcheck, str(SCRIPT)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_cron_file() -> None:
    text = CRON.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "flock" not in text
    entries = [
        line.split() for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert entries == [
        ["*/5", "*", "*", "*", "*", "<RUN_AS_USER>", "bash", "<CHECKOUT_DIR>/deploy/monitor.sh"]
    ]
    assert "只装在生产实例的检出上" in text


def test_runbook_names_two_production_checks_and_three_keys() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert set(re.findall(r"stock_core production (\w+)", text)) == {"readyz", "invariants"}
    assert not re.search(r"stock_core demo \w", text)
    assert set(re.findall(r"CORE_HEALTHCHECK_\w+", text)) == set(URL_KEYS.values())
    env_section = text.split("## 3.", 1)[1].split("## 4.", 1)[0]
    env_lines = re.findall(r"^(CORE_\w+)=", env_section, re.MULTILINE)
    assert env_lines == ["CORE_MONITOR_TOKEN", *URL_KEYS.values()]
    assert "CORE_MONITOR_STATE_FILE" in text
    assert "CORE-TASK-067" in text and "068" in text


@pytest.mark.parametrize("path", [CRON, RUNBOOK], ids=["cron", "runbook"])
def test_no_hosts_addresses_or_checkout_directory(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "://" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert not re.search(r"[\w-]\.(com|io|net|org|cn|dev|internal|lan)\b", text, re.IGNORECASE)
    for prefix in ("/home/", "/srv/", "/opt/", "/root/", "/var/www", "/data/", "/usr/local/"):
        assert prefix not in text
