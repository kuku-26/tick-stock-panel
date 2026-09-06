# 本地数据打包镜像
#
# 用途：标准应用镜像（Dockerfile 构建，**.dockerignore 排除了 data**）*不含*用户数据；
# 本 Dockerfile 在**独立构建上下文**中从基础镜像继承，再把本地 `data/` 解压进 /app/data，
# 产出一个“代码 + 本地数据”一体的、可整体导出迁移的镜像。
#
# 为什么不用主 Dockerfile 直接 COPY data？主 Dockerfile 的构建上下文受根目录
# .dockerignore 的 `data` 排除项限制，且该语法不支持取反，无法把仓库外的 data 带入。
# 因此这里用独立 context：只放 `data.tar.gz` + 本 Dockerfile（由 build_local_image 脚本组装）。
#
# 密钥（IWENCAI_API_KEY / TICKFLOW_API_KEY / AI_API_KEY 等）**刻意不打进镜像**，
# 由运行期 `docker run --env-file .env` 注入，避免敏感信息随镜像分发。

ARG BASE_IMAGE=tickflow:base

FROM ${BASE_IMAGE}

ARG DATA_TAR=data.tar.gz
COPY ${DATA_TAR} /tmp/${DATA_TAR}

# data.tar.gz 由打包脚本用 `tar -czf data.tar.gz data` 生成，含 `data/` 顶层目录，
# 故解压时用 --strip-components=1 落盘到 /app/data，并清理临时包。
RUN mkdir -p /app/data \
    && tar -xzf /tmp/${DATA_TAR} -C /app/data --strip-components=1 \
    && rm /tmp/${DATA_TAR}