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

# 事务任务的文档排版：只装 python-docx，不引 LibreOffice（字体与本身体积
# 会把镜像推到 GB 级）。wheel 在构建期联网装，运行时沙箱仍然 --network none。
#
# PIP_INDEX_URL 由 build-image.sh 以 --build-arg 传入（ARG 在 RUN 期间就是
# 环境变量，pip 原生认它）。国内机器上 files.pythonhosted.org 会读超时，
# 不给定源时构建会在这一步挂住；超时与重试次数同时放宽，避免偶发抖动
# 又要重跑整条镜像链。
ARG PIP_INDEX_URL
RUN pip install --no-cache-dir --only-binary=:all: --retries 5 --timeout 60 \
      "python-docx>=1.1,<2"

# 删除护栏：`sitecustomize` 由解释器在启动时自动导入，比用户代码早，
# 于是模型写的 `os.remove(...)` 拿到的是已经换成「进回收站」的版本，
# 它不需要知道这层存在。实现来自 app/sandbox_guard/safe_delete.py，
# 由 build-image.sh 拷进构建上下文（Docker 不能 COPY 上下文之外的文件）。
#
# 只能放 site-packages：入口带 `-I`（隔离模式），PYTHONPATH 与脚本目录
# 都被忽略，site-packages 是唯一一定在 sys.path 上的位置。
# 两个文件必须同目录——钩子用 `Path(__file__).with_name("safe_delete.py")`
# 按路径加载，不依赖包结构，也不把 PAP 的业务代码带进沙箱。
COPY sitecustomize.py safe_delete.py /usr/local/lib/python3.12/site-packages/

# 运行时由宿主侧 --user 65534:65534 覆盖，这里再声明一次做兜底：
# 即使有人手工 docker run 忘带 --user，也不是 root。
USER 65534:65534

WORKDIR /work

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/work \
    LANG=C.UTF-8

ENTRYPOINT ["python3", "-I", "-B", "/opt/pap/runner.py"]
