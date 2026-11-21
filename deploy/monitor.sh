#!/usr/bin/env bash
#
# 运行告警巡检（契约 8.5）。由服务器 cron 每 5 分钟跑一次（deploy/monitor.cron，装法见 docs/OPS_MONITORING.md）。
#
# 只读。查三个维度：readyz（容器与 /readyz）、periodic（/readyz 正文里的定时任务状态）、invariants（不变量自查）。
# 有不合格的，隔 45 秒整轮复核一次，复核仍不合格的才算不合格。每个维度每轮都推一次心跳（Healthchecks）：
# 合格向检查的地址本身 POST，不合格向地址加 /fail POST、请求体为通知正文（一行一个问题，以级别开头），
# 由心跳服务通知 Telegram。全绿也推：cron 没跑、机器挂了、脚本卡死时只有“该到的心跳没到”能暴露。
#
# 连接同 deploy/healthcheck.sh：按 compose 标签找 core_api 容器（另排除一次性容器），
# docker exec 进去用容器里的 Python 取容器内回环。每一步都限时，超时按该维度没查成处理。
#
# 配置只从 .env 读四个键，按行解析、不 source 整个文件：CORE_MONITOR_TOKEN、CORE_HEALTHCHECK_READYZ_URL、
# CORE_HEALTHCHECK_PERIODIC_URL、CORE_HEALTHCHECK_INVARIANTS_URL。.env 默认是本脚本所在检出的根目录下的 .env，
# 与当前目录无关。令牌经 docker exec -e 按名字传进容器；令牌与心跳地址都不进日志、通知正文与 docker 的参数。
#
# 环境变量：CORE_MONITOR_ENV_FILE 覆盖 .env 的路径（演练用），CORE_MONITOR_RECHECK_SECONDS 覆盖复核前等待的秒数。
# 退出码：全部合格且三个心跳地址都配了为 0，否则为 1。推心跳失败只记日志，不改变结论与退出码。
#
# 用法：bash deploy/monitor.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${CORE_MONITOR_ENV_FILE:-$(dirname -- "$SCRIPT_DIR")/.env}"
RECHECK_SECONDS="${CORE_MONITOR_RECHECK_SECONDS:-45}"
[[ "$RECHECK_SECONDS" =~ ^[0-9]+$ ]] || RECHECK_SECONDS=45
LOG_TAG=stock_core_monitor

CONTAINER_FILTERS=(
    --filter label=com.docker.compose.project=acuven_stock_core
    --filter label=com.docker.compose.service=core_api
    --filter label=com.docker.compose.oneoff=False
)

# 容器里的 Python 共用的部分：取一个路径，只把结论按行打到标准输出，不打错误原文。
# 这几段经命令行参数传进容器，只用 ASCII，免得受容器里语言环境的影响。
# read -d '' 读到文件尾时返回非 0，所以每段后面跟 || true。
IFS= read -r -d '' FETCH_PY <<'PY' || true
import json
import os
import re
import urllib.error
import urllib.request


def fetch(path, seconds, headers):
    """(HTTP status, body); ("timeout", None) on a timeout, ("unreadable", None) otherwise."""
    request = urllib.request.Request("http://127.0.0.1:8000" + path, headers=headers)
    try:
        try:
            response = urllib.request.urlopen(request, timeout=seconds)
        except urllib.error.HTTPError as error:
            response = error
        return response.getcode(), response.read()
    except TimeoutError:
        return "timeout", None
    except urllib.error.URLError as error:
        return ("timeout" if isinstance(error.reason, TimeoutError) else "unreadable"), None
    except Exception:
        return "unreadable", None


def clean(value):
    """Names and IDs keep only safe characters, so a response cannot garble the message."""
    return re.sub(r"[^A-Za-z0-9._:-]", "?", str(value))[:80]


def json_object(raw):
    try:
        body = json.loads(raw)
    except Exception:
        return None
    return body if isinstance(body, dict) else None
PY

# 输出：timeout | unreadable | http 状态码，之后 body unreadable，或 status ok|not_ok 与
# periodic ok|not_ok|missing，periodic 不是 ok 时每个 stale 的任务一行 stale 名字。
IFS= read -r -d '' READYZ_PY <<'PY' || true
code, raw = fetch("/readyz", 10, {})
if not isinstance(code, int):
    print(code)
else:
    print("http", code)
    body = json_object(raw)
    if body is None:
        print("body unreadable")
    else:
        print("status", "ok" if body.get("status") == "ok" else "not_ok")
        periodic = body.get("periodic")
        if not isinstance(periodic, dict):
            print("periodic missing")
        elif periodic.get("status") == "ok":
            print("periodic ok")
        else:
            print("periodic not_ok")
            tasks = periodic.get("tasks")
            for task in tasks if isinstance(tasks, list) else []:
                if isinstance(task, dict) and task.get("stale") is True:
                    print("stale", clean(task.get("name")))
PY
READYZ_PY="$FETCH_PY$READYZ_PY"

# 输出：timeout | unreadable | http 状态码（不是 200 时）| ok | violations 总数 返回的项数，
# 之后按 check 首次出现的顺序每个 check 一行项数，再是最多前 5 项的 check 与 ID（不输出 order_ref）。
IFS= read -r -d '' INVARIANTS_PY <<'PY' || true
token = os.environ.get("CORE_MONITOR_TOKEN", "")
code, raw = fetch("/v1/ops/invariants", 45, {"Authorization": "Bearer " + token})
if not isinstance(code, int):
    print(code)
elif code != 200:
    print("http", code)
else:
    body = json_object(raw)
    data = body.get("data") if body else None
    try:
        ok, count, violations = data["ok"], data["violation_count"], data["violations"]
        if not (isinstance(ok, bool) and type(count) is int and isinstance(violations, list)):
            raise ValueError
        checks = [clean(violation["check"]) for violation in violations]
    except Exception:
        print("unreadable")
    else:
        if ok:
            print("ok")
        else:
            print("violations", count, len(violations))
            for check in dict.fromkeys(checks):
                print("check", check, checks.count(check))
            for violation in violations[:5]:
                if "reservation_id" in violation:
                    ids = "reservation_id=" + clean(violation["reservation_id"])
                else:
                    pool, sku = clean(violation.get("pool_id")), clean(violation.get("sku_id"))
                    ids = "pool_id=" + pool + " sku_id=" + sku
                print("item", clean(violation["check"]), ids)
PY
INVARIANTS_PY="$FETCH_PY$INVARIANTS_PY"

log() {
    logger -t "$LOG_TAG" -- "$1" 2>/dev/null || true
    printf '%s\n' "$1" >&2
}

# 只取四个键：取第一个等号之后的值，去掉首尾空白与一对成对的引号；同一个键出现多次取最后一个；不认行尾注释。
# 文件不存在或读不了时四个值都为空（按没配令牌与没配心跳地址处理）。
token="" readyz_url="" periodic_url="" invariants_url=""
read_env() {
    local file="$1" line key value first last
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then
        return 0
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${line#*=}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        if [ "${#value}" -ge 2 ]; then
            first="${value:0:1}"
            last="${value: -1}"
            if [[ "$first" == "$last" && ( "$first" == '"' || "$first" == "'" ) ]]; then
                value="${value:1:${#value}-2}"
            fi
        fi
        case "$key" in
            CORE_MONITOR_TOKEN) token="$value" ;;
            CORE_HEALTHCHECK_READYZ_URL) readyz_url="$value" ;;
            CORE_HEALTHCHECK_PERIODIC_URL) periodic_url="$value" ;;
            CORE_HEALTHCHECK_INVARIANTS_URL) invariants_url="$value" ;;
        esac
    done <"$file" || true
}

# 一轮的结论：每个维度一段通知正文，合格为空。
readyz_text="" periodic_text="" invariants_text=""

not_checked_all() {
    readyz_text="P1 readyz: not checked: $1"
    periodic_text="P2 periodic: not checked: $1"
    invariants_text="P2 invariants: not checked: $1"
}

check_readyz() {
    local output status=0 key value http="" body_state="" ready_status="" periodic="" names=""
    local stale=()
    output="$(timeout 15 docker exec "$1" python -c "$READYZ_PY" 2>/dev/null)" || status=$?
    while read -r key value; do
        case "$key" in
            timeout) http=timeout ;;
            http) http="$value" ;;
            body) body_state="$value" ;;
            status) ready_status="$value" ;;
            periodic) periodic="$value" ;;
            stale) stale+=("$value") ;;
        esac
    done <<<"$output"

    if [ "$status" -eq 124 ] || [ "$http" = timeout ]; then
        readyz_text="P1 readyz: not checked: timed out"
        periodic_text="P2 periodic: not checked: timed out"
        return
    fi
    if ! [[ "$http" =~ ^[0-9]{3}$ ]]; then
        readyz_text="P1 readyz: not checked: response unreadable"
        periodic_text="P2 periodic: not checked: response unreadable"
        return
    fi

    # 状态码只看数据库；503 的正文照样带 periodic，下面照读。
    if [ "$http" != 200 ]; then
        readyz_text="P1 readyz: /readyz returned HTTP $http"
    elif [ "$ready_status" = ok ]; then
        :
    elif [ "$body_state" = unreadable ] || [ -z "$ready_status" ]; then
        readyz_text="P1 readyz: /readyz response unreadable"
    else
        readyz_text="P1 readyz: /readyz status is not ok"
    fi

    if [ "$periodic" = ok ]; then
        :
    elif [ "$periodic" = missing ]; then
        periodic_text="P2 periodic: /readyz body has no periodic"
    elif [ "$periodic" = not_ok ] && [ "${#stale[@]}" -gt 0 ]; then
        for value in "${stale[@]}"; do
            names+="${names:+, }$value"
        done
        periodic_text="P2 periodic: stale tasks: $names"
    elif [ "$periodic" = not_ok ]; then
        periodic_text="P2 periodic: periodic.status is not ok"
    else
        periodic_text="P2 periodic: not checked: response unreadable"
    fi
}

check_invariants() {
    if [ -z "$token" ]; then
        invariants_text="P2 invariants: not checked: CORE_MONITOR_TOKEN is not configured"
        return
    fi
    local output status=0 key first rest result="" code="" count="" shown="" line text
    local checks=() items=()
    output="$(CORE_MONITOR_TOKEN="$token" timeout 60 \
        docker exec -e CORE_MONITOR_TOKEN "$1" python -c "$INVARIANTS_PY" 2>/dev/null)" || status=$?
    while read -r key first rest; do
        case "$key" in
            ok | timeout | unreadable) result="$key" ;;
            http) result=http code="$first" ;;
            violations) result=violations count="$first" shown="$rest" ;;
            check) checks+=("P1 invariants: check $first: $rest") ;;
            item) items+=("P1 invariants: $first $rest") ;;
        esac
    done <<<"$output"

    if [ "$status" -eq 124 ] || [ "$result" = timeout ]; then
        invariants_text="P2 invariants: not checked: timed out"
    elif [ "$result" = http ] && [[ "$code" =~ ^[0-9]{3}$ ]]; then
        invariants_text="P2 invariants: not checked: HTTP $code"
    elif [ "$result" = ok ]; then
        :
    elif [ "$result" = violations ] && [[ "$count" =~ ^[0-9]+$ ]] && [[ "$shown" =~ ^[0-9]+$ ]]; then
        text="P1 invariants: violation_count=$count"
        # 响应里至多 100 项：总数更多时，按 check 数出的项数只是返回的那些。
        if [ "$count" -gt "$shown" ]; then
            text+=" (counts by check cover only the first $shown returned)"
        fi
        for line in "${checks[@]}" "${items[@]}"; do
            text+=$'\n'"$line"
        done
        invariants_text="$text"
    else
        invariants_text="P2 invariants: not checked: response unreadable"
    fi
}

check_round() {
    readyz_text="" periodic_text="" invariants_text=""
    local ids status=0 count
    ids="$(timeout 10 docker ps -q "${CONTAINER_FILTERS[@]}" 2>/dev/null)" || status=$?
    if [ "$status" -eq 124 ]; then
        not_checked_all "timed out finding the core_api container"
        return
    fi
    if [ "$status" -ne 0 ]; then
        not_checked_all "core_api container count unknown (docker ps failed)"
        return
    fi
    count="$(grep -c . <<<"$ids" || true)"
    if [ "$count" -ne 1 ]; then
        not_checked_all "found $count running core_api containers, expected exactly 1"
        return
    fi
    check_readyz "$ids"
    check_invariants "$ids"
}

# push 维度 键名 心跳地址 通知正文：没配地址时记一行并返回 1；推失败只记日志。
push() {
    local dimension="$1" key="$2" url="$3" text="$4" target body status=0
    if [ -z "$url" ]; then
        log "$key is not configured; $dimension results will not notify anyone"
        return 1
    fi
    if [ -n "$text" ]; then
        target="${url%/}/fail" body="$text"
    else
        target="$url" body="ok"
    fi
    printf '%s\n' "$body" | curl -q -fs --max-time 10 -o /dev/null \
        -H 'Content-Type: text/plain; charset=utf-8' --data-binary @- "$target" 2>/dev/null || status=$?
    if [ "$status" -ne 0 ]; then
        log "heartbeat for $dimension not delivered (curl exit $status)"
    fi
}

read_env "$ENV_FILE"

check_round
if [ -n "$readyz_text$periodic_text$invariants_text" ]; then
    problems=""
    [ -z "$readyz_text" ] || problems+=" readyz"
    [ -z "$periodic_text" ] || problems+=" periodic"
    [ -z "$invariants_text" ] || problems+=" invariants"
    log "problems found in${problems}; rechecking in ${RECHECK_SECONDS} seconds"
    sleep "$RECHECK_SECONDS"
    check_round
fi

exit_status=0
for text in "$readyz_text" "$periodic_text" "$invariants_text"; do
    [ -n "$text" ] || continue
    exit_status=1
    while IFS= read -r line; do
        log "$line"
    done <<<"$text"
done

push readyz CORE_HEALTHCHECK_READYZ_URL "$readyz_url" "$readyz_text" || exit_status=1
push periodic CORE_HEALTHCHECK_PERIODIC_URL "$periodic_url" "$periodic_text" || exit_status=1
push invariants CORE_HEALTHCHECK_INVARIANTS_URL "$invariants_url" "$invariants_text" || exit_status=1

exit "$exit_status"
