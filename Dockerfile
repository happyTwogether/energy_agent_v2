# ============================================================
# 阶段 1: base — 公共基础层（dev / prod 共享）
# ============================================================
FROM python:3.11-slim AS base

# 防止生成 .pyc 文件 & 确保日志实时输出（不缓冲）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 创建非 root 用户，提升容器安全性
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --create-home appuser

WORKDIR /app

# 先单独复制依赖文件，利用 Docker Layer Cache 加速构建
COPY requirements.txt .
# 使用清华 PyPI 镜像加速依赖安装
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# ============================================================
# 阶段 2: development — 本地开发阶段（支持热更新）
# ============================================================
FROM base AS development

# 安装开发期额外工具（如需 debugpy 等可在此追加）
# RUN pip install --no-cache-dir debugpy

# 开发阶段不复制源码 —— 源码通过 docker-compose volumes 挂载实现热更新
# 仅设置权限，确保挂载目录可写
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# 开发阶段默认启动命令：uvicorn + --reload 实现代码热更新
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ============================================================
# 阶段 3: production — 生产部署阶段
# ============================================================
FROM base AS production

# 生产阶段将源码完整复制进镜像（不依赖外部挂载）
COPY app/ ./app/
COPY main.py ./

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# 生产阶段启动命令：不带 --reload，使用多 worker 提升吞吐
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
