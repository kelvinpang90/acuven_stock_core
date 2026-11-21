#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
#
# 运行告警巡检（契约 0.20 §8.5）。只装在生产实例的检出上，由服务器 cron 每 5 分钟跑一次（deploy/monitor.cron，
# 装法见 docs/OPS_MONITORING.md）。
#
# 只读。查三个维度：readyz（容器与 /readyz）、periodic（/readyz 正文里的定时任务状态）、invariants（不变量自查）。
# 有不合格的，隔 45 秒整轮复核一次，复核仍不合格的才算不合格。推两个检查（Healthchecks）：readyz 与 periodic
# 两个维度合用 readyz 那个检查（任一不合格即推失败），invariants 单独一个。每个检查每轮都推一次心跳：
# 合格向检查的地址本身 POST，不合格向地址加 /fail POST、请求体为通知正文（一行一个问题，以级别开头），
# 由心跳服务通知 Telegram。全绿也推：cron 没跑、机器挂了、脚本卡死时只有“该到的心跳没到”能暴露。
#
# 重新通知：心跳服务只在检查状态变化时通知。状态文件按检查记下上一次推送成功的失败里带的问题（维度加级别）；
# 检查已红、而本轮有记录里没有的维度或更高的级别时，先推一次成功、紧接着推失败，让心跳服务重新通知一次。
# 记录读不出（文件不存在、读不了、不合格式，或记为未知）时，本轮这个检查只要不合格就先成功后失败。
# 失败那一推（合格时为成功那一推）送到之后才改这个检查的记录；任何一推没推成就不改。
#
# 连接同 deploy/healthcheck.sh：经 deploy/instance.sh 找本实例、本检出的 core_api 容器（按检出 .env 的 CORE_ENV，
# 另排除一次性容器；不是恰好一个就算没查成），docker exec 进去用容器里的 Python 取容器内回环。
# 每一步都限时，超时按该维度没查成处理。CORE_ENV 读不对时按找不到容器处理，另记一行系统日志。
#
# 配置只从 .env 读三个键，按行解析、不 source 整个文件：CORE_MONITOR_TOKEN、CORE_HEALTHCHECK_READYZ_URL、
# CORE_HEALTHCHECK_INVARIANTS_URL（0.20 取消的 CORE_HEALTHCHECK_PERIODIC_URL 留着也不读）。.env 默认是本脚本所在
# 检出的根目录下的 .env，与当前目录无关。令牌经 docker exec -e 按名字传进容器；令牌与心跳地址都不进日志、
# 通知正文、状态文件与 docker 的参数。
#
# 环境变量：CORE_MONITOR_ENV_FILE 覆盖 .env 的路径（演练用），CORE_MONITOR_RECHECK_SECONDS 覆盖复核前等待的秒数，
# CORE_MONITOR_STATE_FILE 覆盖状态文件的路径（默认 ${XDG_STATE_HOME:-~/.local/state}/stock_core_monitor/state）。
# 退出码：全部合格且两个心跳地址都配了为 0，否则为 1。推心跳失败、状态文件写不进去只记日志，不改变结论与退出码。
#
# 用法：bash deploy/monitor.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=instance.sh
. "$SCRIPT_DIR/instance.sh"
ENV_FILE="${CORE_MONITOR_ENV_FILE:-$(dirname -- "$SCRIPT_DIR")/.env}"
RECHECK_SECONDS="${CORE_MONITOR_RECHECK_SECONDS:-45}"
[[ "$RECHECK_SECONDS" =~ ^[0-9]+$ ]] || RECHECK_SECONDS=45
STATE_FILE="${CORE_MONITOR_STATE_FILE:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/stock_core_monitor/state}"
STATE_HEADER="stock_core_monitor state 1"
LOG_TAG=stock_core_monitor

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

# 只取三个键：取第一个等号之后的值，去掉首尾空白与一对成对的引号；同一个键出现多次取最后一个；不认行尾注释。
# 文件不存在或读不了时三个值都为空（按没配令牌与没配心跳地址处理）。
token="" readyz_url="" invariants_url=""
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
            CORE_HEALTHCHECK_INVARIANTS_URL) invariants_url="$value" ;;
        esac
    done <"$file" || true
}

# 状态文件：第一行是 STATE_HEADER，之后每个检查一行“检查名 记录”。记录为 none（空）、unknown（未知），
# 或逗号分隔的“维度:级别”，例如 readyz readyz:P1,periodic:P2。
declare -A records=([readyz]=unknown [invariants]=unknown)

# valid_record 检查名 记录：记录合这个检查的格式时为真（维度只能是这个检查的，每个维度至多一次）。
valid_record() {
    local check="$1" value="$2" item seen="," items=()
    case "$value" in none | unknown) return 0 ;; esac
    [[ "$value" =~ ^[a-z]+:P[12](,[a-z]+:P[12])*$ ]] || return 1
    IFS=, read -r -a items <<<"$value"
    for item in "${items[@]}"; do
        case "$check:${item%%:*}" in
            readyz:readyz | readyz:periodic | invariants:invariants) ;;
            *) return 1 ;;
        esac
        [[ "$seen" != *",${item%%:*},"* ]] || return 1
        seen+="${item%%:*},"
    done
}

# 文件不存在、读不了或整份不合格式时两个检查都是未知；只有某个检查那一行缺了、重复或不合格式时只那个检查是未知。
read_state() {
    local lines=() line check value
    local -A found=()
    if [ ! -f "$STATE_FILE" ] || [ ! -r "$STATE_FILE" ]; then
        return 0
    fi
    mapfile -t lines 2>/dev/null <"$STATE_FILE" || return 0
    [ "${lines[0]:-}" = "$STATE_HEADER" ] || return 0
    for line in "${lines[@]:1}"; do
        [[ "$line" =~ ^(readyz|invariants)\ (.*)$ ]] || return 0
        check="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
        if [ -n "${found[$check]:-}" ] || ! valid_record "$check" "$value"; then
            value=unknown
        fi
        found[$check]="$value"
    done
    records[readyz]="${found[readyz]:-unknown}"
    records[invariants]="${found[invariants]:-unknown}"
}

# 目录不存在时建成只有运行账号可读写；先写同目录的临时文件再改名。写不进去只记一行日志。
write_state() {
    local dir tmp=""
    dir="$(dirname -- "$STATE_FILE")"
    if [ -n "${STATE_FILE##*/}" ] && [ ! -d "$STATE_FILE" ] \
        && (umask 077 && mkdir -p -- "$dir") 2>/dev/null \
        && tmp="$(mktemp "$dir/.state.XXXXXX" 2>/dev/null)" \
        && printf '%s\n' "$STATE_HEADER" "readyz ${records[readyz]}" "invariants ${records[invariants]}" \
            >"$tmp" 2>/dev/null \
        && mv -f -- "$tmp" "$STATE_FILE" 2>/dev/null; then
        return 0
    fi
    [ -z "$tmp" ] || rm -f -- "$tmp" 2>/dev/null || true
    log "state file could not be written; the next round may notify again or miss a re-notification"
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
    if [ -z "$CORE_INSTANCE" ]; then
        not_checked_all "found 0 running core_api containers, expected exactly 1"
        return
    fi
    ids="$(core_find_api_containers)" || status=$?
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

# problem_list 维度 正文 …：本轮不合格的维度与级别（级别取正文开头），逗号分隔，合格的维度不列。
problem_list() {
    local list=""
    while [ "$#" -gt 0 ]; do
        [ -z "$2" ] || list+="${list:+,}$1:${2:0:2}"
        shift 2
    done
    printf '%s' "$list"
}

# needs_renotify 记录 本轮的问题：记录未知，或本轮有记录里没有的维度、比记录里高的级别（P1 高于 P2）时为真。
needs_renotify() {
    local record="$1" item old items=()
    [ "$record" != unknown ] || return 0
    [ "$record" != none ] || return 1
    IFS=, read -r -a items <<<"$2"
    for item in "${items[@]}"; do
        case ",$record," in
            *",${item%%:*}:P1,"*) old=P1 ;;
            *",${item%%:*}:P2,"*) old=P2 ;;
            *) return 0 ;;
        esac
        if [ "${item#*:}" = P1 ] && [ "$old" = P2 ]; then
            return 0
        fi
    done
    return 1
}

# send 检查名 心跳地址 通知正文：正文为空时推成功，否则推失败；没推成时记一行日志并返回 1。
send() {
    local check="$1" url="$2" text="$3" target body kind status=0
    if [ -n "$text" ]; then
        target="${url%/}/fail" body="$text" kind=failure
    else
        target="$url" body="ok" kind=success
    fi
    printf '%s\n' "$body" | curl -q -fs --max-time 10 -o /dev/null \
        -H 'Content-Type: text/plain; charset=utf-8' --data-binary @- "$target" 2>/dev/null || status=$?
    if [ "$status" -ne 0 ]; then
        log "heartbeat for $check not delivered ($kind, curl exit $status)"
        return 1
    fi
}

# report 检查名 键名 心跳地址 通知正文 本轮的问题：推这个检查并按结果改它的记录。没配地址时记一行并返回 1。
report() {
    local check="$1" key="$2" url="$3" text="$4" problems="$5" delivered=0
    if [ -z "$url" ]; then
        log "$key is not configured; $check results will not notify anyone"
        return 1
    fi
    if [ -z "$text" ]; then
        if send "$check" "$url" ""; then
            records[$check]=none
        fi
        return 0
    fi
    if needs_renotify "${records[$check]}" "$problems"; then
        log "re-notifying $check: pushing success, then failure"
        send "$check" "$url" "" || delivered=1
    fi
    send "$check" "$url" "$text" || delivered=1
    if [ "$delivered" -eq 0 ]; then
        records[$check]="$problems"
    fi
}

read_env "$ENV_FILE"
if [ -z "$CORE_INSTANCE" ]; then
    log "$CORE_INSTANCE_ERROR; treating the core_api container as not found"
fi

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

# readyz 那个检查的正文：两个维度的问题都写，readyz 在前。
readyz_check_text="$readyz_text"
if [ -n "$periodic_text" ]; then
    readyz_check_text+="${readyz_check_text:+$'\n'}$periodic_text"
fi

read_state
report readyz CORE_HEALTHCHECK_READYZ_URL "$readyz_url" "$readyz_check_text" \
    "$(problem_list readyz "$readyz_text" periodic "$periodic_text")" || exit_status=1
report invariants CORE_HEALTHCHECK_INVARIANTS_URL "$invariants_url" "$invariants_text" \
    "$(problem_list invariants "$invariants_text")" || exit_status=1
write_state

exit "$exit_status"
