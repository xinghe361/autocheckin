"""程序入口：启动服务层、后台调度与网页界面。

配置来源优先级：命令行参数 > 环境变量 > 默认值。
容器里通常只用环境变量（compose 的 environment 段）。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from . import secrets as S
from .netutil import mask_proxy_url

DEFAULT_PORT = 28999      # 容器内部监听端口；host 模式下即宿主端口

DEFAULT_DATA_DIR = '/data'
DEFAULT_TZ = 'Asia/Shanghai'

VERSION = '1.4.6'


@dataclass
class Settings:
    """运行参数。"""

    port: int = DEFAULT_PORT
    host: str = '0.0.0.0'
    data_dir: str = DEFAULT_DATA_DIR
    timezone: str = DEFAULT_TZ
    proxy: str = ''
    env_key: str = ''
    tick_seconds: int = 60
    no_scheduler: bool = False
    log_level: str = 'info'
    version: str = VERSION

    def summary(self) -> str:
        tz = self.timezone
        return (
            '版本 %s\n'
            '监听 %s:%d\n'
            '数据目录 %s\n'
            '时区 %s\n'
            '代理 %s\n'
            '调度间隔 %d 秒%s\n'
            '密钥来源 %s'
        ) % (
            self.version, self.host, self.port, self.data_dir, tz,
            mask_proxy_url(self.proxy) if self.proxy else '(未配置，直连)',
            self.tick_seconds,
            '，已禁用调度' if self.no_scheduler else '',
            '环境变量' if self.env_key else '数据目录中的密钥文件',
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on', 'y')


def build_settings(argv: Optional[List[str]] = None) -> Settings:
    """解析命令行与环境变量，得到运行参数。"""
    parser = argparse.ArgumentParser(
        prog='autocheckin',
        description='自动签到：每日自动签到并支持失败分析、通知与 WebDAV 备份')
    parser.add_argument('--port', type=int, default=None, help='网页界面端口')
    parser.add_argument('--host', default=None, help='监听地址')
    parser.add_argument('--data-dir', default=None, help='数据目录')
    parser.add_argument('--timezone', default=None, help='时区，如 Asia/Shanghai')
    parser.add_argument('--proxy', default=None, help='全局代理地址，留空表示直连')
    parser.add_argument('--tick-seconds', type=int, default=None,
                        help='调度检查间隔（秒），最小 5')
    parser.add_argument('--no-scheduler', action='store_true',
                        help='只启动网页界面，不自动签到（便于先配置）')
    parser.add_argument('--log-level', default=None,
                        choices=['debug', 'info', 'warn', 'error'])
    parser.add_argument('--version', action='store_true', help='打印版本后退出')

    args = parser.parse_args(argv)

    if args.version:
        print(VERSION)
        sys.exit(0)

    s = Settings()
    s.port = args.port if args.port is not None else _env_int('PORT', DEFAULT_PORT)
    s.host = args.host if args.host is not None else os.environ.get('HOST', '0.0.0.0')
    s.data_dir = (args.data_dir if args.data_dir is not None
                  else os.environ.get('DATA_DIR', DEFAULT_DATA_DIR))
    s.timezone = (args.timezone if args.timezone is not None
                  else os.environ.get('TZ', DEFAULT_TZ))
    # 代理**只**由网页「设置 → 网络」决定，不再从环境变量读。
    #
    # 为什么去掉环境变量：两处都能设就会互相打架，而"网页里改了却不生效"
    # 是最难排查的一类问题（用户看到的现象是"我明明填了代理，怎么还是直连"）。
    # 现在只有一个来源，行为可预测。
    # --proxy 命令行参数保留（调试用），但没有环境变量回退。
    s.proxy = (args.proxy or '').strip()
    # 代理地址还是模板占位符的话，必须在这里就指出来。
    # 否则它会表现为"域名解析失败（Errno -2）"，看起来像 DNS 故障，
    # 用户会去查 DNS —— 实测就是这么被带偏的。
    if s.proxy:
        from .netutil import looks_like_placeholder_proxy, proxy_host
        if looks_like_placeholder_proxy(s.proxy):
            print('[警告] 代理地址看起来还是模板占位符：%s' % s.proxy)
            print('       程序会把它当成真实主机名去解析，必然失败，')
            print('       报错会显示为"域名解析失败"，但真正的原因是代理没填。')
            print('       请到网页「设置 → 网络」填真实地址。')
            print('       已忽略这个占位符，本次按【直连】运行。')
            s.proxy = ''
        else:
            # 打码后再打印：代理地址常带 user:pass，直接进日志等于凭据泄露
            print('[启动] 代理: %s（主机 %s）'
                  % (mask_proxy_url(s.proxy), proxy_host(s.proxy)))
    if os.environ.get('PROXY', '').strip():
        print('[提示] 检测到环境变量 PROXY，但本版本已改为'
              '**只认网页「设置 → 网络」里的代理配置**。')
        print('       环境变量不再生效，请到网页里填写；'
              'compose 里的 PROXY 行可以删掉了。')
    s.env_key = os.environ.get('AUTOCHECKIN_KEY', '').strip()
    s.log_level = (args.log_level if args.log_level is not None
                   else os.environ.get('LOG_LEVEL', 'info'))
    s.no_scheduler = bool(args.no_scheduler or _env_bool('NO_SCHEDULER', False))

    tick = (args.tick_seconds if args.tick_seconds is not None
            else _env_int('TICK_SECONDS', 60))
    s.tick_seconds = max(5, int(tick))
    # 版本号**只**反映代码真实版本，不可被环境变量覆盖。
    #
    # 为什么去掉覆盖：以前是 `os.environ.get('APP_VERSION', VERSION)`，
    # 于是"界面显示的版本"和"实际跑的代码"可以不一致 —— 排查问题时报的
    # 版本号是错的，等于自毁线索（用户就因此被带偏过：明明更新了镜像，
    # 界面却一直显示旧版本，查了很久）。
    # 如果确实需要标注自建镜像，用 APP_VERSION_SUFFIX 之类另起一个字段，
    # 不能顶替 VERSION 本身。
    s.version = VERSION
    if os.environ.get('APP_VERSION', '').strip():
        print('[提示] 检测到环境变量 APP_VERSION，但本版本已改为'
              '**版本号只反映代码真实版本**，该变量不再生效。')
        print('       当前真实版本：%s' % VERSION)
    return s


def _warn_auth(service) -> None:
    """把认证相关的真实风险窗口打到启动日志里。

    为什么值得单独告警：这两种状态下，局域网内**任何人都能操作这个容器**
    （改全局代理、读配置、触发签到、删除站点）。而用户往往以为"还没配好所以安全"。
    """
    try:
        cfg = service.load_config()
    except Exception:                                           # noqa: BLE001
        return
    if not getattr(cfg, 'auth_required', True):
        print('=' * 56)
        print('[警告] 网页访问口令已关闭：局域网内任何人都能打开界面并修改设置')
        print('[警告] （可改全局代理、读取配置、触发签到、删除站点）')
        print('[警告] 若不希望如此，请到「设置 → 访问口令」重新开启')
        print('=' * 56)
        return
    if not getattr(cfg, 'auth_password_enc', ''):
        print('=' * 56)
        print('[注意] 尚未设置访问口令 —— 现在任何人都能抢先设置它并接管本容器。')
        print('[注意] 请立刻打开 http://<本机IP>:%d/ 完成初始化。' % _env_int(
            'PORT', DEFAULT_PORT))
        print('=' * 56)


def apply_timezone(tz_name: str) -> bool:
    """设置进程时区（影响签到时间的本地解释）。"""
    try:
        os.environ['TZ'] = tz_name
        if hasattr(time, 'tzset'):
            time.tzset()
        return True
    except Exception:  # noqa: BLE001
        return False


def create_service(settings: Settings):
    """构建服务对象（含加密盒与浏览器）。"""
    from . import service as SV

    browser = None
    if not _env_bool('DISABLE_BROWSER', False):
        try:
            from .browser import BrowserRunner, browser_available
            if browser_available():
                # remote_cdp_url / browser_path / headless 都存在配置里，
                # 所以在建服务之前先读一次配置。
                #
                # ⚠️ 以前这里只写 BrowserRunner(headless=True)，**没把配置传进去** ——
                # 于是界面上填的"连接外部 Chrome 调试端口"被完全忽略，
                # 而且不报错（用户会以为配好了却一直走本地浏览器/不可用）。
                # 实测发现。
                try:
                    _cfg = SV.Service(settings.data_dir,
                                      version=settings.version,
                                      env_key=settings.env_key or None,
                                      proxy=settings.proxy).load_config()
                    _cdp = getattr(_cfg, 'remote_cdp_url', '') or ''
                    _bpath = getattr(_cfg, 'browser_path', '') or ''
                    _headless = bool(getattr(_cfg, 'headless', True))
                except Exception:                               # noqa: BLE001
                    _cdp, _bpath, _headless = '', '', True
                browser = BrowserRunner(headless=_headless,
                                        browser_path=_bpath,
                                        remote_cdp_url=_cdp)
                if _cdp:
                    print('[启动] 浏览器：连接外部 Chrome（%s）'
                          % _cdp.split('@')[-1])
        except Exception:  # noqa: BLE001
            browser = None

    return SV.Service(settings.data_dir, version=settings.version,
                      env_key=settings.env_key or None,
                      proxy=settings.proxy, browser=browser)


def main(argv: Optional[List[str]] = None) -> int:
    settings = build_settings(argv)
    apply_timezone(settings.timezone)
    print('=' * 56)
    print('自动签到 启动中')
    print(settings.summary())
    print('=' * 56)

    try:
        service = create_service(settings)
    except Exception as e:  # noqa: BLE001
        print('启动失败：无法初始化数据目录或密钥：%s' % e, file=sys.stderr)
        return 2

    # 显式告警：没有可用密钥时，配置里的凭据只是"可逆编码"，不是加密。
    # （避免让人误以为已经加密了。）
    box = getattr(service, 'box', None)
    if box is not None and getattr(box, 'is_weak', False):
        print('=' * 56)
        print('[警告] 凭据未真正加密：%s' % box.weak_reason())
        print('[警告] 请确认数据目录可写（需要生成密钥文件），'
              '或设置环境变量 %s' % S.KEY_ENV)
        print('=' * 56)

    # 认证相关告警。这两条是真实存在的风险窗口，必须在启动日志里说清楚，
    # 而不是等用户自己翻文档：
    #   1) 要求认证但还没设口令 —— 此时 /api/auth/setup 是公开的，
    #      局域网内任何人都能抢先设一个口令，从而接管这个容器
    #      （能改代理、读配置、触发签到）。窗口一直持续到用户设好口令。
    #   2) 用户主动关掉了认证 —— 那么整个界面（含代理设置）对局域网完全敞开。
    _warn_auth(service)

    from . import web as WB
    from .scheduler import Scheduler

    scheduler = None
    if not settings.no_scheduler:
        scheduler = Scheduler(service, interval=settings.tick_seconds,
                              log_fn=lambda m: print('[调度] %s' % m))
        scheduler.start()
        print('[调度] 已启动，每 %d 秒检查一次（启动时只排程，不会立即签到）'
              % settings.tick_seconds)
    else:
        print('[调度] 已按参数禁用（--no-scheduler）')

    app = WB.WebApp(service, scheduler=scheduler, version=settings.version)

    try:
        httpd = WB.serve(app, host=settings.host, port=settings.port, block=False)
    except OSError as e:
        print('无法监听 %s:%d：%s' % (settings.host, settings.port, e), file=sys.stderr)
        if scheduler:
            scheduler.stop()
        return 3

    print('[网页] 已就绪：http://<本机IP>:%d/' % settings.port)
    print('[提示] 首次使用请到「设置」页填写代理、通知与 WebDAV，再到「站点」页启用站点。')

    stop_event = threading.Event()

    def _shutdown(signum, frame):        # noqa: ARG001
        print('\n收到退出信号，正在停止…')
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        print('正在关闭…')
        if scheduler:
            scheduler.stop()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        print('已退出。')
    return 0


def _run_as_script() -> None:
    """支持 `python app/main.py` 直接运行。"""
    import pathlib
    import sys as _sys

    root = str(pathlib.Path(__file__).resolve().parent.parent)
    if root not in _sys.path:
        _sys.path.insert(0, root)
    # 以脚本方式运行时没有包上下文，需要显式用绝对导入
    globals()['__package__'] = 'app'


if __name__ == '__main__':
    _run_as_script()
    sys.exit(main())
