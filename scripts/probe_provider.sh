#!/usr/bin/env bash
#
# 模型供应商连通性诊断。
#
# 为什么需要它：控制台的「发现模型」由**平台服务所在的那台机器**发起请求，
# 不是浏览器发出的。所以在自己电脑上能打开 http://内网IP/v1/models，
# 平台照样超时。这个脚本要在「平台所在的机器」上跑，结论才有意义。
#
# 用法：
#   ./probe_provider.sh direct   http://203.0.113.20:8083/v1 --key sk-xxx
#   ./probe_provider.sh platform http://203.0.113.20:8083/v1 --key sk-xxx
#   ./probe_provider.sh tunnel      # 只打印反向隧道命令
#
# 不把 Key 写进命令历史：export PROVIDER_API_KEY=sk-xxx 后直接省略 --key。
#
# 退出码：0 可用 / 2 网络不可达 / 3 鉴权失败 / 4 响应异常 / 5 参数错误
#
set -uo pipefail

RED=''; GREEN=''; YELLOW=''; DIM=''; RESET=''
if [ -t 1 ]; then
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
  DIM=$'\033[2m'; RESET=$'\033[0m'
fi

CONNECT_TIMEOUT=5
TOTAL_TIMEOUT=20
API_KEY="${PROVIDER_API_KEY:-}"
PLATFORM_URL="${PAP_URL:-http://127.0.0.1:8000}"
PLATFORM_SUBJECT="${PAP_SUBJECT:-pap-admin}"
PLATFORM_TENANT="${PAP_TENANT:-default}"
META_MARK='__PAP_META__'

say()   { printf '%s\n' "$*"; }
ok()    { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
bad()   { printf '%s✗%s %s\n' "$RED" "$RESET" "$*"; }
warn()  { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
note()  { printf '%s%s%s\n' "$DIM" "$*" "$RESET"; }
head_() { printf '\n%s== %s ==%s\n' "$DIM" "$*" "$RESET"; }

usage() {
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
  exit 0
}

# ---------------------------------------------------------------- 参数解析

MODE="${1:-}"; shift || true
case "$MODE" in
  direct|platform|tunnel) ;;
  ""|-h|--help|help) usage ;;
  *) bad "未知子命令：$MODE（可用 direct / platform / tunnel）"; exit 5 ;;
esac

BASE_URL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --key)       API_KEY="${2:-}"; shift 2 ;;
    --key=*)     API_KEY="${1#*=}"; shift ;;
    --timeout)   TOTAL_TIMEOUT="${2:-20}"; shift 2 ;;
    --platform)  PLATFORM_URL="${2:-}"; shift 2 ;;
    --subject)   PLATFORM_SUBJECT="${2:-}"; shift 2 ;;
    --tenant)    PLATFORM_TENANT="${2:-}"; shift 2 ;;
    -h|--help)   usage ;;
    -*)          bad "未知参数：$1"; exit 5 ;;
    *)           BASE_URL="$1"; shift ;;
  esac
done

if ! command -v curl >/dev/null 2>&1; then
  bad "缺少 curl，先装：apt-get install -y curl"
  exit 5
fi

# ---------------------------------------------------------------- 隧道提示

if [ "$MODE" = "tunnel" ]; then
  cat <<'TIP'
反向隧道：在**能访问内网模型服务**的那台机器上执行（不是平台服务器上）

  ssh -N -R 127.0.0.1:18083:203.0.113.20:8083 \
      -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
      root@203.0.113.10

  # 断线自动重连：
  autossh -M 0 -N -R 127.0.0.1:18083:203.0.113.20:8083 \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      root@203.0.113.10

建好后平台里的 Base URL 填：http://127.0.0.1:18083/v1

隧道监听的是服务器回环地址，只有本机的平台服务能访问，公网打不进来。
验证：服务器上 `ss -ltnp | grep 18083` 应看到 127.0.0.1:18083。
TIP
  exit 0
fi

# ---------------------------------------------------------------- 地址整理

[ -z "$BASE_URL" ] && BASE_URL="${PROVIDER_BASE_URL:-}"
if [ -z "$BASE_URL" ]; then
  bad "缺少 Base URL。示例：./probe_provider.sh $MODE http://203.0.113.20:8083/v1"
  exit 5
fi

BASE_URL="${BASE_URL%/}"
if [ "${BASE_URL%/models}" = "$BASE_URL" ]; then
  ENDPOINT="$BASE_URL/models"
else
  ENDPOINT="$BASE_URL"
fi

parse_url() {
  local url="$1"
  if [[ $url =~ ^(https?)://([^/:]+)(:([0-9]+))?(/.*)?$ ]]; then
    SCHEME="${BASH_REMATCH[1]}"
    HOST="${BASH_REMATCH[2]}"
    PORT="${BASH_REMATCH[4]:-}"
    [ -z "$PORT" ] && { [ "$SCHEME" = "https" ] && PORT=443 || PORT=80; }
    return 0
  fi
  return 1
}

if ! parse_url "$ENDPOINT"; then
  bad "看不懂这个地址：$ENDPOINT（要 http:// 或 https:// 开头）"
  exit 5
fi

mask_key() {
  local k="$1"
  if [ -z "$k" ]; then printf '(未设置)'; return; fi
  printf '%s…(%d 位)' "${k:0:4}" "${#k}"
}

# 默认绕过环境里的 HTTP_PROXY：走代理量的是代理的连通性，不是这台机器到供应商的。
# 平台侧的 httpx 默认 trust_env=True，会读同一批变量——两边可能不一致，所以要察觉。
NOPROXY=("--noproxy" "*")
if [ -n "${HTTP_PROXY:-}${http_proxy:-}${HTTPS_PROXY:-}${https_proxy:-}" ]; then
  PROXY_ENV=1
fi

json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

is_private_ip() {
  local ip="$1"
  [[ $ip =~ ^10\. ]] && return 0
  [[ $ip =~ ^192\.168\. ]] && return 0
  [[ $ip =~ ^127\. ]] && return 0
  if [[ $ip =~ ^172\.([0-9]+)\. ]]; then
    local oct="${BASH_REMATCH[1]}"
    [ "$oct" -ge 16 ] && [ "$oct" -le 31 ] && return 0
  fi
  return 1
}

host_ip() {
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -z "$ip" ] && ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
  printf '%s' "${ip:-未知}"
}

# ---------------------------------------------------------------- 平台模式

if [ "$MODE" = "platform" ]; then
  head_ "经平台探测"
  say "  POST $PLATFORM_URL/api/v1/admin/model-providers/discover"
  say "  身份 $PLATFORM_SUBJECT / 租户 $PLATFORM_TENANT"
  note "  这条走平台服务自己的网络出口，与页面上的「发现模型」完全等价。"

  if [ -n "$API_KEY" ]; then
    key_json="\"$(json_str "$API_KEY")\""
  else
    key_json="null"
  fi
  body="{\"base_url\":\"$(json_str "$BASE_URL")\",\"api_key\":$key_json}"

  raw=$(curl -sS "${NOPROXY[@]}" -m "$((TOTAL_TIMEOUT + 30))" -X POST \
    -H 'Content-Type: application/json' \
    -H "X-Authenticated-Subject: $PLATFORM_SUBJECT" \
    -H "X-Authenticated-Tenant: $PLATFORM_TENANT" \
    -d "$body" \
    -w "\n$META_MARK%{http_code}" \
    "$PLATFORM_URL/api/v1/admin/model-providers/discover")
  code="${raw##*$META_MARK}"
  payload="${raw%$'\n'$META_MARK*}"

  case "$code" in
    200)
      ok "平台探测成功"
      say "  $payload" ;;
    400|401|403|404|422)
      bad "平台返回 $code"
      say "  $payload"
      exit 3 ;;
    000|"")
      bad "连平台自己都连不上（$PLATFORM_URL）"
      note "  查：systemctl status personal-agent-platform"
      exit 2 ;;
    *)
      bad "平台返回 $code"; say "  $payload"
      exit 4 ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------- 直连模式

say ""
say "  主机     $(hostname)  $(host_ip)"
say "  Base URL $BASE_URL"
say "  探测     $ENDPOINT"
say "  API Key  $(mask_key "$API_KEY")"

if is_private_ip "$HOST"; then
  warn "$HOST 是内网地址：只有本机能路由到它时才通。"
fi
if [ "${PROXY_ENV:-0}" = "1" ]; then
  note "  检测到 *_PROXY 环境变量；本脚本已绕过代理直连。平台侧 httpx 默认会走代理，结果可能不同。"
fi

NET_OK=1

# 1. DNS
head_ "1/3 DNS"
if [[ $HOST =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  note "  纯 IP，跳过解析"
else
  resolved=$(getent hosts "$HOST" 2>/dev/null | awk '{print $1}' | head -1)
  if [ -n "$resolved" ]; then
    ok "$HOST → $resolved"
  else
    bad "$HOST 解析失败（DNS 或 /etc/hosts 里没有）"
    NET_OK=0
  fi
fi

# 2. TCP
head_ "2/3 TCP $HOST:$PORT"
if command -v timeout >/dev/null 2>&1; then
  if timeout 3 bash -c "exec 3<>/dev/tcp/$HOST/$PORT" 2>/dev/null; then
    ok "端口可连"
  else
    bad "3 秒内连不上 $HOST:$PORT"
    NET_OK=0
    case "$HOST" in
      127.*|localhost)
        note "  回环地址连不上，通常是反向隧道没建起来或已断开。ss -ltnp | grep $PORT" ;;
      *)
        note "  无路由 / 被防火墙丢包 / 服务没监听——这台机器确实到不了。" ;;
    esac
  fi
else
  note "  没有 timeout 命令，跳过（apt-get install -y coreutils）"
fi

# 3. HTTP
head_ "3/3 HTTP GET $ENDPOINT"
headers=()
[ -n "$API_KEY" ] && headers=(-H "Authorization: Bearer $API_KEY")

# body 与 meta 一次取回，不落临时文件：容器里 mktemp 的清理经常出岔子。
[ "$TOTAL_TIMEOUT" -lt $((CONNECT_TIMEOUT + 5)) ] && TOTAL_TIMEOUT=$((CONNECT_TIMEOUT + 5))
raw=$(curl -sS "${NOPROXY[@]}" -m "$TOTAL_TIMEOUT" --connect-timeout "$CONNECT_TIMEOUT" \
  "${headers[@]}" \
  -w "\n$META_MARK%{http_code}|%{time_connect}|%{time_total}" \
  "$ENDPOINT")
rc=$?

code="${raw##*$META_MARK}"
code="${code%%|*}"
body="${raw%$'\n'$META_MARK*}"

if [ $rc -ne 0 ]; then
  case $rc in
    6)  bad "DNS 解析失败" ;;
    7)  bad "连接被拒绝或不可达" ;;
    28) bad "超时（连接超时 ${CONNECT_TIMEOUT}s / 总超时 ${TOTAL_TIMEOUT}s）" ;;
    35) bad "TLS 握手失败（证书或协议不匹配）" ;;
    60) bad "证书校验失败（自签证书需 -k，但生产别这么干）" ;;
    *)  bad "curl 退出码 $rc" ;;
  esac

  head_ "结论"
  if [ "$NET_OK" = "0" ]; then
    say "  网络不通：这台机器到 $HOST:$PORT 没有通路。"
    say "  平台在公网、模型在内网时，看：./probe_provider.sh tunnel"
  else
    say "  TCP 能连但 HTTP 失败，大概率是 scheme / 证书问题，核对 http 还是 https。"
  fi
  exit 2
fi

meta_rest="${raw##*$META_MARK}"
ctime=$(printf '%s' "$meta_rest" | cut -d'|' -f2)
ttime=$(printf '%s' "$meta_rest" | cut -d'|' -f3)
say "  HTTP $code  连接 ${ctime}s  总计 ${ttime}s"

body_head=$(printf '%s' "$body" | head -c 600 | tr -d '\r')

case "$code" in
  200)
    if command -v jq >/dev/null 2>&1; then
      ids=$(printf '%s' "$body" | jq -r '.data[]?.id // empty' 2>/dev/null)
    else
      ids=$(printf '%s' "$body" \
        | grep -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' \
        | sed 's/.*"\([^"]*\)"$/\1/')
    fi
    count=$(printf '%s\n' "$ids" | grep -c . )
    if [ "$count" -gt 0 ]; then
      ok "返回 200，模型 $count 个"
      printf '%s\n' "$ids" | head -20 | sed 's/^/    /'
      [ "$count" -gt 20 ] && note "    …还有 $((count - 20)) 个"
      exit 0
    fi
    warn "返回 200，但模型列表是空的"
    note "  服务活着，只是没加载模型：核对后端是否已 pull / load 模型。"
    exit 4 ;;
  401|403)
    bad "鉴权失败（HTTP $code）"
    note "  $body_head"
    head_ "结论"
    say "  网络通，卡在 Key："
    say "  - 确认是这个网关签发的 token，不是别处的 key"
    say "  - 有的网关要 Basic auth（curl -u user:pass），不吃 Bearer"
    say "  - 检查 Key 前后有没有多余空格或换行"
    exit 3 ;;
  404)
    bad "路径不存在（HTTP 404）"
    note "  $body_head"
    head_ "结论"
    say "  Base URL 可能多写或少写了 /v1。当前探测：$ENDPOINT"
    say "  试试：$SCHEME://$HOST:$PORT/models"
    say "  或：  $SCHEME://$HOST:$PORT/api/v1/models"
    exit 4 ;;
  *)
    bad "返回 HTTP $code"
    note "  $body_head"
    exit 4 ;;
esac
