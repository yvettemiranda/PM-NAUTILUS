#!/usr/bin/env bash
# One-command, fail-stop upgrade of a running PM-NAUTILUS Docker Compose server.
#
#   sudo deploy/deploy.sh <full 40-character commit SHA> [--yes]
#
# Order: preflight -> record which TEST accounts are RUNNING -> fetch and check
# out the commit -> build the image while the old container keeps serving ->
# graceful stop -> full copy of the data directory and .env -> switch
# PM_GIT_REVISION -> start -> wait for health and market data -> START exactly
# the accounts that were RUNNING before -> compare statuses and positions.
#
# Any failure stops immediately and prints how to roll back. The script never
# enables LIVE, unlocks a wallet or starts an account that was not running.
# It refuses to run while LIVE is enabled: after a restart the wallet stays
# locked and must be handled by the owner in the web page.

set -Eeuo pipefail

APP_DIR=${PM_APP_DIR:-/opt/pm-nautilus}
BACKUP_ROOT=${PM_BACKUP_ROOT:-/var/lib/pm-nautilus-deploy}
KEEP_BACKUPS=${PM_KEEP_BACKUPS:-5}
HEALTH_TIMEOUT=${PM_HEALTH_TIMEOUT:-240}
MARKET_TIMEOUT=${PM_MARKET_TIMEOUT:-300}
COMPOSE_FILES=${PM_COMPOSE_FILES:-compose.yaml}

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

log() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
ROLLBACK_HINT=""
die() {
    printf '\n!! %s\n' "$*" >&2
    if [ -n "$ROLLBACK_HINT" ]; then
        printf '%s\n' "$ROLLBACK_HINT" >&2
    fi
    exit 1
}

TARGET=""
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        -h | --help) usage 0 ;;
        -y | --yes) ASSUME_YES=1 ;;
        -*) die "未知参数: $arg" ;;
        *)
            [ -z "$TARGET" ] || die "只能指定一个提交"
            TARGET=$arg
            ;;
    esac
done
[ -n "$TARGET" ] || usage 1
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || die "请使用完整的 40 位小写提交 SHA（git rev-parse <版本>）"
[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo deploy/deploy.sh $TARGET"
for tool in docker git curl python3; do
    command -v "$tool" >/dev/null 2>&1 || die "缺少命令: $tool"
done

cd "$APP_DIR" || die "找不到程序目录 $APP_DIR"
[ -f .env ] || die "$APP_DIR/.env 不存在"
[ -d runtime/server ] || die "$APP_DIR/runtime/server 不存在"

GIT=(git -c "safe.directory=$APP_DIR" -C "$APP_DIR")
compose_args=(--env-file .env)
for file in $COMPOSE_FILES; do
    [ -f "$file" ] || die "Compose 文件不存在: $file"
    compose_args+=(-f "$file")
done
compose() { docker compose "${compose_args[@]}" "$@"; }

env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }
PORT=$(env_value PM_PORT)
PORT=${PORT:-8765}
PASSWORD=$(env_value PM_UI_PASSWORD)
[ -n "$PASSWORD" ] || die ".env 中没有 PM_UI_PASSWORD"
HOST=localhost
ALLOWED=$(env_value PM_ALLOWED_HOSTS)
case ",${ALLOWED:-localhost,127.0.0.1}," in
    *,localhost,*) HOST=localhost ;;
    *,127.0.0.1,*) HOST=127.0.0.1 ;;
    *) HOST=${ALLOWED%%,*} ;;
esac
BASE="http://127.0.0.1:$PORT"
CURRENT=$(env_value PM_GIT_REVISION)

# The password reaches curl through stdin, never through argv (visible in ps).
curl_auth() {
    local escaped=${PASSWORD//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    printf 'user = "pm:%s"\n' "$escaped" | curl -sS --max-time 30 -K - -H "Host: $HOST" "$@"
}

json() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }

health() { curl -sS --max-time 10 -H "Host: $HOST" "$BASE/api/health"; }

account_states() {
    # Prints one "number status positionCount" line per TEST account.
    local count=$1 number
    for number in $(seq 1 "$count"); do
        curl_auth "$BASE/api/dashboard?details=summary&account_id=$number" |
            json "print($number, d['strategy']['status'], d['positionCount'])"
    done
}

on_error() {
    local status=$?
    printf '\n!! 部署在第 %s 行失败（退出码 %s）。\n' "${BASH_LINENO[0]}" "$status" >&2
    if [ -n "$ROLLBACK_HINT" ]; then
        printf '%s\n' "$ROLLBACK_HINT" >&2
    fi
    exit "$status"
}
trap on_error ERR

log "1/9 预检"
CURRENT_HEALTH=$(health) || die "当前服务没有响应 $BASE/api/health；请先手动核对"
read -r ACCOUNT_COUNT LIVE_ON MODE < <(printf '%s' "$CURRENT_HEALTH" |
    json "print(d['testAccountCount'], d['liveExecutionEnabled'], d['activeExecutionMode'])")
[ "$LIVE_ON" = "False" ] || die "LIVE 已启用。重启后钱包会锁定，须由你在网页处理；本脚本不在 LIVE 期间部署"
[ "$MODE" = "TEST" ] || die "当前运行模式为 $MODE（不是 TEST）；请先在网页核对后再部署"
info "当前版本: ${CURRENT:-未知}  目标版本: $TARGET  TEST 账户数: $ACCOUNT_COUNT"
[ "$CURRENT" != "$TARGET" ] || die "服务器已经是目标版本"
DATA_KB=$(du -sk runtime/server | cut -f1)
FREE_KB=$(df -Pk "$(dirname "$BACKUP_ROOT")" | awk 'NR==2 {print $4}')
NEED_KB=$((DATA_KB * 2 + 3 * 1024 * 1024))
[ "$FREE_KB" -gt "$NEED_KB" ] || die "磁盘空间不足：需要约 $((NEED_KB / 1024)) MB，可用 $((FREE_KB / 1024)) MB"

log "2/9 当前账户状态（停机前会再记录一次）"
PREVIEW=$(account_states "$ACCOUNT_COUNT")
printf '%s\n' "$PREVIEW" | sed 's/^/    /'
info "当前运行中的账户: $(printf '%s\n' "$PREVIEW" | awk '$2 == "RUNNING" {print $1}' | xargs)"

if [ "$ASSUME_YES" -ne 1 ]; then
    printf '\n继续部署 %s 吗？服务会停机约 1–2 分钟。输入 yes 确认: ' "$TARGET"
    read -r answer || answer=""
    [ "$answer" = "yes" ] || die "已取消，未做任何改动"
fi

log "3/9 获取并检出目标版本"
"${GIT[@]}" fetch -q origin
"${GIT[@]}" cat-file -e "$TARGET^{commit}" 2>/dev/null || die "远端没有提交 $TARGET"
if ! "${GIT[@]}" merge-base --is-ancestor "$TARGET" origin/main; then
    info "注意：$TARGET 不在 origin/main 上"
fi
PREVIOUS_HEAD=$("${GIT[@]}" rev-parse HEAD)
"${GIT[@]}" checkout -q --detach "$TARGET"
ROLLBACK_HINT="    还没有停机。恢复源码: sudo git -C $APP_DIR checkout --detach $PREVIOUS_HEAD"

log "4/9 构建镜像（旧版本继续运行）"
PM_GIT_REVISION=$TARGET compose build

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$BACKUP_ROOT/$STAMP-${TARGET:0:7}"
log "5/9 记录停机前账户状态，停机并完整备份到 $BACKUP"
BEFORE=$(account_states "$ACCOUNT_COUNT")
printf '%s\n' "$BEFORE" | sed 's/^/    /'
RUNNING=$(printf '%s\n' "$BEFORE" | awk '$2 == "RUNNING" {print $1}' | xargs)
info "部署后将恢复运行的账户: ${RUNNING:-无}"
mkdir -p "$BACKUP"
chmod 700 "$BACKUP_ROOT" "$BACKUP"
compose stop
ROLLBACK_HINT="    服务已停止。数据目录未被改动，恢复旧版本:
      sudo git -C $APP_DIR checkout --detach $PREVIOUS_HEAD
      sudo sed -i 's/^PM_GIT_REVISION=.*/PM_GIT_REVISION=$CURRENT/' $APP_DIR/.env
      cd $APP_DIR && sudo docker compose ${compose_args[*]} up -d
    备份: $BACKUP"
cp -a runtime/server "$BACKUP/runtime-server"
cp -a .env "$BACKUP/env.before"
printf '%s\n' "$CURRENT" >"$BACKUP/previous-revision"
printf '%s\n' "$BEFORE" >"$BACKUP/accounts-before"
[ "$(du -sk "$BACKUP/runtime-server" | cut -f1)" -ge $((DATA_KB * 95 / 100)) ] ||
    die "备份大小异常，已停止（服务保持停止，见上方回滚步骤）"

log "6/9 切换版本并启动"
sed -i "s/^PM_GIT_REVISION=.*/PM_GIT_REVISION=$TARGET/" .env
chown --reference="$BACKUP/env.before" .env
chmod --reference="$BACKUP/env.before" .env
compose up -d
ROLLBACK_HINT="    新版本已启动但核对未通过。回到旧版本（旧镜像仍在本机）:
      sudo sed -i 's/^PM_GIT_REVISION=.*/PM_GIT_REVISION=$CURRENT/' $APP_DIR/.env
      sudo git -C $APP_DIR checkout --detach $PREVIOUS_HEAD
      cd $APP_DIR && sudo docker compose ${compose_args[*]} up -d
    新版本已运行过，账本可能已有新写入；不要直接用备份覆盖，先核对。备份: $BACKUP"

log "7/9 等待健康检查（最长 ${HEALTH_TIMEOUT}s）"
deadline=$((SECONDS + HEALTH_TIMEOUT))
until NEW_HEALTH=$(health 2>/dev/null) &&
    printf '%s' "$NEW_HEALTH" | json "
ok = d['revision'] == '$TARGET' and d['availableTestAccountCount'] == $ACCOUNT_COUNT
sys.exit(0 if ok and d['status'] in ('ok', 'attention') else 1)" 2>/dev/null; do
    [ "$SECONDS" -lt "$deadline" ] || die "健康检查超时；最后响应: ${NEW_HEALTH:0:300}"
    sleep 3
done
printf '%s' "$NEW_HEALTH" | json "print('    状态', d['status'], '版本', d['revision'][:7], '可用账户', d['availableTestAccountCount'], '后台错误', d['backgroundErrors'])"

RESTARTED=$(account_states "$ACCOUNT_COUNT")
printf '%s\n' "$RESTARTED" >"$BACKUP/accounts-restarted"
if ! printf '%s\n' "$RESTARTED" | awk '$2 != "PAUSED" {exit 1}'; then
    die "重启后有账户不是 PAUSED，与程序设计不符，未恢复任何账户：
$RESTARTED"
fi
if [ "$(printf '%s\n' "$BEFORE" | awk '{print $1, $3}')" != "$(printf '%s\n' "$RESTARTED" | awk '{print $1, $3}')" ]; then
    info "注意：持仓数与停机前不同（暂停的账户仍会维护卖出和结算），请在页面核对:"
    paste <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$RESTARTED") | sed 's/^/      停机前 | 重启后: /'
fi

log "8/9 恢复部署前运行中的账户: ${RUNNING:-无}"
if [ -n "$RUNNING" ]; then
    CSRF=$(curl_auth -D - -o /dev/null "$BASE/api/dashboard?details=summary" |
        awk 'tolower($1) == "x-pm-csrf:" {print $2}' | tr -d '\r')
    [ -n "$CSRF" ] || die "无法取得控制请求令牌"
    for number in $RUNNING; do
        result=$(curl_auth -X POST -H "x-pm-csrf: $CSRF" "$BASE/api/test/start?account_id=$number" |
            json "print(d.get('strategy', {}).get('status'), d.get('error'))")
        info "账户 $number: $result"
    done
fi

log "9/9 核对"
AFTER=$(account_states "$ACCOUNT_COUNT")
printf '%s\n' "$AFTER" >"$BACKUP/accounts-after"
printf '%s\n' "$AFTER" | sed 's/^/    /'
if [ "$(printf '%s\n' "$AFTER" | awk '{print $1, $2}')" != "$(printf '%s\n' "$BEFORE" | awk '{print $1, $2}')" ]; then
    die "账户运行状态与停机前不一致（新版本已运行，未回滚），请在页面核对"
fi
info "全部 $ACCOUNT_COUNT 个账户的运行状态与停机前一致"
deadline=$((SECONDS + MARKET_TIMEOUT))
until health | json "
sys.exit(0 if all(v['status'] == 'ok' for v in d['marketData'].values()) else 1)" 2>/dev/null; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        info "注意：行情在 ${MARKET_TIMEOUT}s 内尚未全部就绪，请稍后查看健康接口"
        break
    fi
    sleep 5
done
health | json "
for k, v in d['marketData'].items():
    print('   ', k, v['status'], '行情组', f\"{v['readyGroupCount']}/{v['groupCount']}\", '盘口', f\"{v['readyBookCount']}/{v['tokenCount']}\")"

trap - ERR
# Keep the newest deploy backups only; never touch other directories.
mapfile -t old < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort -r |
    tail -n +"$((KEEP_BACKUPS + 1))")
for dir in "${old[@]}"; do
    rm -rf -- "$dir"
    info "已清理旧部署备份 $dir"
done

log "完成：$CURRENT -> $TARGET"
info "备份: $BACKUP"
info "回滚旧版本: sudo deploy/deploy.sh $CURRENT（会再做一次备份）"
info "注意：旧版本不一定能读取新版本写过的账本，回滚前先看版本说明。"
