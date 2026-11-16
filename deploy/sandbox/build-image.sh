#!/usr/bin/env bash
# 构建沙箱运行时镜像。
#
# 用法：bash deploy/sandbox/build-image.sh [tag]
# 镜像里只有解释器与执行入口，不含业务代码、不含密钥。
set -euo pipefail

TAG="${1:-pap-sandbox-runtime:1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

docker build --pull -t "$TAG" "$HERE/image"

echo
echo "已构建：$TAG"
echo "自检："
docker run --rm --runtime=runsc --network=none --read-only \
  --tmpfs /work:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
  "$TAG" <<'JSON'
{"language": "python", "code": "print(sum(range(1, 101)))", "stdin": "", "timeout_seconds": 5}
JSON
