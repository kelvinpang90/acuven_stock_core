#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
#
# 把一个提交的镜像部署到本机的 Core 栈。由 .github/workflows/deploy.yml 经 SSH 调用。
#
# 用法（在服务器上本仓库检出的根目录里）：
#     CORE_IMAGE_REPO=ghcr.io/所有者/仓库名 deploy/deploy.sh 完整的40位commitSHA
#
# 实例（Demo 或生产）只由本检出 .env 里的 CORE_ENV 决定，经 deploy/instance.sh 读出；读不对就退出，什么也不碰。
# 所有 compose 调用都在本检出的根目录里跑，并带 -p 本实例的项目名（docs/PRODUCTION_AND_RECOVERY.md 第 1.2 节）。
#
# 顺序：主机锁 → 核对数据网络 → 确保 Demo 的调用方内部网络（生产：核对代理网络）→ 记下正在跑的版本 → 拉新镜像 →
#（只生产）备份本库 → 迁移 → 换容器 → 等健康 → 不健康则回滚。
# 成功才以 0 退出；备份失败、迁移失败、换容器失败、不健康、回滚（无论回滚成败）都以非零退出（部署观察契约 D3）。
# 迁移只能是向后兼容的（加表、加列、加索引）：迁移跑完而新容器没起来时，旧版本要能继续对着新表结构工作。
#
# 生产迁移前的备份（P-09）：经 docker exec 进 infra_mysql，用它自己环境变量里的 root 密码跑 mysqldump，限时 60 秒；
# 密码只在容器里的 shell 中经 MYSQL_PWD 交给 mysqldump，不进任何命令行参数与日志。末尾有 Dump completed 才算成功。
# 放在检出目录之外：${XDG_STATE_HOME:-~/.local/state}/stock_core_production/pre_migration（CORE_BACKUP_DIR 可覆盖），
# 目录 700、文件 600，文件名为 UTC 时间-完整SHA.sql，只保留最近 5 份。Demo 不备份。

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=instance.sh
. "$SCRIPT_DIR/instance.sh"

SHA="${1:-}"
HEALTH_TIMEOUT_SECONDS="${CORE_HEALTH_TIMEOUT_SECONDS:-180}"
SERVICE=core_api
INFRA_MYSQL_CONTAINER=infra_mysql
BACKUP_TIMEOUT_SECONDS=60
BACKUP_KEEP=5
BACKUP_NAME_PATTERN='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{40}\.sql$'

# 在 infra_mysql 里跑，库名是 $1。root 密码取自容器自己的环境变量，经 MYSQL_PWD 交给 mysqldump。
# 不加 --source-data：不加全局读锁。
# shellcheck disable=SC2016
DUMP_SCRIPT='[ -n "${MYSQL_ROOT_PASSWORD:-}" ] || { echo "MYSQL_ROOT_PASSWORD is not set in the container" >&2; exit 3; }
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
exec mysqldump --user=root --single-transaction --no-tablespaces --set-gtid-purged=OFF --databases "$1"'

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

[ -n "$CORE_INSTANCE" ] || die "$CORE_INSTANCE_ERROR; nothing has been changed"
cd -- "$CORE_CHECKOUT_DIR" || die "cannot enter the checkout $CORE_CHECKOUT_DIR; nothing has been changed"

compose() { docker compose -p "$CORE_COMPOSE_PROJECT" "$@"; }

wait_for_health() {
    local deadline
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ "$(compose ps --format '{{.Health}}' "$SERVICE")" = "healthy" ]; then
            return 0
        fi
        sleep 5
    done
    return 1
}

# 只数符合正式文件名的文件，按文件名（即时间）排序删最旧的；临时文件与别的文件不算也不删。删不掉只记一行。
prune_backups() {
    local dir="$1" file index names=()
    for file in "$dir"/*; do
        if [[ "${file##*/}" =~ $BACKUP_NAME_PATTERN ]]; then
            names+=("${file##*/}")
        fi
    done
    [ "${#names[@]}" -gt "$BACKUP_KEEP" ] || return 0
    mapfile -t names < <(printf '%s\n' "${names[@]}" | LC_ALL=C sort)
    for ((index = 0; index < ${#names[@]} - BACKUP_KEEP; index++)); do
        rm -f -- "${dir:?}/${names[index]}" || log "WARNING: could not remove the old backup ${names[index]}"
    done
}

backup_database() {
    local url name dir final tmp status=0 last reason
    # 库名按与 CORE_ENV 相同的读法取出：先在第一个问号处截断，再取最后一个斜杠之后的部分。连接串本身不进日志。
    url="$(core_instance_env_value CORE_DATABASE_URL)" \
        || die "CORE_DATABASE_URL must be exactly one line in .env; nothing has been changed"
    url="${url%%\?*}"
    name="${url##*/}"
    [[ "$name" =~ ^[A-Za-z0-9_]+$ ]] \
        || die "the database name in CORE_DATABASE_URL must be letters, digits and underscores; nothing has been changed"

    dir="${CORE_BACKUP_DIR:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/stock_core_production/pre_migration}"
    if ! { (umask 077 && mkdir -p -- "$dir") && chmod 700 -- "$dir"; }; then
        die "cannot create the backup directory $dir; nothing has been changed"
    fi
    final="$dir/$(date -u +%Y%m%dT%H%M%SZ)-$SHA.sql"
    tmp="$(mktemp "$dir/.pre_migration.XXXXXX")" || die "cannot create a file in $dir; nothing has been changed"

    log "backing up database $name before migrating"
    timeout "$BACKUP_TIMEOUT_SECONDS" docker exec "$INFRA_MYSQL_CONTAINER" sh -c "$DUMP_SCRIPT" sh "$name" >"$tmp" \
        || status=$?
    last="$(tail -n 1 -- "$tmp" 2>/dev/null || true)"
    if [ "$status" -ne 0 ] || [[ "$last" != *"Dump completed"* ]]; then
        rm -f -- "$tmp"
        if [ "$status" -eq 124 ]; then
            reason="timed out after ${BACKUP_TIMEOUT_SECONDS}s"
        elif [ "$status" -ne 0 ]; then
            reason="docker exec $INFRA_MYSQL_CONTAINER exited $status"
        else
            reason="the dump does not end with Dump completed"
        fi
        die "backup of $name failed ($reason); nothing has been changed"
    fi
    if ! { chmod 600 -- "$tmp" && mv -f -- "$tmp" "$final"; }; then
        rm -f -- "$tmp"
        die "cannot store the backup in $dir; nothing has been changed"
    fi
    log "backed up $name to $final"
    prune_backups "$dir"
}

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "usage: deploy/deploy.sh <full 40-character lowercase commit SHA>"
[ -n "${CORE_IMAGE_REPO:-}" ] || die "CORE_IMAGE_REPO is not set"

export CORE_API_IMAGE="${CORE_IMAGE_REPO}-api:${SHA}"

# 主机级部署锁：同一台主机上的各项目部署一个接一个跑（约定见 vps_infra README「部署串行化」），本项目的两个实例也是。
# 起因：另一个项目部署成功后的 image prune 恰好落在本项目拉镜像的几秒里，containerd 报
# content digest not found / failed to extract layer（2026-10-27、11-03、11-14 三次）。
# 锁覆盖整个脚本：prune 与回滚也在锁内。进程退出（含 die）时由内核释放。
DEPLOY_LOCK="${ACUVEN_DEPLOY_LOCK:-/run/lock/acuven-deploy.lock}"
DEPLOY_LOCK_WAIT="${ACUVEN_DEPLOY_LOCK_WAIT:-300}"
command -v flock >/dev/null || die "flock is not installed; nothing has been changed"
# 先试追加写；文件属于别的用户时（protected_regular）退回只读打开，flock 对只读描述符同样有效。
# ⚠️ 2>/dev/null 必须放在 { } 外面：写成 `exec 9>>f 2>/dev/null` 时，exec 会把整段脚本此后的
#    stderr 永久丢掉（2026-11-14 billing 会话实测发现）。
{ exec 9>>"$DEPLOY_LOCK"; } 2>/dev/null || { exec 9<"$DEPLOY_LOCK"; } 2>/dev/null \
    || die "cannot open the host deploy lock $DEPLOY_LOCK; nothing has been changed"
if ! flock -n 9; then
    log "another deploy holds the host lock ($(head -1 "$DEPLOY_LOCK" 2>/dev/null || echo unknown)); waiting up to ${DEPLOY_LOCK_WAIT}s"
    flock -w "$DEPLOY_LOCK_WAIT" 9 \
        || die "the host deploy lock was not free within ${DEPLOY_LOCK_WAIT}s; nothing has been changed (re-run this job)"
fi
# 尽力写下持锁方（带实例名），给排队的人看；写不进去不影响部署。
printf '%s %s pid=%s since=%s\n' "stock_core $CORE_INSTANCE" "$SHA" "$$" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>/dev/null >"$DEPLOY_LOCK" || true
log "holding the host deploy lock (instance $CORE_INSTANCE)"

# 数据网络由 vps_infra 建（生产的是只有 infra_mysql 与生产 Core 的专用网络）：这里只核对，不存在就失败，不自己建。
if ! docker network inspect "$CORE_DATA_NETWORK" >/dev/null 2>&1; then
    die "data network $CORE_DATA_NETWORK does not exist (vps_infra creates it); nothing has been changed"
fi

# Core 对外的那条网络，按 instance.sh 给的标记：
# - Demo（internal）：Core 与调用方之间的内部网络（--internal：不出宿主机）。只在不存在时创建，已存在的不动。
# - 生产（external）：边缘反向代理所在的代理网络，由 vps_infra 建：同数据网络，只核对，不存在就失败，从不自己建。
#   以前建的 stock_core_production_net 不由这里删除，留给人处理。
if ! docker network inspect "$CORE_CALLER_NETWORK" >/dev/null 2>&1; then
    if [ "$CORE_CALLER_NETWORK_KIND" != internal ]; then
        die "network $CORE_CALLER_NETWORK does not exist (vps_infra creates it); nothing has been changed"
    fi
    log "creating internal network $CORE_CALLER_NETWORK"
    docker network create --internal "$CORE_CALLER_NETWORK" >/dev/null || die "could not create $CORE_CALLER_NETWORK"
fi

# 回滚目标必须在任何改动之前记下。
PREVIOUS_API="$(compose ps --format '{{.Image}}' "$SERVICE")"
log "currently running: ${PREVIOUS_API:-nothing}"

log "pulling $CORE_API_IMAGE"
compose pull --quiet || die "pull failed; nothing has been changed"

if [ "$CORE_INSTANCE" = production ]; then
    backup_database
fi

log "running migrations"
compose run --rm --no-deps "$SERVICE" alembic upgrade head \
    || die "migration failed; the previous version is still running"

log "starting $SHA"
if compose up -d --no-build && wait_for_health; then
    log "deployed $SHA"
    # 只清本项目的（构建时打了这个标签，两个实例共用）、且没有容器在用的旧镜像；清理失败不改变部署结论。
    if ! docker image prune --all --force --filter "label=acuven.project=stock_core" >/dev/null; then
        log "WARNING: could not prune old images"
    fi
    exit 0
fi

log "deploy of $SHA is not healthy"
compose ps

if [ -z "$PREVIOUS_API" ]; then
    # 首次部署没有回滚目标：保留现场给人排查。
    die "nothing to roll back to; leaving the stack up for inspection"
fi

log "rolling back to $PREVIOUS_API"
export CORE_API_IMAGE="$PREVIOUS_API"
compose up -d --no-build || die "rollback failed; manual intervention required"
if wait_for_health; then
    # 回滚成功也以非零退出：这个提交没能上线，部署结论必须是失败。
    die "rolled back to $PREVIOUS_API; the deploy of $SHA failed"
fi
die "rollback did not become healthy; manual intervention required"
