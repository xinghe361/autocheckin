# 自动签到镜像
#
# ============================================================================
# 为什么用 python:3.12-slim 而不是 Playwright 官方镜像（重要）
# ============================================================================
# 以前基于 mcr.microsoft.com/playwright/python，它自带 Chromium/Firefox/WebKit
# 与全部系统依赖，镜像很大。后来发现：
#
#   1) 内置的三个站点（V2EX / NodeSeek / Chiphell）全部走**纯 HTTP 请求**，
#      need_browser=False，根本用不到浏览器。
#   2) "从浏览器读取 Cookie"用的是 QD 的 get-cookies 扩展，跑在**用户自己的
#      浏览器**里；"录制签到步骤"也是把脚本注入到用户浏览器的 iframe 里。
#      两者都不需要容器内浏览器。
#   3) 只有"录制的自定义站点"才需要浏览器（录制产出的 click/fill/goto 步骤
#      无法用纯请求回放，纯请求只支持 get/post/extract_regex）。
#
# 而在后面层里 `rm -rf` 浏览器**并不会减小镜像体积** —— 那些文件仍在基础
# 镜像的层里，拉取时照样要下载。实测：浏览器所在层 616MB 原封不动。
# 真正减小的唯一办法是**不用带浏览器的基础镜像**。
#
# 所以这里换成 python:3.12-slim：
#     镜像从约 2.2GB 降到约 0.2GB
# 代价：录制的自定义站点会明确报"需要浏览器但不可用"（内置站点不受影响）。
# 如果你确实要用浏览器路径，把 bases 换回 Playwright 镜像并加上
# `playwright install chromium` 即可（见 README）。
# ============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PORT=28999 \
    DATA_DIR=/data \
    LOG_LEVEL=info

# tzdata 不能省：程序用 time.tzset() 应用 TZ，而 slim 镜像默认**没有**
# 系统时区库 —— 不装的话 TZ=Asia/Shanghai 不生效，签到时间会差 8 小时。
# 顺带装上 ca-certificates（HTTPS 校验需要根证书）。
# 装完立刻清 apt 缓存，避免把包索引打进镜像。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    # tzdata 在非交互环境下可能不写 /etc/localtime，显式设一次
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone

WORKDIR /app

# 先装依赖，利用镜像层缓存：改代码不会重新装依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

# 数据目录：配置、加密密钥、运行状态都放这里
RUN mkdir -p /data && chmod 700 /data
VOLUME ["/data"]

# 默认内部端口用 28999 而不是常见的 8080。
#
# 为什么：如果以宿主网络模式（network_mode: host）运行，容器的 ports 映射是
# 【完全无效】的，容器直接占用宿主端口。而 8080 在 NAS 上通常已被系统服务占用，
# 撞上就会 Address already in use。
# 默认用 28999 可以让 host 模式开箱可用，不用再改任何东西。
EXPOSE 28999

# 健康检查用 $PORT，跟着内部端口走（改 PORT 后不用改这里）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
p=os.environ.get('PORT','28999'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz',timeout=4).status==200 else 1)"

# 默认启动：网页界面 + 后台调度
# 首次使用可加 --no-scheduler，先只开界面把配置填好
ENTRYPOINT ["python", "-m", "app"]
