#!/usr/bin/env bash
#
# 发版入口。默认走 git push：服务器裸仓库的 post-receive 钩子会把工作区
# 自动对齐，所以一次连接就能完成代码上线。
#
#   deploy/sync.sh                推送并校验服务器已对齐
#   deploy/sync.sh --restart      推送 + 重启 + 健康检查
#   deploy/sync.sh --migrate      推送 + 停服迁移 + 重启 + 健康检查
#   deploy/sync.sh --verify       只比对本地提交与服务器工作区
#   deploy/sync.sh --tar          应急：不走 git，用归档覆盖服务器文件
#   deploy/sync.sh --dry-run      说明将要上线的提交，不做任何改动
#
# 环境变量：
#   PAP_REMOTE       默认 root@203.0.113.10
#   PAP_REMOTE_DIR   默认 /personal-agent-platform
#   PAP_SERVICES     默认 "personal-agent-platform.service personal-agent-platform-studio.service"
#
# 为什么默认不用 rsync：这台机器对新建 SSH 连接有速率限制（sshd 未认证连接
# 堆积会被 MaxStartups 丢弃），能少一次连接就少一次。归档模式只在服务器
# git 状态异常时用。
#
set -Eeuo pipefail

REMOTE="${PAP_REMOTE:-root@203.0.113.10}"
REMOTE_DIR="${PAP_REMOTE_DIR:-/personal-agent-platform}"
SERVICES="${PAP_SERVICES:-personal-agent-platform.service personal-agent-platform-studio.service}"
BRANCH="${PAP_BRANCH:-master}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o TCPKeepAlive=yes)

RSYNC_EXCLUDES=(
  --exclude .git --exclude .venv --exclude data --exclude .env
  --exclude .ruff_cache --exclude .workbuddy --exclude __pycache__
)

MODE=push
DO_MIGRATE=0
DO_RESTART=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart) DO_RESTART=1 ;;
    --migrate) DO_MIGRATE=1; DO_RESTART=1 ;;
    --verify) MODE=verify ;;
    --tar) MODE=tar ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
  esac
  shift
done

cd "$(git rev-parse --show-toplevel)"

COMMIT="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"

say() { printf '%s\n' "$*"; }

# 这台机器会短暂拒绝新连接，遇到就退避重试，别让发版半途而废。
ssh_retry() {
  local attempt
  for attempt in 1 2 3; do
    if ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"; then
      return 0
    fi
    [[ $attempt -lt 3 ]] && { say "  连接失败（第 $attempt 次），等 40 秒重试…" >&2; sleep 40; }
  done
  echo "SSH 连续失败，服务器可能在做连接限速；稍后再试。" >&2
  return 1
}

remote_head() {
  ssh_retry "cat $REMOTE_DIR/.deployed-commit 2>/dev/null || echo none" | tr -d '\r' | tail -1
}

# 连接限速也会打在 git push 上（它同样要新建 SSH 连接），
# 只重试 ssh 而漏掉 push 就会出现「校验能过、推不上去」。
push_retry() {
  local attempt
  for attempt in 1 2 3; do
    if git push origin "HEAD:$BRANCH"; then
      return 0
    fi
    [[ $attempt -lt 3 ]] && { say "  推送失败（第 $attempt 次），等 45 秒重试…" >&2; sleep 45; }
  done
  return 1
}

# ---------- 只校验 ----------
if [[ $MODE == verify ]]; then
  say "本地：$BRANCH @ $SHORT"
  ssh_retry "cd $REMOTE_DIR && echo \"工作区：\$(git rev-parse --short HEAD 2>/dev/null || echo none)\" && echo \"部署记录：\$(cat .deployed-commit 2>/dev/null || echo none)\" && echo \"未提交改动：\$(git status --porcelain 2>/dev/null | wc -l) 个文件\""
  exit 0
fi

# ---------- 推送 / 归档 ----------
if [[ -n "$(git status --porcelain)" ]]; then
  say "注意：工作区有未提交改动，它们不会上线（只发布 $SHORT）"
  git status --short | head -10 | sed 's/^/      /'
  say ""
fi

if [[ $DRY_RUN -eq 1 ]]; then
  say "将上线：$BRANCH @ $SHORT"
  say "服务器当前：$(remote_head)"
  git log --oneline origin/$BRANCH..HEAD 2>/dev/null | sed 's/^/  /' || true
  exit 0
fi

if [[ $MODE == tar ]]; then
  say "→ 归档模式：$SHORT"
  TARBALL="$(mktemp -t pap-code-XXXXXX.tar.gz)"
  trap 'rm -f "$TARBALL"' EXIT
  git archive --format=tar.gz -o "$TARBALL" "$COMMIT"
  REMOTE_TAR="/tmp/pap-deploy-$SHORT.tar.gz"
  cat "$TARBALL" | ssh_retry "cat > '$REMOTE_TAR'"
  ssh_retry "set -e; rm -rf /tmp/pap-deploy; mkdir -p /tmp/pap-deploy; tar -xzf '$REMOTE_TAR' -C /tmp/pap-deploy; rsync -a ${RSYNC_EXCLUDES[*]} /tmp/pap-deploy/ '$REMOTE_DIR/'; printf '%s\n' '$COMMIT' > '$REMOTE_DIR/.deployed-commit'; rm -rf /tmp/pap-deploy '$REMOTE_TAR'; echo '覆盖完成'"
else
  say "→ 推送 $SHORT（钩子会把服务器工作区对齐）"
  if ! push_retry; then
    say "推送连续失败。若是连接被拒，用 deploy/sync.sh --tar 走归档模式。" >&2
    exit 1
  fi

  LANDED="$(remote_head)"
  if [[ "$LANDED" != "$COMMIT" ]]; then
    say "服务器部署记录是 $LANDED，与本地 $COMMIT 不一致。" >&2
    say "查看 /var/log/pap-deploy.log，或改用 --tar。" >&2
    exit 1
  fi
  say "  服务器已对齐：$SHORT"
fi

# ---------- 迁移 ----------
if [[ $DO_MIGRATE -eq 1 ]]; then
  say "→ 数据库迁移（先停服；旧代码与新结构不兼容）"
  # 这一步不走 ssh_retry：连接断了可以重连，但迁移失败重试只会重复停服、
  # 又把真正的报错刷掉。失败就停下来让人看。
  # 远端必须 pipefail：之前的 `| tail -2` 让 init_checkpointer 的退出码消失，
  # checkpointer 建表连续多次发版都是静默失败。
  if ! ssh "${SSH_OPTS[@]}" "$REMOTE" "set -euo pipefail; cd $REMOTE_DIR; systemctl stop $SERVICES; ADMIN_URL=\"\$(cat /root/.pap-admin-db-url)\"; DATABASE_URL=\"\$ADMIN_URL\" /root/.local/bin/uv run --no-sync alembic upgrade head 2>&1 | tail -5; /root/.local/bin/uv run --no-sync python -m scripts.init_checkpointer --url \"\$ADMIN_URL\" 2>&1 | tail -5; echo 迁移完成"; then
    say "迁移失败：服务已停止，修好后再跑 deploy/sync.sh --restart。" >&2
    exit 1
  fi
fi

# ---------- 重启 ----------
if [[ $DO_RESTART -eq 1 ]]; then
  say "→ 重启服务"
  ssh_retry "systemctl reset-failed $SERVICES 2>/dev/null; systemctl restart $SERVICES; true"

  say "→ 健康检查"
  ssh_retry "sleep 8; for svc in $SERVICES; do printf '%-46s %s\n' \"\$svc\" \"\$(systemctl is-active \$svc || true)\"; done; printf '%-46s %s\n' /health \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health || echo ERR)\"; printf '%-46s %s\n' /ready \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ready || echo ERR)\""
fi

say ""
say "完成：$REMOTE_DIR 现在是 $SHORT"
