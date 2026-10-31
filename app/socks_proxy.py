"""socks4 / socks5 / socks5h 代理支持。

urllib **原生不支持 socks**：把 `socks5://host:port` 塞进 ProxyHandler 之后，
它会在解析协议头时认不出来，最终报 `unknown url type: socks5` ——
装没装 PySocks 都一样（实测：装了 PySocks 1.7.1 仍然报这个）。
常见做法是把 `socket.socket` 全局替换成 `socks.socksocket`，但这个进程里
同时有网页服务线程和后台签到线程，全局替换会让"访问网页"也莫名走代理。
所以这里做的是**每条连接自己走 socks**：

    http.client.HTTPConnection/HTTPSConnection 各派生一个类，
    只覆写 connect()：用 socks.create_connection 建好 socket 再交给 urllib。

这样代理只作用于构造了它那个 opener 的请求，不影响进程里其它网络调用。
"""

from __future__ import annotations

import http.client
import urllib.request
from typing import Any, Optional, Tuple
from urllib.parse import unquote, urlsplit

try:                                  # 没装 PySocks 时整个模块仍可导入
    import socks as _socks
except Exception:                     # noqa: BLE001
    _socks = None


# proxy 参数包：(类型, 主机, 端口, 用户名, 密码)
ProxySpec = Tuple[int, str, int, Optional[str], Optional[str]]


def available() -> bool:
    """PySocks 是否可用。"""
    return _socks is not None


def parse_proxy(proxy: str) -> Optional[ProxySpec]:
    """把 `socks5://user:pass@host:1080` 解析成 PySocks 要的参数。

    认不出/不完整就返回 None（由调用方给出可读的错误）。
    socks5h 的 h 表示"域名交给代理解析"，PySocks 的 proxy_rdns=True
    正是这个语义，所以 socks5 与 socks5h 都映射到 SOCKS5。
    """
    if not _socks or not proxy:
        return None
    try:
        p = urlsplit(proxy if '://' in proxy else 'socks5://' + proxy)
    except ValueError:
        return None
    scheme = (p.scheme or '').lower()
    if scheme.startswith('socks4'):
        ptype = _socks.PROXY_TYPE_SOCKS4
    elif scheme.startswith('socks5') or scheme.startswith('socks'):
        ptype = _socks.PROXY_TYPE_SOCKS5
    else:
        return None
    host = p.hostname
    if not host:
        return None
    try:
        port = int(p.port or (1080 if ptype == _socks.PROXY_TYPE_SOCKS5 else 1080))
    except (TypeError, ValueError):
        return None
    if not (0 < port < 65536):
        return None
    user = unquote(p.username) if p.username else None
    pwd = unquote(p.password) if p.password else None
    return (ptype, host, port, user, pwd)


class _SocksMixin:
    """把 connect() 需要的"建 socket"换成走 socks。"""

    socks_spec: Optional[ProxySpec] = None

    def _open_socks_socket(self):
        ptype, host, port, user, pwd = self.socks_spec
        return _socks.create_connection(
            (self.host, self.port), self.timeout, self.source_address,
            proxy_type=ptype, proxy_addr=host, proxy_port=port,
            # rdns=True：域名让代理解析（等价于 socks5h 的行为），
            # 这样国内被污染的域名能由代理侧给出正确地址
            proxy_rdns=True, proxy_username=user, proxy_password=pwd)


class SocksHTTPConnection(_SocksMixin, http.client.HTTPConnection):
    def connect(self):
        self.sock = self._open_socks_socket()
        if self._tunnel_host:
            self._tunnel()


class SocksHTTPSConnection(_SocksMixin, http.client.HTTPSConnection):
    def connect(self):
        sock = self._open_socks_socket()
        if self._tunnel_host:
            # 走 CONNECT 隧道时代理已经连通，TLS 要套在隧道之上
            self.sock = sock
            self._tunnel()
            sock = self.sock
        self.sock = self._context.wrap_socket(
            sock, server_hostname=self._tunnel_host or self.host)


def handlers(spec: ProxySpec, context: Any = None):
    """构造 urllib 的 handler 列表（http + https）。

    每次请求都会重新 build_opener，所以这里用**闭包内的子类**携带代理参数，
    而不是改类属性 —— 否则并发请求之间会互相覆盖。
    """
    class _Http(SocksHTTPConnection):
        socks_spec = spec

    class _Https(SocksHTTPSConnection):
        socks_spec = spec

    class _HttpHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_Http, req)

    class _HttpsHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_Https, req, context=context)

    return [_HttpHandler(), _HttpsHandler(context=context)]
