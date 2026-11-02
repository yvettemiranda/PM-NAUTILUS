#!/usr/bin/env bash
# One-command, fail-stop upgrade of a running PM-NAUTILUS Docker Compose server.
#
#   sudo deploy/deploy.sh <full 40-character commit SHA> [--yes] [--allow-unverified]
#
# Order: preflight -> record which TEST accounts are RUNNING -> fetch and check
# out the commit -> build the image while the old container keeps serving ->
# graceful stop -> full copy of the data directory and .env -> switch
# PM_GIT_REVISION -> start -> wait for health and market data -> START exactly
# the accounts that were RUNNING before -> compare statuses and positions.
#
# Any failure stops immediately and prints how to roll back. The script never
# enables LIVE, unlocks a wallet or starts an account that was not running.
# It refuses to run while LIVE is enabled or a wallet is unlocked (checked at
# the start and again immediately before the stop). It builds only a clean
# checkout of exactly the target commit, and refuses a commit that is not on
# origin/main or whose GitHub CI for that exact SHA has not fully passed,
# unless --allow-unverified is given.

set -Eeuo pipefail

APP_DIR=${PM_APP_DIR:-/opt/pm-nautilus}
BACKUP_ROOT=${PM_BACKUP_ROOT:-/var/lib/pm-nautilus-deploy}
KEEP_BACKUPS=${PM_KEEP_BACKUPS:-5}
HEALTH_TIMEOUT=${PM_HEALTH_TIMEOUT:-240}
MARKET_TIMEOUT=${PM_MARKET_TIMEOUT:-300}
COMPOSE_FILES=${PM_COMPOSE_FILES:-compose.yaml}
GITHUB_REPO=${PM_GITHUB_REPO:-yvettemiranda/PM-NAUTILUS}

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
ALLOW_UNVERIFIED=0
for arg in "$@"; do
    case "$arg" in
        -h | --help) usage 0 ;;
        -y | --yes) ASSUME_YES=1 ;;
        --allow-unverified) ALLOW_UNVERIFIED=1 ;;
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
for tool in docker git curl python3 flock; do
    command -v "$tool" >/dev/null 2>&1 || die "缺少命令: $tool"
done

# One deploy at a time: a second concurrent run would interleave stop/backup/start.
exec 9>"${PM_DEPLOY_LOCK:-/run/lock/pm-nautilus-deploy.lock}"
flock -n 9 || die "另一个部署正在进行，本次未做任何改动"

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

# A restart locks the wallet: refuse while LIVE runs or a wallet is unlocked.
# Called at preflight and again immediately before the stop, after the build.
require_safe_to_stop() {
    local state live mode wallet
    state=$(health) || die "当前服务没有响应 $BASE/api/health；请先手动核对"
    read -r live mode wallet < <(printf '%s' "$state" |
        json "print(d['liveExecutionEnabled'], d['activeExecutionMode'], d.get('liveWalletStatus'))")
    [ "$live" = "False" ] || die "LIVE 已启用。重启后钱包会锁定，须由你在网页处理；本脚本不在 LIVE 期间部署"
    [ "$mode" = "TEST" ] || die "当前运行模式为 $mode（不是 TEST）；请先在网页核对后再部署"
    case "$wallet" in
        UNCONFIGURED | LOCKED) ;;
        *) die "LIVE 钱包当前状态为 $wallet（已解锁或异常）。重启会锁定钱包，请先在网页锁定或处理后再部署" ;;
    esac
}

# Every GitHub check run for this exact SHA must be completed and successful,
# including both CI architectures. Prints a one-line summary.
ci_passed() {
    curl -sS --max-time 20 -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$GITHUB_REPO/commits/$TARGET/check-runs?per_page=100" |
        json "
runs = d['check_runs']
verify = [r for r in runs if r['name'].startswith('verify')]
ok = len(verify) >= 2 and all(r['status'] == 'completed' and r['conclusion'] == 'success' for r in runs)
print(f\"{sum(r['conclusion'] == 'success' for r in runs)}/{len(runs)} 项检查通过\")
sys.exit(0 if ok else 1)"
}

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
require_safe_to_stop
ACCOUNT_COUNT=$(health | json "print(d['testAccountCount'])")
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
# Uncommitted or untracked source would enter the image under the target's name.
DIRTY=$("${GIT[@]}" status --porcelain --untracked-files=all)
[ -z "$DIRTY" ] || die "程序目录有未提交或未跟踪的文件，镜像将与目标版本不一致；请先核对:
$DIRTY"
"${GIT[@]}" fetch -q origin
"${GIT[@]}" cat-file -e "$TARGET^{commit}" 2>/dev/null || die "远端没有提交 $TARGET"
UNVERIFIED=""
"${GIT[@]}" merge-base --is-ancestor "$TARGET" origin/main || UNVERIFIED="不在 origin/main 上"
if [ -z "$UNVERIFIED" ]; then
    if CI_SUMMARY=$(ci_passed 2>/dev/null); then
        info "GitHub CI（此提交）: $CI_SUMMARY"
    else
        UNVERIFIED="GitHub CI 未全部通过或无法确认（${CI_SUMMARY:-无响应}）"
    fi
fi
if [ -n "$UNVERIFIED" ]; then
    [ "$ALLOW_UNVERIFIED" -eq 1 ] ||
        die "$TARGET $UNVERIFIED。等 CI 通过后再部署；确需部署请加 --allow-unverified"
    info "注意：$TARGET $UNVERIFIED（已用 --allow-unverified 确认）"
fi
PREVIOUS_HEAD=$("${GIT[@]}" rev-parse HEAD)
"${GIT[@]}" checkout -q --detach "$TARGET"
[ "$("${GIT[@]}" rev-parse HEAD)" = "$TARGET" ] || die "检出后的版本不是 $TARGET"
[ -z "$("${GIT[@]}" status --porcelain --untracked-files=all)" ] || die "检出后程序目录不干净"
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
# Re-check as the last step before stopping: LIVE or a wallet may have
# changed during the build or the account queries.
require_safe_to_stop
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
# Compare with the stopped data directory: a graceful stop checkpoints and
# removes SQLite WAL files, so the pre-stop size is legitimately larger.
STOPPED_KB=$(du -sk runtime/server | cut -f1)
BACKUP_KB=$(du -sk "$BACKUP/runtime-server" | cut -f1)
SOURCE_FILES=$(find runtime/server -type f | wc -l)
BACKUP_FILES=$(find "$BACKUP/runtime-server" -type f | wc -l)
info "备份 ${BACKUP_KB} KB / ${BACKUP_FILES} 个文件（停机后数据目录 ${STOPPED_KB} KB / ${SOURCE_FILES} 个文件）"
[ "$BACKUP_FILES" -eq "$SOURCE_FILES" ] && [ "$BACKUP_KB" -ge $((STOPPED_KB * 99 / 100)) ] ||
    die "备份与停机后的数据目录不一致，已停止（服务保持停止，见下方回滚步骤）"
# Only verified backups carry this marker; pruning counts and deletes only them.
touch "$BACKUP/.complete"

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
# Keep the newest verified deploy backups only. Incomplete directories from a
# failed run are never counted or deleted here; they are left for inspection.
mapfile -t old < <(find "$BACKUP_ROOT" -mindepth 2 -maxdepth 2 -name .complete -printf '%h\n' |
    grep -E '/20[^/]*$' | sort -r | tail -n +"$((KEEP_BACKUPS + 1))")
for dir in "${old[@]}"; do
    rm -rf -- "$dir"
    info "已清理旧部署备份 $dir"
done

log "完成：$CURRENT -> $TARGET"
info "备份: $BACKUP"
info "回滚旧版本: sudo deploy/deploy.sh $CURRENT（会再做一次备份）"
info "注意：旧版本不一定能读取新版本写过的账本，回滚前先看版本说明。"
