"""强制 IPv4 的网络工具。

背景（家用 NAS 环境实测）：
    v2ex.com / nodeseek.com / api.telegram.org 的 DNS 会优先返回 IPv6 地址，
    而这台机器的 IPv6 实际不通 —— curl 会直接卡死无响应。
    因此所有出站请求都必须强制走 IPv4，否则这些站点的签到必然失败。

同时处理：部分站点（实测 chiphell.com）证书链校验失败，可按需放宽。
"""

from __future__ import annotations

import socket
from typing import List, Optional, Tuple

# 原始 getaddrinfo：在导入时就固定下来，之后任何地方改动它都不会影响"还原"的目标。
# （踩过的坑：若还原时去读一个可变的模块级变量，一旦该变量被测试或第三方改写，
#   退出上下文就会把 socket.getaddrinfo 还原成一个错误的对象。）
# 原始 getaddrinfo：在导入时就固定下来，永不改动。__exit__ 的还原目标。
_TRUE_GETADDRINFO = socket.getaddrinfo

# 解析时实际委托的实现。测试可以替换它来注入假的 DNS 结果
# （替换它只影响"解析得到什么"，不会影响上下文退出时的还原目标）。
_REAL_GETADDRINFO = _TRUE_GETADDRINFO


def ipv4_only_getaddrinfo(*args, **kwargs):
    """只返回 IPv4 结果；若该域名没有 A 记录则回退到原始结果。

    委托给 _REAL_GETADDRINFO（导入时固定的真实实现），
    因此不会因为 socket.getaddrinfo 已被替换而递归调用自己。
    """
    results = _REAL_GETADDRINFO(*args, **kwargs)
    v4 = [r for r in results if r[0] == socket.AF_INET]
    if v4:
        return v4
    # 没有 A 记录（纯 IPv6 站点）时才回退，避免完全不可用
    return results


class force_ipv4:
    """上下文管理器：在其作用域内让所有 DNS 解析只返回 IPv4。

    用引用计数支持嵌套，避免内层退出时提前恢复。
    还原时始终回到 _TRUE_GETADDRINFO（导入时固定、永不改动），
    与当前 socket.getaddrinfo / _REAL_GETADDRINFO 是什么无关。

    （踩过的坑：原先还原时读的是可变模块变量，一旦该变量在上下文内被改动，
      退出后就会把 socket.getaddrinfo 留成一个假的实现——整个进程网络行为被污染。）
    """

    _depth = 0

    def __enter__(self):
        cls = type(self)
        if cls._depth == 0:
            socket.getaddrinfo = ipv4_only_getaddrinfo
        cls._depth += 1
        return self

    def __exit__(self, *exc):
        cls = type(self)
        cls._depth -= 1
        if cls._depth <= 0:
            cls._depth = 0
            socket.getaddrinfo = _TRUE_GETADDRINFO
        return False


def resolve_ipv4(host: str, port: int = 443, timeout: float = 5.0) -> List[str]:
    """解析出该域名的 IPv4 地址列表（只读探测，便于自检）。"""
    try:
        infos = _REAL_GETADDRINFO(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    seen = []
    for info in infos:
        if info[0] == socket.AF_INET and info[4][0] not in seen:
            seen.append(info[4][0])
    return seen


def has_ipv6_only(host: str, port: int = 443) -> bool:
    """判断某域名是否"只有 IPv6、没有 IPv4"——这种站点在纯 IPv4 环境下不可达。"""
    try:
        infos = _REAL_GETADDRINFO(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    has_v4 = any(i[0] == socket.AF_INET for i in infos)
    has_v6 = any(i[0] == socket.AF_INET6 for i in infos)
    return has_v6 and not has_v4


class dns_override:
    """上下文管理器：把指定域名的解析重定向到给定 IP（可同时覆盖多个）。

    用途：本地 DNS 被污染、又不想/不能走代理时，直接指定真实 IP。
    必须替换 socket.getaddrinfo 本身 —— urllib / requests 都直接调用它，
    只改 netutil 内部的委托变量是拦不住的（踩过这个坑）。
    """

    _depth = 0
    _saved = None

    def __init__(self, mapping: Optional[dict] = None):
        self.mapping = dict(mapping or {})

    def __enter__(self):
        cls = type(self)
        if cls._depth == 0:
            cls._saved = socket.getaddrinfo
            base = cls._saved
            mapping = self.mapping

            def patched(host, port, *args, **kwargs):
                ip = mapping.get(host)
                if ip:
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
                return base(host, port, *args, **kwargs)

            socket.getaddrinfo = patched
        else:
            # 嵌套：把新的映射并入已安装的补丁
            prev = socket.getaddrinfo
            mapping = self.mapping

            def patched_nested(host, port, *args, **kwargs):
                ip = mapping.get(host)
                if ip:
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))]
                return prev(host, port, *args, **kwargs)

            socket.getaddrinfo = patched_nested
        cls._depth += 1
        return self

    def __exit__(self, *exc):
        cls = type(self)
        cls._depth -= 1
        if cls._depth <= 0:
            cls._depth = 0
            socket.getaddrinfo = cls._saved or _TRUE_GETADDRINFO
            cls._saved = None
        return False


def build_ssl_context(verify: bool = True, cafile: Optional[str] = None):
    """构造 SSL 上下文。

    verify=False 用于证书链有问题的站点（实测 chiphell.com），
    调用方有责任把"已放宽校验"记录到日志/通知里，不能静默降级。
    """
    import ssl

    if verify:
        ctx = ssl.create_default_context(cafile=cafile)
        return ctx
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def classify_network_error(exc: BaseException) -> str:
    """把底层网络异常翻译成可读原因，便于通知与 AI 分析。

    注意 urllib 会把真实原因包在 URLError.reason 里（例如 gaierror），
    所以必须递归查看，否则 DNS 失败会被误判成"未知错误"。
    """
    # urllib.error.URLError 会把原因放在 .reason
    reason = getattr(exc, 'reason', None)
    if isinstance(reason, BaseException):
        inner = classify_network_error(reason)
        if inner != 'unknown':
            return inner

    # 异常链也要看
    cause = getattr(exc, '__cause__', None)
    if isinstance(cause, BaseException) and cause is not exc:
        inner = classify_network_error(cause)
        if inner != 'unknown':
            return inner

    name = type(exc).__name__
    msg = str(exc)
    low = (name + ' ' + msg).lower()

    if isinstance(exc, socket.gaierror) or 'gaierror' in low:
        return 'dns_error'
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return 'timeout'
    if isinstance(exc, ConnectionRefusedError):
        return 'refused'
    if isinstance(exc, ConnectionResetError):
        return 'reset'

    if 'certificate' in low or 'ssl' in low or 'tls' in low:
        return 'tls_error'
    if 'timed out' in low or 'timeout' in low:
        return 'timeout'
    if 'connection refused' in low:
        return 'refused'
    if 'name or service not known' in low or 'nodename' in low or 'getaddrinfo' in low:
        return 'dns_error'
    if 'unreachable' in low:
        return 'unreachable'
    if 'reset' in low:
        return 'reset'
    return 'unknown'


def describe_network_error(kind: str) -> str:
    """给用户看的解释与建议。"""
    return {
        'tls_error': '证书校验失败；若确认站点可信，可在该站点配置里放宽证书校验',
        'timeout': '连接超时；常见原因是该域名只有 IPv6 而本机 IPv6 不通',
        'refused': '连接被拒绝；端口不通或被防火墙拦截',
        'dns_error': '域名解析失败；检查 DNS 设置',
        'unreachable': '网络不可达；检查路由与代理',
        'reset': '连接被重置；可能被中间设备拦截',
        'unknown': '未知网络错误',
    }.get(kind, '未知网络错误')
