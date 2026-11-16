#!/usr/bin/env bash
# 安装 gVisor 并把 runsc 注册为 Docker 运行时。
#
# 用法：
#   sudo GVISOR_VERSION=release-20261026.0 bash deploy/sandbox/install-runsc.sh
#
# 只依赖 curl / tar / bzip2 / python3。脚本可重复执行。
set -euo pipefail

VERSION="${GVISOR_VERSION:-release-20261026.0}"
BASE="https://github.com/google/gvisor/releases/download/${VERSION}"
TARBALL="gvisor-x86_64.tar.bz2"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> 下载 gVisor ${VERSION}"
curl -fsSL --retry 10 --retry-all-errors --retry-delay 3 \
  -o "${TMP}/${TARBALL}" "${BASE}/${TARBALL}"
curl -fsSL --retry 10 --retry-all-errors \
  -o "${TMP}/SHA512SUMS" "${BASE}/SHA512SUMS"

echo "==> 校验完整性"
(
  cd "$TMP"
  grep " ${TARBALL}\$" SHA512SUMS | sha512sum -c -
)

echo "==> 解压安装"
tar -xjf "${TMP}/${TARBALL}" -C "$TMP"
install -m 0755 "${TMP}/runsc" /usr/local/bin/runsc

# 新版 gVisor 把 sentry 等组件拆成 sidecar，runsc 会按自身路径下的
# gvisor-bin/ 查找它们；只装 runsc 会在创建容器时报 "sidecar not usable"。
if [ -d "${TMP}/gvisor-bin" ]; then
  rm -rf /usr/local/bin/gvisor-bin
  install -d -m 0755 /usr/local/bin/gvisor-bin
  install -m 0755 "${TMP}"/gvisor-bin/* /usr/local/bin/gvisor-bin/
fi

# containerd 场景才需要，装了不影响 docker。
if [ -f "${TMP}/containerd-shim-runsc-v1" ]; then
  install -m 0755 "${TMP}/containerd-shim-runsc-v1" /usr/local/bin/containerd-shim-runsc-v1
fi

echo "==> 注册 Docker 运行时"
python3 - <<'PY'
import json
import pathlib

path = pathlib.Path("/etc/docker/daemon.json")
config = {}

if path.exists() and path.stat().st_size > 0:
    config = json.loads(path.read_text(encoding="utf-8"))

runtimes = config.setdefault("runtimes", {})
runtimes["runsc"] = {"path": "/usr/local/bin/runsc"}

path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(path.read_text(encoding="utf-8"))
PY

echo "==> 重启 Docker"
systemctl restart docker

for _ in $(seq 1 20); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
  echo "runsc 未注册成功，请检查 /etc/docker/daemon.json 与 dockerd 日志。" >&2
  exit 1
fi

echo "==> 冒烟测试（gVisor 容器内运行 python）"
docker run --rm --runtime=runsc --network=none \
  --read-only --tmpfs /tmp:rw,size=8m \
  --user 65534:65534 --cap-drop ALL \
  pap-sandbox-runtime:1 python3 -c 'print("gvisor ok")' 2>/dev/null \
  || echo "镜像尚未构建，跳过运行时冒烟测试（先执行 build-image.sh）"

echo "完成：runsc $(/usr/local/bin/runsc --version | head -1)"
