#!/usr/bin/env bash
# 构建沙箱运行时镜像。
#
# 用法：bash deploy/sandbox/build-image.sh [tag]
# 镜像里只有解释器、执行入口与删除护栏，不含业务代码、不含密钥。
#
# 为什么要组装临时上下文：`safe_delete.py` 的**唯一源**在
# `app/sandbox_guard/`，而 Docker 不能 COPY 构建上下文之外的文件。把它拷一份
# 进 `image/` 也能构建，但仓库里就有两份删除逻辑，改一处忘一处时容器里的
# 行为会和单测静悄悄分叉。
#
# 发布是两步：先打 tag，再把 `/etc/pap-sandbox.env` 的 `PAP_SANDBOX_IMAGE`
# 指过去并 `systemctl restart pap-sandbox`。旧 tag 留着就能回滚。
#
# `PAP_BUILD_PULL=0` 跳过 `--pull`：只想给已有基底加一层时用它，否则
# `python:3.12-slim` 悄悄换代，出问题的就不只是这一层。
set -euo pipefail

TAG="${1:-pap-sandbox-runtime:1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

PULL="--pull"
[ "${PAP_BUILD_PULL:-1}" = "0" ] && PULL=""

CONTEXT="$(mktemp -d)"
trap 'rm -rf "$CONTEXT"' EXIT

cp "$HERE/image/Dockerfile" "$HERE/image/runner.py" \
   "$HERE/image/sitecustomize.py" "$CONTEXT/"
cp "$ROOT/app/sandbox_guard/safe_delete.py" "$CONTEXT/"

docker build $PULL -t "$TAG" "$CONTEXT"

echo
echo "已构建：$TAG"
echo "自检（应看到 patched True / trashed True）："
# `-i` 不能省：runner 从 stdin 读那份 JSON。少了它容器拿到的是空 stdin，
# 自检会稳定地报「请求体不是合法 JSON」——而这看起来像镜像坏了。
docker run --rm -i --runtime=runsc --network=none --read-only \
  --tmpfs /work:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
  "$TAG" <<'JSON'
{"language": "python", "code": "import os\np = '/work/t.txt'\nopen(p, 'w').write('x')\nos.remove(p)\nprint('sum', sum(range(1, 101)))\nprint('patched', getattr(os.remove, '_pap_safe_delete', False))\nprint('trashed', os.path.exists('/work/.local/share/Trash/files/t.txt'))", "stdin": "", "timeout_seconds": 5}
JSON
