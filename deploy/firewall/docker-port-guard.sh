#!/usr/bin/env bash
# 宿主机级端口收敛：让 Docker 发布的端口只对本机与容器网段可见。
#
# 为什么需要它：docker publish（-p 0.0.0.0:6379:6379）的转发由 NAT 表的
# DOCKER 链完成，流量走 FORWARD 而非 INPUT，所以 ufw / iptables INPUT 规则
# 对它是无效的。要拦住只能在 DOCKER-USER 链上做——这条链由 docker 创建，
# 且在所有 DOCKER 转发规则之前生效。
#
# 本脚本不改容器、不重启服务，规则即时生效、可即时撤销。
# 它管的是宿主机上所有 Docker 端口，不属于 PAP 业务逻辑；PAP 自己的
# compose 已经把端口绑到 127.0.0.1，不需要这条兜底规则。
#
# 用法（需 root）：
#   deploy/firewall/docker-port-guard.sh status
#   deploy/firewall/docker-port-guard.sh apply
#   deploy/firewall/docker-port-guard.sh remove
#   deploy/firewall/docker-port-guard.sh persist     # 写 systemd 单元，开机自动 apply
#   deploy/firewall/docker-port-guard.sh unpersist
#
# 端口清单可用环境变量覆盖：
#   PORTS=6379,3307 deploy/firewall/docker-port-guard.sh apply

set -euo pipefail

CHAIN="DOCKER-USER"
TAG="pap-port-guard"
PORTS="${PORTS:-6379,3307,15672,5672,25672,4369,8765}"
# 172.16.0.0/12 覆盖 172.17.0.0/16（docker0）与 172.18.0.0/16 等自定义网桥。
DOCKER_NETS="${DOCKER_NETS:-172.16.0.0/12}"
UNIT="/etc/systemd/system/pap-port-guard.service"
SELF="$(readlink -f "$0")"

log() { printf '[port-guard] %s\n' "$*"; }
die() { printf '[port-guard] 错误：%s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行。"

have_chain() { iptables -n -L "$CHAIN" >/dev/null 2>&1; }

require_chain() {
  have_chain || die "链 $CHAIN 不存在——docker 未运行？先 systemctl start docker"
}

cmd_status() {
  echo "端口清单：$PORTS"
  if have_chain; then
    iptables -n -L "$CHAIN" --line-numbers
  else
    echo "链 $CHAIN 不存在"
  fi
  if [ -f "$UNIT" ]; then
    echo "持久化单元：已安装（systemctl is-enabled pap-port-guard）"
  else
    echo "持久化单元：未安装（重启后规则会丢失）"
  fi
}

cmd_apply() {
  require_chain

  if iptables -n -L "$CHAIN" 2>/dev/null | grep -q "$TAG"; then
    log "规则已存在，先清理再重建（保证顺序正确）"
    cmd_remove
  fi

  # 先插 DROP，再把两条放行插到它前面，最终顺序为：
  #   lo RETURN → docker 网段 RETURN → 目标端口 DROP
  iptables -I "$CHAIN" 1 -p tcp -m multiport --dports "$PORTS" \
    -m comment --comment "$TAG-drop" -j DROP
  iptables -I "$CHAIN" 1 -s "$DOCKER_NETS" \
    -m comment --comment "$TAG-net" -j RETURN
  iptables -I "$CHAIN" 1 -i lo \
    -m comment --comment "$TAG-lo" -j RETURN

  log "已收敛端口：$PORTS（仅本机与 $DOCKER_NETS 可访问）"
  cmd_status
}

cmd_remove() {
  have_chain || { log "链 $CHAIN 不存在，无需清理"; return 0; }

  # 倒序删除：每删一条行号会变，反复删第一条直到没有本脚本的规则。
  while iptables -n -L "$CHAIN" 2>/dev/null | grep -q "$TAG"; do
    iptables -D "$CHAIN" 1
  done
  log "已撤销本脚本添加的规则"
}

cmd_persist() {
  [ -x "$SELF" ] || die "$SELF 不可执行"
  cat > "$UNIT" <<EOF
[Unit]
Description=Host-level guard: restrict Docker published ports to localhost and bridge networks
Documentation=file://$SELF
After=docker.service network-online.target
Wants=docker.service
# docker 可能被单独重启并把 DOCKER-USER 链重建，这里跟着重来一次。
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=PORTS=$PORTS
Environment=DOCKER_NETS=$DOCKER_NETS
ExecStart=$SELF apply
ExecStop=$SELF remove

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now pap-port-guard.service >/dev/null
  log "已安装 $UNIT 并启用"
}

cmd_unpersist() {
  systemctl disable --now pap-port-guard.service >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl daemon-reload
  log "已移除持久化单元（当前规则仍在，用 remove 撤销）"
}

case "${1:-status}" in
  status) cmd_status ;;
  apply) cmd_apply ;;
  remove) cmd_remove ;;
  persist) cmd_persist ;;
  unpersist) cmd_unpersist ;;
  *) die "未知子命令：$1（可用：status / apply / remove / persist / unpersist）" ;;
esac
