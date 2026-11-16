# 沙箱运行时镜像：只放解释器与执行入口，不放任何业务代码或密钥。
#
# 用多阶段复制而不是 apt-get install nodejs：
# 基础镜像里的 Debian 源在部分网络环境下极慢（实测 9.6MB 的 Packages 卡住数分钟），
# 而 node 官方镜像本身已经在本地/镜像站就绪。
#
# 构建：bash deploy/sandbox/build-image.sh
FROM node:22-slim AS node

FROM python:3.12-slim

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN node --version && python3 --version

COPY runner.py /opt/pap/runner.py
RUN chmod 0555 /opt/pap/runner.py

# 运行时由宿主侧 --user 65534:65534 覆盖，这里再声明一次做兜底：
# 即使有人手工 docker run 忘带 --user，也不是 root。
USER 65534:65534

WORKDIR /work

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/work \
    LANG=C.UTF-8

ENTRYPOINT ["python3", "-I", "-B", "/opt/pap/runner.py"]
