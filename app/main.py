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

DEFAULT_PORT = 28999      # 容器内部监听端口；host 模式下即宿主端口

DEFAULT_DATA_DIR = '/data'
DEFAULT_TZ = 'Asia/Shanghai'

VERSION = '1.2.1'


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
            self.proxy or '(未配置，直连)',
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
    s.proxy = (args.proxy if args.proxy is not None
               else os.environ.get('PROXY', '')).strip()
    # 代理地址还是模板占位符的话，必须在这里就指出来。
    # 否则它会表现为"域名解析失败（Errno -2）"，看起来像 DNS 故障，
    # 用户会去查 DNS —— 实测就是这么被带偏的。
    if s.proxy:
        from .netutil import looks_like_placeholder_proxy, proxy_host
        if looks_like_placeholder_proxy(s.proxy):
            print('[警告] 代理地址看起来还是模板占位符：%s' % s.proxy)
            print('       程序会把它当成真实主机名去解析，必然失败，')
            print('       报错会显示为"域名解析失败"，但真正的原因是代理没填。')
            print('       请到「设置 → 网络」填真实地址，或删掉 compose 里的 PROXY 行。')
            print('       已忽略这个占位符，本次按【直连】运行。')
            s.proxy = ''
        else:
            print('[启动] 代理: %s（主机 %s）' % (s.proxy, proxy_host(s.proxy)))
    s.env_key = os.environ.get('AUTOCHECKIN_KEY', '').strip()
    s.log_level = (args.log_level if args.log_level is not None
                   else os.environ.get('LOG_LEVEL', 'info'))
    s.no_scheduler = bool(args.no_scheduler or _env_bool('NO_SCHEDULER', False))

    tick = (args.tick_seconds if args.tick_seconds is not None
            else _env_int('TICK_SECONDS', 60))
    s.tick_seconds = max(5, int(tick))
    s.version = os.environ.get('APP_VERSION', VERSION)
    return s


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
                browser = BrowserRunner(headless=True)
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
