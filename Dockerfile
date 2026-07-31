# HF Spaces / 通用容器 部署镜像
# 仅依赖 Python 标准库，无需 pip 安装任何包。
FROM python:3.11-slim

WORKDIR /app

# 复制全部代码与配置
COPY . /app

# HF Spaces 会通过环境变量注入 PORT（通常 7860）；应用读取该变量。
# SPACE_ID 由平台注入 -> 自动绑定 0.0.0.0 对外暴露。
ENV HOST=0.0.0.0 \
    PORT=7860 \
    PYTHONUNBUFFERED=1

EXPOSE 7860

CMD ["python", "zotero_web_search.py"]
