# =====================================================================
# build_local_image.ps1  — 把“代码 + 本地 data”打包成一个可迁移的 Docker 镜像
#
#  用法（在项目根目录）:
#     powershell -ExecutionPolicy Bypass -File deploy/build_local_image.ps1
#
#  产物: 项目根目录下 tickflow-local.tar
#
#  流程:
#    1. 用根目录 Dockerfile 构建“标准应用镜像”(不含 data, .dockerignore 已排除)
#    2. 把本地 data/ 打成 data.tar.gz
#    3. 用 deploy/embedded/Dockerfile 构建“数据镜像”(解压 data 进 /app/data)
#    4. docker save 导出为单一 tar，便于 scp/rsync 到服务器
#
#  密钥不打包: 运行时用 --env-file .env 注入。
# =====================================================================

param(
  [string]$ImageName = "tickflow-local",   # 镜像名
  [string]$Tag        = "1.0",             # 镜像标签
  [string]$BaseImage  = "tickflow:base",   # 标准应用镜像(不含 data)
  [string]$OutTar     = "tickflow-local.tar"
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $PSScriptRoot      # deploy/../ = 项目根
$Docker  = Join-Path $PSScriptRoot "embedded\Dockerfile"
$Staging = Join-Path $env:TEMP "tsp-embedded"
$Full    = "${ImageName}:${Tag}"

Write-Host "==> 1/4 构建标准应用镜像(不含 data)  $BaseImage" -ForegroundColor Cyan
docker build -t $BaseImage $Root
if ($LASTEXITCODE -ne 0) { throw "基础镜像构建失败" }

# 重新组装干净的 staging 上下文
if (Test-Path $Staging) { Remove-Item -Recurse -Force $Staging }
New-Item -ItemType Directory -Force -Path $Staging | Out-Null

Write-Host "==> 2/4 打包本地 data/" -ForegroundColor Cyan
Push-Location $Root
  tar -czf (Join-Path $Staging "data.tar.gz") data
  if ($LASTEXITCODE -ne 0) { Pop-Location; throw "data 打包失败" }
Pop-Location

Write-Host "==> 3/4 构建数据镜像  $Full" -ForegroundColor Cyan
Copy-Item $Docker (Join-Path $Staging "Dockerfile") -Force
docker build -t $Full --build-arg BASE_IMAGE=$BaseImage $Staging
if ($LASTEXITCODE -ne 0) { throw "数据镜像构建失败" }

$Out = Join-Path $Root $OutTar
Write-Host "==> 4/4 导出镜像 -> $Out" -ForegroundColor Cyan
docker save -o $Out $Full
if ($LASTEXITCODE -ne 0) { throw "docker save 失败" }

Write-Host ""
Write-Host "完成! 生成的镜像: $Full ; 导出文件: $Out" -ForegroundColor Green
Write-Host ""
Write-Host "把 $Out 传到服务器后执行:" -ForegroundColor Yellow
Write-Host "  docker load -i $OutTar"
Write-Host "  # 密钥由 .env 运行时注入(不打进镜像)"
Write-Host "  docker run -d --name tsp --restart unless-stopped --env-file .env -p 3018:3018 $Full"
Write-Host "  # 访问 http://服务器IP:3018"