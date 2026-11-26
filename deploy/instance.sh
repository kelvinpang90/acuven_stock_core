# shellcheck shell=bash disable=SC2034
#
# 实例片段（docs/PRODUCTION_AND_RECOVERY.md 第 1.1、1.2 节）：同一台服务器上的 Demo 与生产实例只由检出目录 .env 里的
# CORE_ENV 区分。由 deploy/deploy.sh、healthcheck.sh、monitor.sh 与运行手册里的命令 source（不单独执行）：
#     . deploy/instance.sh
#
# source 时只定义变量与函数，配置不对也不退出调用方：那时 CORE_INSTANCE 为空、CORE_INSTANCE_ERROR 写着原因，
# 由调用方决定怎么失败。上面的 disable=SC2034：这里赋的变量由 source 它的脚本用。
#
# .env 固定是本文件所在检出的根目录下的 .env，与当前目录无关；只读 CORE_ENV 一个键，不 source 整个文件：
# 恰好一行以 CORE_ENV= 开头，等号之后整行严格等于 demo 或 production（不去空格、不认引号与行尾注释，行尾带回车也算错）。
#
# 读对时给出（都不是 .env 的键：多一个键就多一种漏配）：
#   CORE_INSTANCE         demo 或 production
#   CORE_COMPOSE_PROJECT  compose 项目名；所有 compose 调用都带 -p "$CORE_COMPOSE_PROJECT"
#   CORE_DATA_NETWORK     数据网络（导出给 docker-compose.yml）
#   CORE_CALLER_NETWORK   Core 对外的那条网络（导出）：Demo 是调用方内部网络 stock_core_net，
#                         生产是边缘反向代理所在的代理网络 proxy_net（生产没有外部调用方）
#   CORE_CALLER_ALIAS     Core 在这条网络上的别名（导出）：Demo 是 core_api，生产是 stock_core_production（vhost 按它转发）
#   CORE_CALLER_NETWORK_KIND  这条网络归谁建（不导出）：internal 是部署脚本确保存在的内部网络（Demo），
#                         external 是 vps_infra 建的外部网络、部署脚本不建（生产）
#   CORE_CHECKOUT_DIR     本检出的根目录（realpath；读不对时也有）

CORE_CHECKOUT_DIR="$(realpath -- "$(dirname -- "${BASH_SOURCE[0]}")/.." 2>/dev/null)" || CORE_CHECKOUT_DIR=""

# core_instance_env_value 键：.env 里恰好一行以“键=”开头时，原样打出等号之后的整行；没有、多于一行或读不了时返回 1。
core_instance_env_value() {
    local key="$1" file="$CORE_CHECKOUT_DIR/.env" line value="" count=0
    if [ -z "$CORE_CHECKOUT_DIR" ] || [ ! -f "$file" ] || [ ! -r "$file" ]; then
        return 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" == "$key="* ]]; then
            count=$((count + 1))
            value="${line#"$key="}"
        fi
    done <"$file" || return 1
    [ "$count" -eq 1 ] || return 1
    printf '%s\n' "$value"
}

core_instance_load() {
    local value data_network caller_network caller_alias caller_kind
    CORE_INSTANCE="" CORE_COMPOSE_PROJECT="" CORE_INSTANCE_ERROR="" CORE_CALLER_NETWORK_KIND=""
    # 调用方环境里留着的旧值不能顶替这里没读对的实例。
    unset CORE_DATA_NETWORK CORE_CALLER_NETWORK CORE_CALLER_ALIAS
    CORE_INSTANCE_ERROR="CORE_ENV in ${CORE_CHECKOUT_DIR:-this checkout}/.env must be exactly one line CORE_ENV=demo or CORE_ENV=production"
    value="$(core_instance_env_value CORE_ENV)" || return 1
    case "$value" in
        demo)
            CORE_COMPOSE_PROJECT=acuven_stock_core
            data_network=data_net caller_network=stock_core_net caller_alias=core_api caller_kind=internal
            ;;
        production)
            CORE_COMPOSE_PROJECT=acuven_stock_core_production
            data_network=infra_mysql_core_production_net
            caller_network=proxy_net caller_alias=stock_core_production caller_kind=external
            ;;
        *)
            CORE_COMPOSE_PROJECT=""
            return 1
            ;;
    esac
    CORE_INSTANCE="$value" CORE_INSTANCE_ERROR="" CORE_CALLER_NETWORK_KIND="$caller_kind"
    export CORE_DATA_NETWORK="$data_network" CORE_CALLER_NETWORK="$caller_network" CORE_CALLER_ALIAS="$caller_alias"
}

# core_find_api_containers：按行打出本实例、本检出的运行中 core_api 容器 ID。可能是 0 行或多行：由调用方计数，
# 不是恰好一个就算没找到。按 compose 项目与服务标签找、排除一次性容器（部署跑迁移用的），
# 再要求标签 com.docker.compose.project.working_dir 与本检出的根目录相同（两边都 realpath）。
# 返回 0：查成了；1：docker ps 失败；2：实例没读对（不调用 docker）；124：docker ps 超过 10 秒。
core_find_api_containers() {
    local listing status=0 id dir real
    [ -n "$CORE_INSTANCE" ] || return 2
    listing="$(timeout 10 docker ps \
        --filter "label=com.docker.compose.project=$CORE_COMPOSE_PROJECT" \
        --filter label=com.docker.compose.service=core_api \
        --filter label=com.docker.compose.oneoff=False \
        --format '{{.ID}} {{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null)" || status=$?
    [ "$status" -ne 124 ] || return 124
    [ "$status" -eq 0 ] || return 1
    while read -r id dir; do
        if [ -z "$id" ] || [ -z "$dir" ]; then
            continue
        fi
        real="$(realpath -- "$dir" 2>/dev/null)" || continue
        if [ "$real" = "$CORE_CHECKOUT_DIR" ]; then
            printf '%s\n' "$id"
        fi
    done <<<"$listing"
    return 0
}

core_instance_load || true
