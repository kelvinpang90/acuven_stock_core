"""deploy/monitor.sh 的场景测试（契约 8.5，CORE-TASK-065）。

用 bash 跑脚本；PATH 最前面放假的 docker、timeout 与 logger，都把收到的参数记进状态目录：
- 假 docker 的 ps 按场景列出容器；exec 把交给容器里 Python 的代码换成本机 Python 跑，
  只带 -e 按名字传入的变量，容器内回环的地址换成本机回环上的假 Core
  （就绪检查与不变量自查按场景回应，自查照真实接口按令牌鉴权）；
- 假 timeout 按场景让某一步超时（退出码 124，不真等），其余照常执行那条命令，并记下每一步的限时；
- 假 logger 只记参数。
心跳服务也是本机回环上记录请求的假服务；复核秒数设为 0。每次运行都核对输出、日志、请求体与
假 docker、假 timeout 收到的参数里没有令牌与心跳地址。
"""

import json
import os
import re
import shutil
import socket
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
DIMENSIONS = ("readyz", "periodic", "invariants")
URL_KEYS = {
    "readyz": "CORE_HEALTHCHECK_READYZ_URL",
    "periodic": "CORE_HEALTHCHECK_PERIODIC_URL",
    "invariants": "CORE_HEALTHCHECK_INVARIANTS_URL",
}
TASKS = ("reservation-expiry", "security-event-purge")
CONTAINER = "c0ffee5eed01"
LOG_TAG = "stock_core_monitor"
OK_BODY = "ok\n"

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
    pushes: dict[str, tuple[str, str]]

    def docker(self, command: str) -> list[list[str]]:
        calls = [call["argv"] for call in self.calls if call["tool"] == "docker"]
        return [argv for argv in calls if argv[0] == command]

    def execs(self, path: str) -> list[list[str]]:
        return [argv for argv in self.docker("exec") if path in argv[-1]]


class Monitor:
    def __init__(self, tmp_path: Path, core: FakeCore, core_url: str, heartbeat_url: str, pings):
        self.core = core
        self.core_url = core_url
        self.pings = pings
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.home = tmp_path / "home"
        for directory in (self.bin, self.state, self.home):
            directory.mkdir()
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
        self.urls = {dimension: f"{base_url}/ping/{uuid4()}" for dimension in DIMENSIONS}
        self.values |= {URL_KEYS[dimension]: url for dimension, url in self.urls.items()}

    def secrets(self) -> list[str]:
        found = [TOKEN, WRONG_TOKEN, *self.values.values(), *self.urls.values()]
        found += [urlparse(url).path.rsplit("/", 1)[-1] for url in self.urls.values()]
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
        (self.state / "config.json").write_text(json.dumps(config))
        (self.state / "calls.jsonl").write_text("")
        self.pings.clear()
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE_STATE_DIR": str(self.state),
            "CORE_MONITOR_RECHECK_SECONDS": "0",
        }
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
        lines = (self.state / "calls.jsonl").read_text().splitlines()
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
        )
        self.assert_clean(result)
        return result

    def pushes(self) -> dict[str, tuple[str, str]]:
        """每个维度推到地址本身（ok）还是加 /fail（fail），与请求体；同一维度只推一次。"""
        found: dict[str, tuple[str, str]] = {}
        for method, path, body in self.pings:
            assert method == "POST"
            for dimension, url in self.urls.items():
                base = urlparse(url).path
                if path in (base, f"{base}/fail"):
                    assert dimension not in found, f"{dimension} pushed twice"
                    found[dimension] = ("fail" if path.endswith("/fail") else "ok", body)
                    break
            else:
                pytest.fail(f"heartbeat to an unknown path {path}")
        return found

    def assert_clean(self, result: Result) -> None:
        texts = [result.output, *result.logs, *(body for _, body in result.pushes.values())]
        texts += [json.dumps(call) for call in result.calls if call["tool"] != "logger"]
        for secret in self.secrets():
            for text in texts:
                assert secret not in text


@pytest.fixture
def monitor(tmp_path: Path) -> Iterator[Monitor]:
    core = FakeCore()
    pings: list[tuple[str, str, str]] = []

    def record(method: str, path: str, headers: dict, body: bytes) -> tuple[int, bytes]:
        pings.append((method, path, body.decode()))
        return 200, b"OK"

    with serve(core.handle) as core_url, serve(record) as heartbeat_url:
        yield Monitor(tmp_path, core, core_url, heartbeat_url, pings)


def assert_ok(result: Result, *dimensions: str) -> None:
    for dimension in dimensions:
        assert result.pushes[dimension] == ("ok", OK_BODY)


def assert_fail(result: Result, dimension: str, *lines: str) -> None:
    """推到 /fail，请求体正好是这几行，每行也写进了系统日志。"""
    kind, body = result.pushes[dimension]
    assert kind == "fail"
    assert body.splitlines() == list(lines)
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
    assert_ok(result, *DIMENSIONS)
    assert result.logs == []
    assert rounds(result) == 1

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
    assert_fail(result, "readyz", f"P1 readyz: {reason}")
    assert_fail(result, "periodic", f"P2 periodic: {reason}")
    assert_fail(result, "invariants", f"P2 invariants: {reason}")
    assert rounds(result) == 2
    assert result.docker("exec") == []


def test_finding_the_container_times_out(monitor: Monitor) -> None:
    monitor.timeouts = ["ps"]
    result = monitor.run()
    assert result.returncode == 1
    reason = "not checked: timed out finding the core_api container"
    assert_fail(result, "readyz", f"P1 readyz: {reason}")
    assert_fail(result, "periodic", f"P2 periodic: {reason}")
    assert_fail(result, "invariants", f"P2 invariants: {reason}")


def test_readyz_times_out(monitor: Monitor) -> None:
    monitor.timeouts = ["readyz"]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P1 readyz: not checked: timed out")
    assert_fail(result, "periodic", "P2 periodic: not checked: timed out")
    assert_ok(result, "invariants")
    assert rounds(result) == 2


def test_readyz_503_still_reads_periodic(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(503)]
    result = monitor.run()
    assert result.returncode == 1
    assert_fail(result, "readyz", "P1 readyz: /readyz returned HTTP 503")
    assert_ok(result, "periodic", "invariants")


def test_readyz_without_periodic(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(periodic=False)]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "invariants")
    assert_fail(result, "periodic", "P2 periodic: /readyz body has no periodic")


def test_periodic_stale_names_the_task(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(stale=("security-event-purge",))]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "invariants")
    assert_fail(result, "periodic", "P2 periodic: stale tasks: security-event-purge")


def test_invariant_violations(monitor: Monitor) -> None:
    violations = [balance_violation("ledger_totals", n) for n in (1, 2, 3)]
    violations += [balance_violation("reserved_vs_orders", n) for n in (4, 5)]
    violations += [reservation_violation("order_commit_state", n) for n in (6, 7)]
    monitor.core.invariants = [invariants(violations)]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "periodic")
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
    assert_ok(result, "readyz", "periodic")
    assert_fail(result, "invariants", "P2 invariants: not checked: response unreadable")


def test_invariants_401(monitor: Monitor) -> None:
    monitor.values["CORE_MONITOR_TOKEN"] = WRONG_TOKEN
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "periodic")
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")
    # 首轮加复核：自查各请求一次。
    assert len(result.execs("/v1/ops/invariants")) == 2


def test_invariants_503(monitor: Monitor) -> None:
    monitor.core.invariants = [error(503, "UNAVAILABLE")]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "periodic")
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 503")


def test_invariants_time_out(monitor: Monitor) -> None:
    monitor.timeouts = ["invariants"]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "periodic")
    assert_fail(result, "invariants", "P2 invariants: not checked: timed out")


def test_no_monitor_token(monitor: Monitor) -> None:
    del monitor.values["CORE_MONITOR_TOKEN"]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "periodic")
    assert_fail(
        result, "invariants", "P2 invariants: not checked: CORE_MONITOR_TOKEN is not configured"
    )
    assert result.execs("/v1/ops/invariants") == []


def test_recheck_clears_a_first_round_failure(monitor: Monitor) -> None:
    monitor.core.readyz = [readyz(503), readyz()]
    monitor.core.invariants = [(200, b"not json"), invariants([])]
    result = monitor.run()
    assert result.returncode == 0
    assert_ok(result, *DIMENSIONS)
    assert rounds(result) == 2
    assert problem_logs(result) == []


def test_missing_heartbeat_url_notifies_nobody(monitor: Monitor) -> None:
    del monitor.values["CORE_HEALTHCHECK_PERIODIC_URL"]
    result = monitor.run()
    assert result.returncode == 1
    assert_ok(result, "readyz", "invariants")
    assert "periodic" not in result.pushes
    (line,) = [line for line in result.logs if "will not notify anyone" in line]
    assert line.startswith("CORE_HEALTHCHECK_PERIODIC_URL ")
    assert "http" not in line


@pytest.mark.parametrize("containers", [[CONTAINER], []], ids=["green", "red"])
def test_unreachable_heartbeat_service_only_logs(monitor: Monitor, containers: list[str]) -> None:
    monitor.point_heartbeats_at(f"http://127.0.0.1:{unused_port()}")
    monitor.containers = containers
    result = monitor.run()
    assert result.returncode == (0 if containers else 1)
    assert result.pushes == {}
    for dimension in DIMENSIONS:
        prefix = f"heartbeat for {dimension} not delivered"
        assert any(line.startswith(prefix) for line in result.logs)
    assert len(problem_logs(result)) == (0 if containers else 3)


def test_env_file_missing(monitor: Monitor) -> None:
    monitor.env_present = False
    result = monitor.run()
    assert result.returncode == 1
    assert result.pushes == {}
    assert result.execs("/v1/ops/invariants") == []
    assert "P2 invariants: not checked: CORE_MONITOR_TOKEN is not configured" in result.logs
    for key in URL_KEYS.values():
        assert any(line.startswith(f"{key} is not configured") for line in result.logs)


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
    assert_ok(result, *DIMENSIONS)


def test_env_values_with_quotes_whitespace_and_repeats(monitor: Monitor) -> None:
    urls = [monitor.values[URL_KEYS[dimension]] for dimension in DIMENSIONS]
    monitor.env_text = "\n".join(
        [
            "# 巡检",
            f"CORE_MONITOR_TOKEN={WRONG_TOKEN}",
            "CORE_ENV=demo",
            f'CORE_MONITOR_TOKEN="{TOKEN}"  ',
            f"CORE_HEALTHCHECK_READYZ_URL='{urls[0]}'",
            f'CORE_HEALTHCHECK_PERIODIC_URL=  "{urls[1]}"',
            f"CORE_HEALTHCHECK_INVARIANTS_URL={urls[2]}\r",
        ]
    )
    result = monitor.run()
    assert result.returncode == 0
    assert_ok(result, *DIMENSIONS)


def test_env_trailing_comment_is_part_of_the_value(monitor: Monitor) -> None:
    monitor.values["CORE_MONITOR_TOKEN"] = f"{TOKEN} # ops token"
    result = monitor.run()
    assert_fail(result, "invariants", "P2 invariants: not checked: HTTP 401")


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


@pytest.mark.parametrize("path", [CRON, RUNBOOK], ids=["cron", "runbook"])
def test_no_hosts_addresses_or_checkout_directory(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "://" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert not re.search(r"[\w-]\.(com|io|net|org|cn|dev|internal|lan)\b", text, re.IGNORECASE)
    for prefix in ("/home/", "/srv/", "/opt/", "/root/", "/var/www", "/data/", "/usr/local/"):
        assert prefix not in text
