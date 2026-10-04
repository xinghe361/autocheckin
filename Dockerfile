# 自动签到镜像
#
# 基础镜像说明：用 Playwright 官方镜像，它已经装好 Chromium 及其全部系统依赖
# （libnss3 / libatk / 字体等）。自己从 slim 镜像装浏览器要处理一堆依赖和字体问题，
# 而且容易在中文页面上出现方块字。这里直接用官方镜像最省事、最稳。
#
# 为什么默认在镜像内自带浏览器，而不是复用外部 Chrome：
#   Chrome 的远程调试端口通常只绑在它自己容器内的 127.0.0.1 上，外部连不进去；
#   要连上必须共享它的网络命名空间（network_mode: "container:xxx"），
#   而那样本容器的端口就无法映射出来 —— Web 界面会失联。
#   所以镜像内自带浏览器最省事；同时保留 remote_cdp_url 配置项，
#   如果你愿意接受上面的限制，也可以在设置里改成连接外部 Chrome。
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PORT=28999 \
    DATA_DIR=/data \
    LOG_LEVEL=info

# 只用 Python 部分，Node/npm 在这里没用，顺手清掉减小体积
RUN rm -rf /usr/lib/node_modules /usr/bin/node /usr/bin/npm /usr/bin/npx \
           /root/.npm /root/.cache 2>/dev/null || true

# 清掉用不到的浏览器内核，显著减小体积。
#
# Playwright 官方镜像默认带三种内核，但本项目只用 Chromium：
#   chromium-1140  542M  ← 唯一需要的
#   firefox-1465   238M  ← 用不到
#   webkit-2083    254M  ← 用不到
#   ffmpeg-1010    4.9M  ← 那是录屏用的，本项目不录视频
# 实测清掉后省约 497M（镜像 2.21GB -> 约 1.71GB）。
#
# 注意：这里只删浏览器二进制，**保留 playwright 的 Python 包与系统依赖**，
# 所以 chromium 仍能正常启动（已验证）。
# 另外 chromium_headless_shell 只在 1.49+ 存在，这里用通配删以免版本变化后失效。
RUN set -eux; \
    rm -rf /ms-playwright/firefox-* \
           /ms-playwright/webkit-* \
           /ms-playwright/ffmpeg-* \
           /ms-playwright/chromium_headless_shell-*; \
    ls -d /ms-playwright/* | sed 's/^/保留: /'

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
