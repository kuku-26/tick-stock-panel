#!/usr/bin/env bash
# =====================================================================
# build_local_image.sh — 把“代码 + 本地 data”打包成一个可迁移的 Docker 镜像
#
#  用法（在项目根目录）:
#     bash deploy/build_local_image.sh
#
#  产物: 项目根目录下 tickflow-local.tar
#
#  流程:
#    1. 用根目录 Dockerfile 构建“标准应用镜像”(不含 data)
#    2. 把本地 data/ 打成 data.tar.gz
#    3. 用 deploy/embedded/Dockerfile 构建“数据镜像”(解压 data 进 /app/data)
#    4. docker save 导出为单一 tar
#
#  密钥不打包: 运行时用 --env-file .env 注入。
# =====================================================================
set -euo pipefail

IMAGE_NAME="${1:-tickflow-local}"
TAG="${2:-1.0}"
BASE_IMAGE="${3:-tickflow:base}"
OUT_TAR="${4:-tickflow-local.tar}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKER="$ROOT/deploy/embedded/Dockerfile"
STAGING="$(mktemp -d)"
FULL="${IMAGE_NAME}:${TAG}"
trap 'rm -rf "$STAGING"' EXIT

echo "==> 1/4 构建标准应用镜像(不含 data)  ${BASE_IMAGE}"
docker build -t "${BASE_IMAGE}" "${ROOT}"

echo "==> 2/4 打包本地 data/"
tar -czf "${STAGING}/data.tar.gz" -C "${ROOT}" data

echo "==> 3/4 构建数据镜像  ${FULL}"
cp "${DOCKER}" "${STAGING}/Dockerfile"
docker build -t "${FULL}" --build-arg BASE_IMAGE="${BASE_IMAGE}" "${STAGING}"

echo "==> 4/4 导出镜像 -> ${ROOT}/${OUT_TAR}"
docker save -o "${ROOT}/${OUT_TAR}" "${FULL}"

echo ""
echo "完成! 镜像: ${FULL}; 导出文件: ${ROOT}/${OUT_TAR}"
echo ""
echo "把 ${OUT_TAR} 传到服务器后执行:"
echo "  docker load -i ${OUT_TAR}"
echo "  # 密钥由 .env 运行时注入(不打进镜像)"
echo "  docker run -d --name tsp --restart unless-stopped --env-file .env -p 3018:3018 ${FULL}"
echo "  # 访问 http://服务器IP:3018"