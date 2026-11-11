#!/usr/bin/env bash
#
# 部署后健康检查（部署观察契约 D3）。由 .github/workflows/deploy.yml 经 SSH 调用。
#
# Core 没有公网入口，所以不能像网店那样从 GitHub 访问健康地址：在服务器上进容器取 /healthz，
# 要求返回体里带着本次提交的 SHA——检查通过即证明线上跑的就是这个提交。
#
# 按 compose 标签找容器再 docker exec，不用 docker compose exec：后者要展开 docker-compose.yml 里
# 只由 deploy/deploy.sh 设置的 CORE_API_IMAGE，这里没有它就会直接报错。
#
# 用法（在服务器上本仓库检出的根目录里）：deploy/healthcheck.sh 完整的40位commitSHA

set -euo pipefail

SHA="${1:-}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "usage: deploy/healthcheck.sh <full 40-character lowercase commit SHA>"; exit 1; }

output=""
for attempt in $(seq 1 30); do
    container="$(docker ps -q \
        --filter label=com.docker.compose.project=acuven_stock_core \
        --filter label=com.docker.compose.service=core_api)"
    if [ -z "$container" ]; then
        output="no running core_api container"
    else
        output="$(docker exec "$container" python -c \
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
