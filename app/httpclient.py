"""HTTP 客户端：全局代理 + 每站点可调的证书/超时/请求头。

设计取舍（按用户要求）：
    代理已经做了分流（国内直连、国外走代理），所以这里只提供一个全局代理设置，
    所有站点统一使用，不做"按站点选代理"那套复杂逻辑。

实测依据（家用 NAS 环境，走本地代理）：
    直连          走代理
    v2ex          DNS 污染不可达     200
    nodeseek      DNS 污染不可达     403（Cloudflare 挑战，需浏览器）
    telegram      不可达             302
    deepseek      401                401
    chiphell      000 证书链问题     000
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any, Dict, Optional

from . import netutil

DEFAULT_TIMEOUT = 20


@dataclass
class HttpConfig:
    """一次请求的网络策略（代理统一由全局设置决定）。"""

    # 是否放宽证书校验。个别站点（实测 chiphell）证书链不完整，
    # 按站点开启；开启时会在日志与通知里明确记录，不静默降级。
    verify_ssl: bool = True
    timeout: int = DEFAULT_TIMEOUT
    user_agent: str = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class HttpResponse:
    status: int
    text: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


def normalize_proxy(proxy: str) -> str:
    """把用户填的代理地址规整成 urllib 能用的形式。

    允许省略协议（按 http 处理）：
        '10.0.0.1:7890'        -> 'http://10.0.0.1:7890'
        'http://10.0.0.1:7890' -> 原样
        'socks5://10.0.0.1:1080'   -> 原样（需要 PySocks 支持）
    """
    p = (proxy or '').strip()
    if not p:
        return ''
    if '://' not in p:
        p = 'http://' + p
    return p


# 会被 urllib 读取的代理环境变量（大小写两种都覆盖）
_PROXY_ENV_KEYS = ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY',
                   'HTTPS_PROXY', 'ALL_PROXY')


@contextlib.contextmanager
def _no_proxy_env():
    """临时清空代理环境变量，确保未配置代理时是真正的直连。

    为什么不能只用 ProxyHandler({})：
      urllib.request.ProxyHandler.__init__ 在 proxies 为空时会调用 getproxies()，
      也就是回退去读环境变量；实测 ProxyHandler({}) 之后代理 handler 甚至不会注册，
      但仍可能悄悄用上环境变量里的代理。所以这里直接把环境变量摘掉，退出时还原。

    （注意：Python 3.12 的 urllib.request 没有模块级代理缓存，
      getproxies / getproxies_environment 都没有 cache_clear，
      所以只需要处理环境变量本身。）
    """
    saved = {k: os.environ.get(k) for k in _PROXY_ENV_KEYS}
    had = {k: (k in os.environ) for k in _PROXY_ENV_KEYS}
    try:
        for k in _PROXY_ENV_KEYS:
            os.environ.pop(k, None)
        yield
    finally:
        for k in _PROXY_ENV_KEYS:
            if had[k]:
                os.environ[k] = saved[k] or ''
            else:
                os.environ.pop(k, None)


def build_opener(cfg: HttpConfig, global_proxy: str = '',
                 cookie_jar: Optional[CookieJar] = None):
    """按配置构造 urllib 的 opener。"""
    handlers = []
    proxy = normalize_proxy(global_proxy)
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    if not cfg.verify_ssl:
        handlers.append(urllib.request.HTTPSHandler(
            context=netutil.build_ssl_context(verify=False)))
    handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar or CookieJar()))
    return urllib.request.build_opener(*handlers)


def request(method: str, url: str, cfg: Optional[HttpConfig] = None,
            global_proxy: str = '', data: Optional[bytes] = None,
            cookie_jar: Optional[CookieJar] = None) -> HttpResponse:
    """发起一次 HTTP(S) 请求；网络层错误抛 HttpError。"""
    cfg = cfg or HttpConfig()
    headers = {'User-Agent': cfg.user_agent, 'Accept': '*/*'}
    headers.update(cfg.headers or {})

    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    proxy = normalize_proxy(global_proxy)

    try:
        if proxy:
            opener = build_opener(cfg, proxy, cookie_jar)
            return _do_open(opener, req, cfg, url)
        # 未配置代理：在"无代理环境"下构造并执行，保证真正直连
        with _no_proxy_env():
            opener = build_opener(cfg, '', cookie_jar)
            return _do_open(opener, req, cfg, url)
    except HttpError:
        raise
    except Exception as e:  # noqa: BLE001
        kind = netutil.classify_network_error(e)
        raise HttpError(kind, netutil.describe_network_error(kind), str(e)) from e


def _do_open(opener, req, cfg: HttpConfig, url: str) -> HttpResponse:
    try:
        with opener.open(req, timeout=cfg.timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or 'utf-8'
            return HttpResponse(
                status=resp.status,
                text=raw.decode(charset, 'replace'),
                url=resp.geturl(),
                headers={k.lower(): v for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as e:
        raw = b''
        try:
            raw = e.read()
        except Exception:
            pass
        charset = 'utf-8'
        try:
            charset = e.headers.get_content_charset() or 'utf-8'
        except Exception:
            pass
        return HttpResponse(
            status=e.code,
            text=raw.decode(charset, 'replace'),
            url=url,
            headers={k.lower(): v for k, v in (e.headers or {}).items()},
        )


class HttpError(Exception):
    """网络层错误，带可读原因，便于通知与交给 AI 分析。"""

    def __init__(self, kind: str, message: str, detail: str = ''):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return self.message + (('（%s）' % self.detail) if self.detail else '')


def get(url: str, **kw) -> HttpResponse:
    return request('GET', url, **kw)


def post(url: str, data: Optional[bytes] = None, **kw) -> HttpResponse:
    return request('POST', url, data=data, **kw)


def enc(data: Dict[str, Any]) -> bytes:
    """表单编码。"""
    return urllib.parse.urlencode(data).encode('utf-8')


def enc_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode('utf-8')
