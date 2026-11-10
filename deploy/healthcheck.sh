#!/usr/bin/env bash
#
# 部署后健康检查（部署观察契约 D3）。由 .github/workflows/deploy.yml 经 SSH 调用。
#
# Core 没有公网入口，所以不能像网店那样从 GitHub 访问健康地址：在服务器上进容器取 /healthz，
# 要求返回体里带着本次提交的 SHA——检查通过即证明线上跑的就是这个提交。
#
# 用法（在服务器上本仓库检出的根目录里）：deploy/healthcheck.sh 完整的40位commitSHA

set -euo pipefail

SHA="${1:-}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "usage: deploy/healthcheck.sh <full 40-character lowercase commit SHA>"; exit 1; }

for attempt in $(seq 1 30); do
    body="$(docker compose exec -T core_api python -c \
        "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/healthz', timeout=5).read().decode())" \
        2>/dev/null || true)"
    if [[ "$body" == *"$SHA"* ]]; then
        echo "healthy on attempt $attempt: $body"
        exit 0
    fi
    sleep 10
done
echo "health check did not report $SHA within 5 minutes"
exit 1
