FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
# 防止 Python 生成 .pyc 文件
ENV PYTHONDONTWRITEBYTECODE=1
# 确保 Python 输出不被缓存
ENV PYTHONUNBUFFERED=1

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目代码
COPY . .

# 暴露端口（容器内部默认 8000）
EXPOSE 8000

# 启动命令：生产容器禁用开发模式的 reload
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
