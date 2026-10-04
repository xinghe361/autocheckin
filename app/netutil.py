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
from urllib.parse import urlsplit

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


def normalize_proxy(raw: str) -> str:
    """把用户填的代理地址整理成 urllib 能用的形式。"""
    p = (raw or '').strip()
    if not p:
        return ''
    if '://' not in p:
        p = 'http://' + p
    return p


def mask_proxy_url(url: str) -> str:
    """把代理地址里的账号密码打码，用于日志/界面显示。

    为什么专门做：代理常写成 http://user:pass@host:port，
    直接打印就把代理凭据写进容器日志（NAS 日志面板谁都能看）。
    """
    s = (url or '').strip()
    if not s or '@' not in s:
        return s
    try:
        p = urlsplit(normalize_proxy(s))
        if not p.hostname:
            return s
        port = (':%d' % p.port) if p.port else ''
        return '%s://***@%s%s' % (p.scheme or 'http', p.hostname, port)
    except Exception:                                           # noqa: BLE001
        return s


def proxy_host(raw: str) -> str:
    """取出代理地址里的主机名（用于判断"是不是代理本身解析不了"）。"""
    p = normalize_proxy(raw)
    if not p:
        return ''
    try:
        return urlsplit(p).hostname or ''
    except Exception:                                           # noqa: BLE001
        return ''


# 代理地址里的占位符特征。用户从模板复制 compose 时很容易忘记替换，
# 而症状（域名解析失败）看起来像 DNS 故障，会把人带偏很久 —— 实测踩过。
_PLACEHOLDER_MARKS = ('你的', '你的代理', 'your-', 'your_', 'example.com',
                      'example.org', '<', 'xxxx', '请填', '待填', '替换')


def looks_like_placeholder_proxy(raw: str) -> bool:
    """这个代理地址看起来还是模板占位符吗？"""
    p = (raw or '').strip()
    if not p:
        return False
    host = proxy_host(p)
    if not host:
        return True
    low = host.lower()
    if any(m in low for m in _PLACEHOLDER_MARKS):
        return True
    # 主机名里有非 ASCII（比如中文）—— 合法域名不会这样，
    # 一定是从模板里抄来还没改。
    if any(ord(ch) > 127 for ch in host):
        return True
    return False


def _proxy_host_resolves(host: str) -> bool:
    """代理主机名本身能否解析？

    用途：DNS 失败时判断"是目标站点解析不了，还是代理地址写错了"。
    不能靠解析异常文本来判断 —— 各平台的报错文本不一致
    （Windows 只说"getaddrinfo failed"，不带主机名；Linux 带），
    所以主动解析一次才可靠。
    """
    if not host:
        return True
    try:
        socket.getaddrinfo(host, None)
        return True
    except Exception:                                           # noqa: BLE001
        return False


def classify_network_error(exc: BaseException, proxy: str = '') -> str:
    """把底层网络异常翻译成可读原因，便于通知与 AI 分析。

    注意 urllib 会把真实原因包在 URLError.reason 里（例如 gaierror），
    所以必须递归查看，否则 DNS 失败会被误判成"未知错误"。

    proxy 非空时，额外区分"解析不了目标站点"与"解析不了代理"：
    后者是配置问题，报成 DNS 故障会把人带偏（实测踩过这个坑）。
    """
    # urllib.error.URLError 会把原因放在 .reason
    reason = getattr(exc, 'reason', None)
    if isinstance(reason, BaseException):
        inner = classify_network_error(reason, proxy=proxy)
        if inner != 'unknown':
            return inner

    # 异常链也要看
    cause = getattr(exc, '__cause__', None)
    if isinstance(cause, BaseException) and cause is not exc:
        inner = classify_network_error(cause, proxy=proxy)
        if inner != 'unknown':
            return inner

    name = type(exc).__name__
    msg = str(exc)
    low = (name + ' ' + msg).lower()

    is_dns_failure = (
        isinstance(exc, socket.gaierror) or 'gaierror' in low
        or 'name or service not known' in low or 'nodename' in low
        or 'getaddrinfo' in low
    )
    if is_dns_failure:
        # 关键区分：是目标站点解析不了，还是代理本身就没法解析？
        if proxy and not _proxy_host_resolves(proxy_host(proxy)):
            return 'proxy_dns_error'
        return 'dns_error'

    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return 'timeout'
    if isinstance(exc, ConnectionRefusedError):
        return 'proxy_refused' if proxy else 'refused'
    if isinstance(exc, ConnectionResetError):
        return 'reset'

    if 'certificate' in low or 'ssl' in low or 'tls' in low:
        return 'tls_error'
    if 'timed out' in low or 'timeout' in low:
        return 'timeout'
    if 'connection refused' in low:
        return 'proxy_refused' if proxy else 'refused'
    if 'unreachable' in low:
        return 'unreachable'
    if 'reset' in low:
        return 'reset'
    return 'unknown'


def describe_network_error(kind: str, proxy: str = '') -> str:
    """给用户看的解释与建议。

    proxy 非空表示确实配了代理（此时失败原因指向代理）；
    为空表示按直连跑的，那就要提醒"这个站点可能需要代理"
    —— 实测 V2EX 直连超时、NodeSeek 直连被拒，配上代理就通了。
    """
    base = {
        'tls_error': '证书校验失败；若确认站点可信，可在该站点配置里放宽证书校验',
        'timeout': '连接超时',
        'refused': '连接被拒绝',
        'dns_error': '域名解析失败；检查 DNS 设置',
        'proxy_dns_error': ('连不上你配置的代理服务器（它的地址解析不了）；'
                            '请检查「设置 → 网络」里的代理地址是否填对 —— '
                            '注意模板里的占位符要换成真实地址。'
                            '这一条不是目标站点或 DNS 的问题'),
        'proxy_refused': ('代理服务器拒绝连接；请确认代理已启动、端口正确。'
                          '这一条不是目标站点的问题'),
        'unreachable': '网络不可达',
        'reset': '连接被重置；可能被中间设备拦截',
        'unknown': '未知网络错误',
    }.get(kind, '未知网络错误')

    # 直连时的失败，追加"可能需要代理"的提示。
    # 这几个 kind 都是"直连到不了站点"的典型症状。
    if not proxy and kind in ('timeout', 'refused', 'unreachable', 'reset'):
        base += ('；该站点可能需要通过代理访问'
                 '（国内部分站点/被污染的域名直连不通）。'
                 '可在「设置 → 网络」里配置代理后重试')
    return base
