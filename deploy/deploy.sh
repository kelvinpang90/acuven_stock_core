#!/usr/bin/env bash
#
# 把一个提交的镜像部署到本机的 Core 栈。由 .github/workflows/deploy.yml 经 SSH 调用。
#
# 用法（在服务器上本仓库检出的根目录里）：
#     CORE_IMAGE_REPO=ghcr.io/所有者/仓库名 deploy/deploy.sh 完整的40位commitSHA
#
# 顺序：确保内部网络 → 记下正在跑的版本 → 拉新镜像 → 迁移 → 换容器 → 等健康 → 不健康则回滚。
# 成功才以 0 退出；迁移失败、换容器失败、不健康、回滚（无论回滚成败）都以非零退出（部署观察契约 D3）。
# 迁移只能是向后兼容的（加表、加列、加索引）：迁移跑完而新容器没起来时，旧版本要能继续对着新表结构工作。

set -euo pipefail

SHA="${1:-}"
HEALTH_TIMEOUT_SECONDS="${CORE_HEALTH_TIMEOUT_SECONDS:-180}"
SERVICE=core_api
INTERNAL_NETWORK=stock_core_net

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

wait_for_health() {
    local deadline
    deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ "$(docker compose ps --format '{{.Health}}' "$SERVICE")" = "healthy" ]; then
            return 0
        fi
        sleep 5
    done
    return 1
}

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "usage: deploy/deploy.sh <full 40-character lowercase commit SHA>"
[ -n "${CORE_IMAGE_REPO:-}" ] || die "CORE_IMAGE_REPO is not set"

export CORE_API_IMAGE="${CORE_IMAGE_REPO}-api:${SHA}"

# Core 与网店之间的内部网络（--internal：不出宿主机）。只在不存在时创建，已存在的不动。
if ! docker network inspect "$INTERNAL_NETWORK" >/dev/null 2>&1; then
    log "creating internal network $INTERNAL_NETWORK"
    docker network create --internal "$INTERNAL_NETWORK" >/dev/null || die "could not create $INTERNAL_NETWORK"
fi

# 回滚目标必须在任何改动之前记下。
PREVIOUS_API="$(docker compose ps --format '{{.Image}}' "$SERVICE")"
log "currently running: ${PREVIOUS_API:-nothing}"

log "pulling $CORE_API_IMAGE"
docker compose pull --quiet || die "pull failed; nothing has been changed"

log "running migrations"
docker compose run --rm --no-deps "$SERVICE" alembic upgrade head \
    || die "migration failed; the previous version is still running"

log "starting $SHA"
if docker compose up -d --no-build && wait_for_health; then
    log "deployed $SHA"
    # 只清本项目的（构建时打了这个标签）、且没有容器在用的旧镜像；清理失败不改变部署结论。
    if ! docker image prune --all --force --filter "label=acuven.project=stock_core" >/dev/null; then
        log "WARNING: could not prune old images"
    fi
    exit 0
fi

log "deploy of $SHA is not healthy"
docker compose ps

if [ -z "$PREVIOUS_API" ]; then
    # 首次部署没有回滚目标：保留现场给人排查。
    die "nothing to roll back to; leaving the stack up for inspection"
fi

log "rolling back to $PREVIOUS_API"
export CORE_API_IMAGE="$PREVIOUS_API"
docker compose up -d --no-build || die "rollback failed; manual intervention required"
if wait_for_health; then
    # 回滚成功也以非零退出：这个提交没能上线，部署结论必须是失败。
    die "rolled back to $PREVIOUS_API; the deploy of $SHA failed"
fi
die "rollback did not become healthy; manual intervention required"
