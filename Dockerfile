# 作者：LHF
# 时间：2026-11-01
# 作用：构建并启动 EvoAgent 服务镜像。
# 基于精简 Python 镜像构建运行环境。
FROM python:3.11-slim

# 关闭字节码落盘并使日志立即输出。
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# 先安装依赖，以便镜像构建复用依赖层缓存。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 复制后端、前端静态资源及标准 Skill。
COPY evoagent ./evoagent
COPY web ./web
COPY skills ./skills
# 声明服务端口，并通过模块入口启动应用。
EXPOSE 8080
CMD ["python", "-m", "evoagent"]

