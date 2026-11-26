#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
#
# 部署后健康检查（部署观察契约 D3）。由 .github/workflows/deploy.yml 经 SSH 调用。
#
# 健康检查端点不经反向代理转发（Demo 没有公网入口，生产的 vhost 只转发后台与平台接口两个前缀），
# 所以不能像网店那样从 GitHub 访问健康地址：仍在服务器上进容器取 /healthz，
# 要求返回体里带着本次提交的 SHA——检查通过即证明线上跑的就是这个提交。
#
# 经 deploy/instance.sh 找本实例、本检出的 core_api 容器（按 .env 的 CORE_ENV）再 docker exec；不是恰好一个就算没找到。
# 不用 docker compose exec：后者要展开 docker-compose.yml 里只由 deploy/deploy.sh 设置的 CORE_API_IMAGE，这里没有它就会直接报错。
#
# 用法（在服务器上本仓库检出的根目录里）：deploy/healthcheck.sh 完整的40位commitSHA

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=instance.sh
. "$SCRIPT_DIR/instance.sh"

SHA="${1:-}"
[ -n "$CORE_INSTANCE" ] || { echo "$CORE_INSTANCE_ERROR"; exit 1; }
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "usage: deploy/healthcheck.sh <full 40-character lowercase commit SHA>"; exit 1; }

output=""
for attempt in $(seq 1 30); do
    status=0
    ids="$(core_find_api_containers)" || status=$?
    count="$(grep -c . <<<"$ids" || true)"
    if [ "$status" -eq 124 ]; then
        output="timed out finding the core_api container"
    elif [ "$status" -ne 0 ]; then
        output="core_api container count unknown (docker ps failed)"
    elif [ "$count" -ne 1 ]; then
        output="found $count running core_api containers, expected exactly 1"
    else
        output="$(docker exec "$ids" python -c \
            "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/healthz', timeout=5).read().decode())" \
            2>&1 || true)"
        if [[ "$output" == *"$SHA"* ]]; then
            echo "healthy on attempt $attempt: $output"
            exit 0
        fi
    fi
    sleep 10
done
echo "health check did not report $SHA within 5 minutes; last output:"
echo "$output"
exit 1
