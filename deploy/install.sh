#!/usr/bin/env bash
# 安装/更新部署资产：systemd 单元 + 巡检定时器 + Nginx 站点。
#
# 用法（需 root）：
#   PAP_USER=pap-admin PAP_PASSWORD='<强密码>' bash deploy/install.sh
#
# 幂等：重复执行只会覆盖配置并 reload，不会重复建账号或清空 htpasswd；
# 巡检配置 /etc/pap-watchdog.env 只在缺失时生成，不会覆盖已有告警地址。
# 不做的事：不建库、不跑迁移、不写 .env、不装依赖——那些属于部署流程，
# 见 docs/DEPLOYMENT.md，避免脚本悄悄改动生产状态。

set -euo pipefail

APP_DIR="${APP_DIR:-/personal-agent-platform}"
UNIT_SRC="$APP_DIR/deploy/systemd/personal-agent-platform.service"
UNIT_DST="/etc/systemd/system/personal-agent-platform.service"
NGINX_SRC="$APP_DIR/deploy/nginx/personal-agent-platform.conf"
NGINX_DST="/etc/nginx/sites-available/personal-agent-platform"
HTPASSWD="/etc/nginx/pap.htpasswd"
PAP_USER="${PAP_USER:-}"
PAP_PASSWORD="${PAP_PASSWORD:-}"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] 错误：%s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行。"
[ -f "$UNIT_SRC" ] || die "找不到 $UNIT_SRC，请确认 APP_DIR=$APP_DIR"
[ -f "$APP_DIR/.env" ] || die "缺少 $APP_DIR/.env，先按 docs/DEPLOYMENT.md 准备配置"

# ---- systemd ----
log "安装 systemd 单元 → $UNIT_DST"
install -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable personal-agent-platform >/dev/null
systemctl restart personal-agent-platform
sleep 3
systemctl is-active --quiet personal-agent-platform \
  || die "服务未启动，查看：journalctl -u personal-agent-platform -n 50 --no-pager"
log "服务已启动并通过 is-active 检查"

# ---- LangGraph Studio（可选，PAP_INSTALL_STUDIO=1 时安装）----
# 只监听 127.0.0.1:2024，跨机使用必须走 SSH 隧道，见 docs/STUDIO.md。
if [ "${PAP_INSTALL_STUDIO:-0}" = "1" ]; then
  STUDIO_SRC="$APP_DIR/deploy/systemd/personal-agent-platform-studio.service"
  STUDIO_DST="/etc/systemd/system/personal-agent-platform-studio.service"

  if [ ! -f "$STUDIO_SRC" ]; then
    log "跳过 Studio：找不到 $STUDIO_SRC"
  elif ! /root/.local/bin/uv run --no-sync python -c "import langgraph_api" >/dev/null 2>&1; then
    log "跳过 Studio：未安装 studio 依赖组，先执行 /root/.local/bin/uv sync --group studio"
  else
    log "安装 Studio 单元 → $STUDIO_DST"
    install -m 0644 "$STUDIO_SRC" "$STUDIO_DST"
    systemctl daemon-reload
    systemctl enable personal-agent-platform-studio >/dev/null
    systemctl restart personal-agent-platform-studio
    sleep 3
    systemctl is-active --quiet personal-agent-platform-studio \
      && log "Studio 已在 127.0.0.1:2024 启动（仅回环，需 SSH 隧道接入）" \
      || log "警告：Studio 未启动，查看 systemctl status personal-agent-platform-studio"
  fi
fi

# ---- 代码沙箱（PAP_INSTALL_SANDBOX=0 可跳过）----
# 沙箱启动器是仓库外的一份副本（/opt/pap-sandbox），历史上靠手工 scp，
# 结果 server.py 停留在旧版本：缺平台注入的 X-Sandbox-User/Conversation 归属校验。
# 现在纳入本脚本，跟随每次部署一起刷新。
#
# 只同步启动器源码与单元；运行镜像（pap-sandbox-runtime:1）与 runsc 属于
# 一次性准备，见 deploy/sandbox/build-image.sh、install-runsc.sh。
if [ "${PAP_INSTALL_SANDBOX:-1}" = "1" ]; then
  SBX_UNIT_SRC="$APP_DIR/deploy/systemd/pap-sandbox.service"
  SBX_UNIT_DST="/etc/systemd/system/pap-sandbox.service"
  SBX_DIR="/opt/pap-sandbox"
  SBX_PORT=8199

  if [ ! -f "$SBX_UNIT_SRC" ]; then
    log "跳过沙箱：找不到 $SBX_UNIT_SRC"
  elif ! id -u sandbox >/dev/null 2>&1; then
    log "跳过沙箱：系统用户 sandbox 不存在（先跑 deploy/sandbox/install-runsc.sh）"
  elif [ ! -x "$SBX_DIR/.venv/bin/uvicorn" ]; then
    log "跳过沙箱：$SBX_DIR/.venv 未就绪（先跑 deploy/sandbox/install-runsc.sh）"
  else
    install -m 0644 "$SBX_UNIT_SRC" "$SBX_UNIT_DST"
    install -d -m 0755 "$SBX_DIR"

    # 工作区文件可能因 core.autocrlf 带着 CRLF；Python 虽能容忍，但会让
    # 版本比对（md5）与实际内容对不上，这里统一去掉行尾 CR。
    # records.py：执行记录直写 PG 的独立 SQL 层；缺它时配了 DSN 会启动失败。
    for f in server.py runtime.py records.py; do
      sed 's/\r$//' "$APP_DIR/deploy/sandbox/$f" > "$SBX_DIR/$f.tmp"
      install -o sandbox -g sandbox -m 0644 "$SBX_DIR/$f.tmp" "$SBX_DIR/$f"
      rm -f "$SBX_DIR/$f.tmp"
    done
    log "已同步沙箱启动器 → $SBX_DIR"

    if ! docker image inspect pap-sandbox-runtime:1 >/dev/null 2>&1; then
      log "警告：镜像 pap-sandbox-runtime:1 不存在，执行会失败（跑 deploy/sandbox/build-image.sh）"
    fi

    systemctl daemon-reload
    systemctl enable pap-sandbox >/dev/null
    systemctl restart pap-sandbox
    sleep 3

    if systemctl is-active --quiet pap-sandbox; then
      health=$(curl -s -m 5 "http://127.0.0.1:$SBX_PORT/v1/health" || true)
      case "$health" in
        *'"ready":true'*) log "沙箱已在 127.0.0.1:$SBX_PORT 就绪" ;;
        *) log "警告：沙箱进程在但健康检查异常：${health:-无响应}" ;;
      esac
    else
      log "警告：沙箱未启动，查看 systemctl status pap-sandbox"
    fi
  fi
fi

# ---- 巡检告警（PAP_INSTALL_WATCHDOG=0 可跳过）----
# 每分钟跑一次 scripts/watchdog.py，用系统 python，不依赖项目 venv。
if [ "${PAP_INSTALL_WATCHDOG:-1}" = "1" ]; then
  WD_SERVICE="$APP_DIR/deploy/systemd/pap-watchdog.service"
  WD_TIMER="$APP_DIR/deploy/systemd/pap-watchdog.timer"
  WD_ENV="/etc/pap-watchdog.env"

  if [ ! -f "$WD_SERVICE" ] || [ ! -f "$WD_TIMER" ]; then
    log "跳过巡检：找不到 pap-watchdog 单元文件"
  else
    log "安装巡检单元与定时器"
    install -m 0644 "$WD_SERVICE" /etc/systemd/system/pap-watchdog.service
    install -m 0644 "$WD_TIMER" /etc/systemd/system/pap-watchdog.timer

    # 状态目录存「连续失败次数 + 上次报警时间」，删了只会多重报一次。
    install -d -m 0750 /var/lib/pap-watchdog

    # 配置文件只在缺失时铺一份模板，避免覆盖已有的告警地址。
    if [ -f "$WD_ENV" ]; then
      log "已存在 $WD_ENV，保留现有告警配置"
    elif [ -f "$APP_DIR/deploy/pap-watchdog.env.example" ]; then
      install -m 0600 "$APP_DIR/deploy/pap-watchdog.env.example" "$WD_ENV"
      log "已生成 $WD_ENV（未配置告警通道时只写 journal）"
    fi

    systemctl daemon-reload
    systemctl enable --now pap-watchdog.timer >/dev/null

    # 立刻跑一次。--strict 让退出码反映真实结果，否则脚本恒为 0，
    # 这一步就变成了「无论如何都打印通过」。
    if /usr/bin/python3 "$APP_DIR/scripts/watchdog.py" --no-alert --strict >/dev/null 2>&1; then
      log "巡检首跑通过"
    else
      log "警告：巡检首跑有失败项，查看：python3 $APP_DIR/scripts/watchdog.py"
    fi
  fi
fi

# ---- Nginx（未安装则跳过）----
if ! command -v nginx >/dev/null 2>&1; then
  log "未检测到 nginx，跳过反向代理安装（应用仍只监听 127.0.0.1）"
  exit 0
fi

[ -f "$NGINX_SRC" ] || die "找不到 $NGINX_SRC"
log "安装 Nginx 站点 → $NGINX_DST"
install -m 0644 "$NGINX_SRC" "$NGINX_DST"
ln -sf "../sites-available/personal-agent-platform" /etc/nginx/sites-enabled/personal-agent-platform
rm -f /etc/nginx/sites-enabled/default

if [ ! -f "$HTPASSWD" ]; then
  [ -n "$PAP_USER" ] && [ -n "$PAP_PASSWORD" ] \
    || die "首次安装需要 PAP_USER 与 PAP_PASSWORD 来创建 $HTPASSWD"
  command -v htpasswd >/dev/null 2>&1 \
    || die "缺少 htpasswd，请先 apt-get install -y apache2-utils"
  htpasswd -cb "$HTPASSWD" "$PAP_USER" "$PAP_PASSWORD"
  chmod 640 "$HTPASSWD"
  chown root:www-data "$HTPASSWD"
  log "已创建基础认证账号：$PAP_USER"
else
  log "已存在 $HTPASSWD，保留现有账号（新增用户：htpasswd $HTPASSWD <subject>）"
fi

nginx -t
systemctl reload nginx
log "Nginx 已重载"

# ---- 自检 ----
code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1/health || true)
if [ "$code" = "200" ]; then
  log "自检通过：/health 经 Nginx 返回 200"
else
  log "警告：/health 经 Nginx 返回 $code，请检查 nginx 配置与上游端口"
fi

log "完成。外部访问前请确认 AUTH_MODE=gateway（见 docs/DEPLOYMENT.md 第 1 节）"
