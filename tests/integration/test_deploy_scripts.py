"""deploy/instance.sh、deploy.sh、healthcheck.sh 与 docker-compose.yml 的场景测试（CORE-TASK-067，
docs/PRODUCTION_AND_RECOVERY.md 第 1.1、1.2 节）。

脚本与 docker-compose.yml 拷进临时目录里搭的假检出，.env 也写在那里。用 bash 跑脚本；
PATH 最前面放假的 docker、timeout 与 sleep，都把收到的参数记进假工具的目录：
- 假 docker 的 ps 按标签过滤场景里的容器、按 --format 打出 ID 与 working_dir 标签；
  network inspect 按场景的网络；compose 按子命令回应；
  exec 进 infra_mysql 时在本机用 sh 跑交给容器的脚本，环境里只有 root 密码，
  PATH 里只有假的 mysqldump（记下参数与是否经 MYSQL_PWD 拿到密码，按场景输出导出内容）；
- 假 timeout 记下限时，按场景让某一步超时（124，不真等），否则照常执行；假 sleep 只记一下。
主机锁用 ACUVEN_DEPLOY_LOCK 指到临时文件。
docker compose config 与 shellcheck 用真的，没有时测试失败。
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("instance.sh", "deploy.sh", "healthcheck.sh", "monitor.sh")
SHA = "0123456789abcdef0123456789abcdef01234567"
PASSWORD = f"root-{uuid4().hex}"
DB_URL = "mysql+pymysql://stock_core:pw@mysql:3306/stock_core_production"
DUMP = "-- MySQL dump\nCREATE TABLE t (id int);\n-- Dump completed on 2026-11-21  8:30:00\n"
BACKUP_NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{40}\.sql$")
MYSQLDUMP_ARGS = [
    "--user=root",
    "--single-transaction",
    "--no-tablespaces",
    "--set-gtid-purged=OFF",
    "--databases",
]
INSTANCES = {
    "demo": {
        "project": "acuven_stock_core",
        "data": "data_net",
        "callers": "stock_core_net",
        "alias": "core_api",
    },
    "production": {
        "project": "acuven_stock_core_production",
        "data": "infra_mysql_core_production_net",
        "callers": "stock_core_production_net",
        "alias": "stock_core_production",
    },
}
NETWORK_KEYS = ("data", "callers")
COMPOSE_VARS = ("CORE_DATA_NETWORK", "CORE_CALLER_NETWORK", "CORE_CALLER_ALIAS")

FAKE_DOCKER = r"""
import json
import os
import re
import subprocess
import sys

state = os.environ["FAKE_STATE_DIR"]
with open(os.path.join(state, "config.json")) as file:
    config = json.load(file)
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as file:
    file.write(json.dumps({"tool": "docker", "argv": args}) + "\n")


def ps(rest):
    if config["ps_exit"]:
        sys.exit(config["ps_exit"])
    wanted, template = [], None
    while rest:
        option = rest.pop(0)
        if option == "--filter" and rest[0].startswith("label=") and "=" in rest[0][6:]:
            wanted.append(rest.pop(0)[6:].split("=", 1))
        elif option == "--format":
            template = rest.pop(0)
        else:
            sys.exit(2)
    for container in config["containers"]:
        labels = container["labels"]
        if template and all(labels.get(key) == value for key, value in wanted):
            line = template.replace("{{.ID}}", container["id"])
            line = re.sub(r'\{\{\.Label "([^"]+)"\}\}', lambda m: labels.get(m[1], ""), line)
            print(line)
    sys.exit(0)


def compose(rest):
    if rest[:1] != ["-p"]:
        sys.exit(3)
    command = rest[2:]
    if command[:1] == ["ps"]:
        if "{{.Image}}" in command:
            print(config["previous_image"])
        elif "{{.Health}}" in command:
            print(config["health"])
        sys.exit(0)
    sys.exit(config["compose_exit"].get(command[0], 0))


def exec_(rest):
    if rest[0] == "infra_mysql":
        if config["mysql_exec_exit"]:
            print("-- partial output")
            sys.exit(config["mysql_exec_exit"])
        env = {
            "PATH": os.environ["FAKE_MYSQL_PATH"],
            "FAKE_STATE_DIR": state,
            "MYSQL_ROOT_PASSWORD": config["root_password"],
        }
        sys.exit(subprocess.run(rest[1:], env=env).returncode)
    if rest[0] in [container["id"] for container in config["containers"]]:
        print(config["healthz"])
        sys.exit(0)
    sys.exit(1)


if args[:1] == ["ps"]:
    ps(args[1:])
if args[:1] == ["compose"]:
    compose(args[1:])
if args[:1] == ["exec"]:
    exec_(args[1:])
if args[:2] == ["network", "inspect"]:
    sys.exit(0 if args[2] in config["networks"] else 1)
if args[:2] in (["network", "create"], ["image", "prune"]):
    sys.exit(0)
sys.exit(2)
"""

FAKE_TIMEOUT = r"""
import json
import os
import sys

state = os.environ["FAKE_STATE_DIR"]
with open(os.path.join(state, "config.json")) as file:
    config = json.load(file)
seconds, command = sys.argv[1], sys.argv[2:]
with open(os.path.join(state, "calls.jsonl"), "a") as file:
    file.write(json.dumps({"tool": "timeout", "seconds": seconds, "command": command[:2]}) + "\n")
if " ".join(command[:2]) in config["timeouts"]:
    sys.exit(124)
os.execvp(command[0], command)
"""

FAKE_SLEEP = r"""
import json
import os
import sys

with open(os.path.join(os.environ["FAKE_STATE_DIR"], "calls.jsonl"), "a") as file:
    file.write(json.dumps({"tool": "sleep", "argv": sys.argv[1:]}) + "\n")
"""

FAKE_MYSQLDUMP = r"""
import json
import os
import sys

state = os.environ["FAKE_STATE_DIR"]
with open(os.path.join(state, "config.json")) as file:
    config = json.load(file)
with open(os.path.join(state, "calls.jsonl"), "a") as file:
    call = {
        "tool": "mysqldump",
        "argv": sys.argv[1:],
        "mysql_pwd": os.environ.get("MYSQL_PWD") == config["root_password"],
    }
    file.write(json.dumps(call) + "\n")
sys.stdout.write(config["dump"])
sys.exit(config["dump_exit"])
"""


def container(container_id: str, working_dir: Path, project: str, oneoff: str = "False") -> dict:
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": "core_api",
        "com.docker.compose.oneoff": oneoff,
        "com.docker.compose.project.working_dir": str(working_dir),
    }
    return {"id": container_id, "labels": labels}


def make_checkout(path: Path, core_env: str | None = "CORE_ENV=demo\n") -> Path:
    """假检出：deploy/ 下四个脚本与 docker-compose.yml；core_env 为 None 时不写 .env。"""
    (path / "deploy").mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copyfile(ROOT / "deploy" / name, path / "deploy" / name)
    shutil.copyfile(ROOT / "docker-compose.yml", path / "docker-compose.yml")
    if core_env is not None:
        (path / ".env").write_text(core_env)
    return path


@dataclass
class Result:
    returncode: int
    output: str
    calls: list[dict]

    def docker(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls if call["tool"] == "docker"]

    def compose(self) -> list[list[str]]:
        return [argv for argv in self.docker() if argv[0] == "compose"]

    def steps(self) -> list[str]:
        """部署里会改东西的几步，按发生的顺序。"""
        found = []
        for argv in self.docker():
            if argv[0] == "compose" and argv[3] in ("pull", "run", "up"):
                found.append(argv[3])
            elif argv[:2] == ["exec", "infra_mysql"]:
                found.append("backup")
            elif argv[:2] == ["network", "create"]:
                found.append("network create")
            elif argv[:2] == ["image", "prune"]:
                found.append("prune")
        return found

    def tool(self, name: str) -> list[dict]:
        return [call for call in self.calls if call["tool"] == name]


class Fakes:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.mysql_bin = tmp_path / "mysql_bin"
        self.state = tmp_path / "fakes"
        self.home = tmp_path / "home"
        for directory in (self.bin, self.mysql_bin, self.state, self.home):
            directory.mkdir()
        sources = {
            self.bin / "docker": FAKE_DOCKER,
            self.bin / "timeout": FAKE_TIMEOUT,
            self.bin / "sleep": FAKE_SLEEP,
            self.mysql_bin / "mysqldump": FAKE_MYSQLDUMP,
        }
        for fake, source in sources.items():
            fake.write_text(f"#!{sys.executable} -I\n{source}")
            fake.chmod(0o755)
        self.lock = tmp_path / "lock" / "acuven-deploy.lock"
        self.lock.parent.mkdir()
        self.extra_env: dict[str, str] = {}
        self.config: dict = {
            "containers": [],
            "ps_exit": 0,
            "networks": [values[key] for values in INSTANCES.values() for key in NETWORK_KEYS],
            "previous_image": "",
            "health": "healthy",
            "compose_exit": {},
            "healthz": "",
            "root_password": PASSWORD,
            "mysql_exec_exit": 0,
            "dump": DUMP,
            "dump_exit": 0,
            "timeouts": [],
        }

    def run(self, argv: list[str], cwd: Path) -> Result:
        (self.state / "config.json").write_text(json.dumps(self.config))
        (self.state / "calls.jsonl").write_text("")
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE_STATE_DIR": str(self.state),
            "FAKE_MYSQL_PATH": f"{self.mysql_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "CORE_IMAGE_REPO": "ghcr.io/acuven/stock-core",
            "ACUVEN_DEPLOY_LOCK": str(self.lock),
            "CORE_HEALTH_TIMEOUT_SECONDS": "30",
            **self.extra_env,
        }
        completed = subprocess.run(
            ["bash", *argv], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
        )
        lines = (self.state / "calls.jsonl").read_text().splitlines()
        result = Result(
            completed.returncode,
            completed.stdout + completed.stderr,
            [json.loads(line) for line in lines],
        )
        assert PASSWORD not in result.output
        assert PASSWORD not in json.dumps(result.docker())
        return result

    def deploy(self, checkout: Path) -> Result:
        return self.run([str(checkout / "deploy" / "deploy.sh"), SHA], cwd=checkout)

    def healthcheck(self, checkout: Path) -> Result:
        return self.run([str(checkout / "deploy" / "healthcheck.sh"), SHA], cwd=checkout)

    def backup_dir(self) -> Path:
        return self.home / ".local" / "state" / "stock_core_production" / "pre_migration"


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


@pytest.fixture
def production(tmp_path: Path) -> Path:
    env = f"CORE_ENV=production\nCORE_DATABASE_URL={DB_URL}\n"
    return make_checkout(tmp_path / "production", env)


# instance.sh

SHOW_INSTANCE = """
set -euo pipefail
. "$1"
printf 'instance=%s\\n' "$CORE_INSTANCE"
printf 'project=%s\\n' "$CORE_COMPOSE_PROJECT"
printf 'data=%s\\n' "$(printenv CORE_DATA_NETWORK || true)"
printf 'callers=%s\\n' "$(printenv CORE_CALLER_NETWORK || true)"
printf 'alias=%s\\n' "$(printenv CORE_CALLER_ALIAS || true)"
printf 'checkout=%s\\n' "$CORE_CHECKOUT_DIR"
printf 'error=%s\\n' "$CORE_INSTANCE_ERROR"
"""

FIND = """
set -euo pipefail
. "$1"
status=0
ids="$(core_find_api_containers)" || status=$?
printf 'status=%s\\n' "$status"
printf '%s\\n' "$ids"
"""


def show_instance(fakes: Fakes, checkout: Path) -> dict[str, str]:
    # 当前目录里另放一份 .env：instance.sh 只认本文件所在检出的。
    elsewhere = fakes.tmp / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    (elsewhere / ".env").write_text("CORE_ENV=production\n")
    instance = str(checkout / "deploy" / "instance.sh")
    result = fakes.run(["-c", SHOW_INSTANCE, "bash", instance], cwd=elsewhere)
    assert result.returncode == 0, result.output
    assert result.docker() == []
    return dict(line.split("=", 1) for line in result.output.splitlines())


@pytest.mark.parametrize("name", INSTANCES)
def test_instance_values(fakes: Fakes, tmp_path: Path, name: str) -> None:
    checkout = make_checkout(tmp_path / "checkout", f"# comment\nCORE_ENV={name}\nOTHER=1")
    values = show_instance(fakes, checkout)
    expected = INSTANCES[name]
    assert values["instance"] == name
    assert values["project"] == expected["project"]
    assert values["data"] == expected["data"]
    assert values["callers"] == expected["callers"]
    assert values["alias"] == expected["alias"]
    assert values["checkout"] == str(checkout.resolve())
    assert values["error"] == ""


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "OTHER=1\n",
        "CORE_ENV=prod\n",
        "CORE_ENV=Demo\n",
        "CORE_ENV=\n",
        'CORE_ENV="production"\n',
        "CORE_ENV='demo'\n",
        "CORE_ENV= demo\n",
        "CORE_ENV=demo \n",
        " CORE_ENV=demo\n",
        "CORE_ENV=demo # instance\n",
        "CORE_ENV=demo\r\n",
        "CORE_ENV=demo\nCORE_ENV=demo\n",
        "CORE_ENV=demo\nCORE_ENV=production\n",
    ],
    ids=[
        "no-env-file",
        "empty",
        "missing",
        "misspelled",
        "uppercase",
        "empty-value",
        "double-quoted",
        "single-quoted",
        "leading-space",
        "trailing-space",
        "indented",
        "comment",
        "crlf",
        "twice",
        "two-values",
    ],
)
def test_instance_rejects(fakes: Fakes, tmp_path: Path, text: str | None) -> None:
    checkout = make_checkout(tmp_path / "checkout", text)
    # 调用方环境里留着的旧值不算数。
    fakes.extra_env = {"CORE_DATA_NETWORK": "data_net", "CORE_CALLER_ALIAS": "core_api"}
    values = show_instance(fakes, checkout)
    assert values["instance"] == values["project"] == ""
    assert values["data"] == values["callers"] == values["alias"] == ""
    assert "must be exactly one line CORE_ENV=demo or CORE_ENV=production" in values["error"]


def find(fakes: Fakes, checkout: Path) -> tuple[int, list[str]]:
    instance = str(checkout / "deploy" / "instance.sh")
    result = fakes.run(["-c", FIND, "bash", instance], cwd=fakes.home)
    assert result.returncode == 0, result.output
    first, *ids = result.output.splitlines()
    return int(first.removeprefix("status=")), [line for line in ids if line]


def test_find_only_this_checkout(fakes: Fakes, production: Path, tmp_path: Path) -> None:
    project = INSTANCES["production"]["project"]
    other = make_checkout(tmp_path / "other", "CORE_ENV=production\n")
    link = tmp_path / "link"
    link.symlink_to(production)
    fakes.config["containers"] = [
        container("aaaa00000001", other, project),
        container("aaaa00000002", production, project, oneoff="True"),
        container("aaaa00000003", production, "acuven_stock_core"),
        container("aaaa00000004", tmp_path / "gone", project),
        container("cafe00000001", link, project),
    ]
    assert find(fakes, production) == (0, ["cafe00000001"])
    (ps,) = [argv for argv in fakes_calls(fakes) if argv[0] == "ps"]
    for label in (
        f"label=com.docker.compose.project={project}",
        "label=com.docker.compose.service=core_api",
        "label=com.docker.compose.oneoff=False",
    ):
        assert ps[ps.index(label) - 1] == "--filter"
    (limit,) = [call for call in read_calls(fakes) if call["tool"] == "timeout"]
    assert limit == {"tool": "timeout", "seconds": "10", "command": ["docker", "ps"]}


def test_find_prints_every_match(fakes: Fakes, production: Path) -> None:
    project = INSTANCES["production"]["project"]
    fakes.config["containers"] = [
        container("cafe00000001", production, project),
        container("cafe00000002", production, project),
    ]
    assert find(fakes, production) == (0, ["cafe00000001", "cafe00000002"])


def test_find_failures_have_distinct_codes(fakes: Fakes, production: Path) -> None:
    fakes.config["ps_exit"] = 1
    assert find(fakes, production) == (1, [])
    fakes.config["ps_exit"] = 0
    fakes.config["timeouts"] = ["docker ps"]
    assert find(fakes, production) == (124, [])


def test_find_without_instance_does_not_call_docker(fakes: Fakes, tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout", "CORE_ENV=staging\n")
    assert find(fakes, checkout) == (2, [])
    assert fakes_calls(fakes) == []


def read_calls(fakes: Fakes) -> list[dict]:
    lines = (fakes.state / "calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def fakes_calls(fakes: Fakes) -> list[list[str]]:
    return [call["argv"] for call in read_calls(fakes) if call["tool"] == "docker"]


# deploy.sh


@pytest.mark.parametrize("text", [None, "CORE_ENV=prod\n", "CORE_ENV=demo # x\n"])
def test_deploy_bad_core_env_touches_nothing(
    fakes: Fakes, tmp_path: Path, text: str | None
) -> None:
    checkout = make_checkout(tmp_path / "checkout", text)
    result = fakes.deploy(checkout)
    assert result.returncode == 1
    assert "CORE_ENV" in result.output and "nothing has been changed" in result.output
    assert result.calls == []
    assert not fakes.lock.exists()


def test_deploy_demo(fakes: Fakes, tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    result = fakes.deploy(checkout)
    assert result.returncode == 0, result.output
    assert result.compose()
    for argv in result.compose():
        assert argv[:3] == ["compose", "-p", "acuven_stock_core"]
    assert result.steps() == ["pull", "run", "up", "prune"]
    assert ["network", "inspect", "data_net"] in result.docker()
    # Demo 不备份。
    assert result.tool("mysqldump") == []
    assert not (fakes.home / ".local" / "state" / "stock_core_production").exists()
    assert fakes.lock.read_text().startswith(f"stock_core demo {SHA} pid=")
    assert "--filter" in result.docker()[-1]
    assert "label=acuven.project=stock_core" in result.docker()[-1]


def test_deploy_production_backs_up_before_migrating(fakes: Fakes, production: Path) -> None:
    result = fakes.deploy(production)
    assert result.returncode == 0, result.output
    for argv in result.compose():
        assert argv[:3] == ["compose", "-p", "acuven_stock_core_production"]
    assert result.steps() == ["pull", "backup", "run", "up", "prune"]
    assert ["network", "inspect", "infra_mysql_core_production_net"] in result.docker()
    assert ["network", "inspect", "data_net"] not in result.docker()
    assert fakes.lock.read_text().startswith(f"stock_core production {SHA} pid=")

    # 密码只经 MYSQL_PWD 进 mysqldump：不在 docker 与 mysqldump 的参数里，不用 -p。
    (backup,) = [argv for argv in result.docker() if argv[:2] == ["exec", "infra_mysql"]]
    assert backup[2:4] == ["sh", "-c"] and backup[5:] == ["sh", "stock_core_production"]
    assert "MYSQL_PWD" in backup[4] and "--source-data" not in backup[4]
    (dump,) = result.tool("mysqldump")
    assert dump["argv"] == [*MYSQLDUMP_ARGS, "stock_core_production"]
    assert dump["mysql_pwd"] is True
    for argv in (backup, dump["argv"]):
        for arg in argv:
            assert not arg.startswith("-p") and not arg.startswith("--password")
            assert " -p" not in arg and "--password" not in arg
    timeouts = [call for call in result.tool("timeout") if call["command"] == ["docker", "exec"]]
    assert [call["seconds"] for call in timeouts] == ["60"]

    directory = fakes.backup_dir()
    (file,) = directory.iterdir()
    assert BACKUP_NAME.match(file.name) and file.name.endswith(f"-{SHA}.sql")
    assert file.read_text() == DUMP
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert not directory.is_relative_to(production)


@pytest.mark.parametrize("name", INSTANCES)
def test_deploy_creates_the_callers_network_only_when_missing(
    fakes: Fakes, tmp_path: Path, name: str
) -> None:
    env = f"CORE_ENV={name}\nCORE_DATABASE_URL={DB_URL}\n"
    checkout = make_checkout(tmp_path / "checkout", env)
    callers = INSTANCES[name]["callers"]
    fakes.config["networks"].remove(callers)
    result = fakes.deploy(checkout)
    assert result.returncode == 0, result.output
    assert ["network", "create", "--internal", callers] in result.docker()
    assert result.steps()[0] == "network create"

    fakes.config["networks"].append(callers)
    result = fakes.deploy(checkout)
    assert "network create" not in result.steps()


def test_deploy_production_without_its_data_network(fakes: Fakes, production: Path) -> None:
    fakes.config["networks"].remove("infra_mysql_core_production_net")
    result = fakes.deploy(production)
    assert result.returncode == 1
    assert "infra_mysql_core_production_net does not exist" in result.output
    assert result.steps() == []
    assert result.compose() == []
    assert not fakes.backup_dir().exists()


@pytest.mark.parametrize(
    "failure", ["incomplete", "mysqldump-fails", "exec-fails", "timeout", "no-password"]
)
def test_failed_backup_changes_nothing(fakes: Fakes, production: Path, failure: str) -> None:
    if failure == "incomplete":
        fakes.config["dump"] = DUMP.rsplit("-- Dump completed", 1)[0]
    elif failure == "mysqldump-fails":
        fakes.config["dump_exit"] = 2
    elif failure == "exec-fails":
        fakes.config["mysql_exec_exit"] = 1
    elif failure == "timeout":
        fakes.config["timeouts"] = ["docker exec"]
    else:
        fakes.config["root_password"] = ""
    result = fakes.deploy(production)
    assert result.returncode == 1
    assert "backup of stock_core_production failed" in result.output
    assert "nothing has been changed" in result.output
    # 超时由假 timeout 直接给出 124，docker exec 没有真的跑。
    assert result.steps() == (["pull"] if failure == "timeout" else ["pull", "backup"])
    assert [call["command"] for call in result.tool("timeout")] == [["docker", "exec"]]
    # 不留下临时文件，也没有正式文件。
    assert list(fakes.backup_dir().iterdir()) == []


def test_backup_keeps_the_latest_five(fakes: Fakes, production: Path) -> None:
    directory = fakes.backup_dir()
    directory.mkdir(parents=True)
    old = [f"2020010{day}T000000Z-{SHA}.sql" for day in range(1, 6)]
    others = [".pre_migration.abc123", "notes.txt", f"20200101T000000Z-{SHA[:7]}.sql"]
    for name in old + others:
        (directory / name).write_text("old\n")
    result = fakes.deploy(production)
    assert result.returncode == 0, result.output
    names = sorted(path.name for path in directory.iterdir())
    backups = [name for name in names if BACKUP_NAME.match(name)]
    assert len(backups) == 5
    assert old[0] not in names
    assert backups[:4] == old[1:]
    assert set(others) <= set(names)


@pytest.mark.parametrize("where", ["xdg", "override"])
def test_backup_location(fakes: Fakes, production: Path, tmp_path: Path, where: str) -> None:
    if where == "xdg":
        fakes.extra_env["XDG_STATE_HOME"] = str(tmp_path / "xdg")
        directory = tmp_path / "xdg" / "stock_core_production" / "pre_migration"
    else:
        fakes.extra_env["XDG_STATE_HOME"] = str(tmp_path / "xdg")
        fakes.extra_env["CORE_BACKUP_DIR"] = str(tmp_path / "backups")
        directory = tmp_path / "backups"
    result = fakes.deploy(production)
    assert result.returncode == 0, result.output
    (file,) = directory.iterdir()
    assert BACKUP_NAME.match(file.name)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert not fakes.backup_dir().exists()


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("mysql+pymysql://u:pw@mysql:3306/stock_core_production", "stock_core_production"),
        ("mysql+pymysql://u:pw@mysql/Core2?charset=utf8mb4", "Core2"),
        ("mysql+pymysql://u:pw@mysql:3306/core_db?ssl_ca=/etc/ssl/ca.pem&x=a/b", "core_db"),
        ("mysql+pymysql://u:pw@mysql:3306/core_db?next=?/x", "core_db"),
    ],
    ids=["plain", "query", "slash-in-query", "second-question-mark"],
)
def test_database_name_from_url(fakes: Fakes, tmp_path: Path, url: str, name: str) -> None:
    env = f"CORE_ENV=production\nCORE_DATABASE_URL={url}\n"
    checkout = make_checkout(tmp_path / "checkout", env)
    result = fakes.deploy(checkout)
    assert result.returncode == 0, result.output
    (dump,) = result.tool("mysqldump")
    assert dump["argv"] == [*MYSQLDUMP_ARGS, name]


@pytest.mark.parametrize(
    "line",
    [
        "CORE_DATABASE_URL=mysql+pymysql://u:pw@mysql:3306/stock-core\n",
        "CORE_DATABASE_URL=mysql+pymysql://u:pw@mysql:3306/\n",
        "CORE_DATABASE_URL=mysql+pymysql://u:pw@mysql:3306/core;drop\n",
        "CORE_DATABASE_URL=mysql+pymysql://u:pw@mysql:3306/core_db \n",
        'CORE_DATABASE_URL="mysql+pymysql://u:pw@mysql:3306/core_db"\n',
        "",
        f"CORE_DATABASE_URL={DB_URL}\nCORE_DATABASE_URL={DB_URL}\n",
    ],
    ids=["hyphen", "empty", "semicolon", "trailing-space", "quoted", "missing", "twice"],
)
def test_bad_database_name_fails_before_backup(fakes: Fakes, tmp_path: Path, line: str) -> None:
    checkout = make_checkout(tmp_path / "checkout", f"CORE_ENV=production\n{line}")
    result = fakes.deploy(checkout)
    assert result.returncode == 1
    assert "CORE_DATABASE_URL" in result.output and "nothing has been changed" in result.output
    assert result.steps() == ["pull"]
    assert "pw@" not in result.output


# healthcheck.sh


def test_healthcheck_uses_this_checkouts_container(
    fakes: Fakes, production: Path, tmp_path: Path
) -> None:
    project = INSTANCES["production"]["project"]
    other = make_checkout(tmp_path / "other", "CORE_ENV=production\n")
    fakes.config["containers"] = [
        container("aaaa00000001", other, project),
        container("cafe00000001", production, project),
    ]
    fakes.config["healthz"] = json.dumps({"status": "ok", "git_sha": SHA})
    result = fakes.healthcheck(production)
    assert result.returncode == 0, result.output
    execs = [argv for argv in result.docker() if argv[0] == "exec"]
    assert [argv[1] for argv in execs] == ["cafe00000001"]
    assert result.tool("sleep") == []


@pytest.mark.parametrize("count", [0, 2])
def test_healthcheck_without_exactly_one_container(
    fakes: Fakes, production: Path, tmp_path: Path, count: int
) -> None:
    project = INSTANCES["production"]["project"]
    other = make_checkout(tmp_path / "other", "CORE_ENV=production\n")
    fakes.config["containers"] = [
        container("aaaa00000001", other, project),
        container("aaaa00000002", production, project, oneoff="True"),
        *(container(f"cafe0000000{n}", production, project) for n in range(count)),
    ]
    fakes.config["healthz"] = SHA
    result = fakes.healthcheck(production)
    assert result.returncode == 1
    assert f"found {count} running core_api containers, expected exactly 1" in result.output
    assert [argv for argv in result.docker() if argv[0] == "exec"] == []
    assert len(result.tool("sleep")) == 30


def test_healthcheck_bad_core_env(fakes: Fakes, tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout", "CORE_ENV=production \n")
    result = fakes.healthcheck(checkout)
    assert result.returncode == 1
    assert "CORE_ENV" in result.output
    assert result.calls == []


# docker-compose.yml 与 shellcheck（用真的 docker 与 shellcheck）


def real_env(**values: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("CORE_")}
    return env | {"CORE_API_IMAGE": "ci/acuven-stock-core-api:ci"} | values


def compose_checkout(tmp_path: Path, name: str) -> Path:
    env = f"CORE_ENV={name}\nCORE_DATABASE_URL={DB_URL}\n"
    return make_checkout(tmp_path / f"compose_{name}", env)


@pytest.mark.parametrize("missing", COMPOSE_VARS)
def test_compose_requires_the_instance_variables(tmp_path: Path, missing: str) -> None:
    docker = shutil.which("docker")
    assert docker, "docker is not installed"
    checkout = compose_checkout(tmp_path, "demo")
    demo = INSTANCES["demo"]
    values = dict(zip(COMPOSE_VARS, (demo["data"], demo["callers"], demo["alias"]), strict=True))
    del values[missing]
    completed = subprocess.run(
        [docker, "compose", "-p", "acuven_stock_core", "config", "--quiet"],
        cwd=checkout,
        env=real_env(**values),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert missing in completed.stdout + completed.stderr


@pytest.mark.parametrize("name", INSTANCES)
def test_compose_resolves_through_the_instance(tmp_path: Path, name: str) -> None:
    assert shutil.which("docker"), "docker is not installed"
    checkout = compose_checkout(tmp_path, name)
    script = (
        '. deploy/instance.sh && docker compose -p "$CORE_COMPOSE_PROJECT" config --format json'
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=checkout,
        env=real_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    config = json.loads(completed.stdout)
    expected = INSTANCES[name]
    assert config["name"] == expected["project"]
    networks = {key: value["name"] for key, value in config["networks"].items()}
    assert sorted(networks.values()) == sorted([expected["data"], expected["callers"]])
    assert all(value.get("external") for value in config["networks"].values())
    attached = config["services"]["core_api"]["networks"]
    assert len(attached) == 2
    aliases = {networks[key]: (value or {}).get("aliases") for key, value in attached.items()}
    assert aliases[expected["callers"]] == [expected["alias"]]
    if name == "production":
        assert "data_net" not in networks.values()


def go_duration_seconds(value: object) -> float:
    """compose 的时长：字符串如 "45s"、"1m30s"，或整数纳秒。"""
    if isinstance(value, int | float):
        return value / 1e9
    assert isinstance(value, str)
    parts = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", value)
    assert parts and value, value
    hours, minutes, seconds = (float(part or 0) for part in parts.groups())
    return hours * 3600 + minutes * 60 + seconds


@pytest.mark.parametrize("name", INSTANCES)
def test_compose_stop_grace_period(tmp_path: Path, name: str) -> None:
    # 10 秒 uvicorn 收尾 + 30 秒定时任务停止的截止时间 + 5 秒余量（契约 8.6 第 4、5 条）。
    assert shutil.which("docker"), "docker is not installed"
    checkout = compose_checkout(tmp_path, name)
    script = (
        '. deploy/instance.sh && docker compose -p "$CORE_COMPOSE_PROJECT" config --format json'
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=checkout,
        env=real_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    config = json.loads(completed.stdout)
    assert config["name"] == INSTANCES[name]["project"]
    assert go_duration_seconds(config["services"]["core_api"]["stop_grace_period"]) == 45


def test_dockerfile_cmd_turns_off_uvicorn_access_log_and_bounds_graceful_shutdown() -> None:
    lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    (cmd,) = [line for line in lines if line.startswith("CMD")]
    # exec 形式：JSON 数组，uvicorn 直接收到停止信号，不经 shell。
    argv = json.loads(cmd.removeprefix("CMD").strip())
    assert isinstance(argv, list) and argv[0] == "uvicorn"
    assert "--no-access-log" in argv
    assert argv[argv.index("--timeout-graceful-shutdown") + 1] == "10"


def test_shellcheck_all_deploy_scripts() -> None:
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "shellcheck is not installed"
    command = [shellcheck, "-x", *(f"deploy/{name}" for name in SCRIPTS)]
    checked = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
