"""访问控制：网页界面的登录。

网页界面可以修改全局代理、删除站点、触发签到、恢复配置，因此默认需要口令。
在 host 网络模式下本服务直接使用宿主网络，更需要这一层保护。

设计取舍：
  * 默认开启认证；口令用 Fernet 加密存进 config.json
  * 登录后发一个随机令牌，服务端只存 **SHA-256 哈希**，不存令牌本身
  * 令牌放 Cookie（HttpOnly），**不放 URL**（URL 会进浏览器历史、代理日志、Referer）
  * 口令比较用 hmac.compare_digest，避免通过响应时间推断口令
  * 支持关闭认证（auth_required=False），但界面会给出提示
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

COOKIE_NAME = 'ac_session'
SESSION_TTL_SECONDS = 30 * 24 * 3600      # 30 天
MIN_PASSWORD_LEN = 6

# 登录尝试限速：窗口内超过次数就拒绝，避免被暴力破解
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW = 300


def hash_token(token: str) -> str:
    return hashlib.sha256(('autocheckin-session:' + token).encode('utf-8')).hexdigest()


def new_token() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    """常数时间比较，避免通过响应时间猜口令。"""
    try:
        return hmac.compare_digest(str(a), str(b))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- 配对码
# 用途：配套的浏览器脚本运行在**别的网站**上，它发往本容器的请求带不上
# 本容器的登录 Cookie（跨站），所以不能复用会话令牌。
# 又绝不能为此开一个"免认证"接口 —— 那等于把改配置、删站点、触发签到的
# 能力向所有能访问到本服务的人敞开。
#
# 折中做法：用户在网页上点一下就生成一个**一次性的短码**，
# 粘到浏览器脚本里，脚本拿它换一个专用令牌（存在脚本本地）。
#   * 短码只存 SHA-256 哈希，不存原文
#   * 10 分钟过期、用掉即作废（防止被截屏/粘贴后长期可用）
#   * 脚本拿到的令牌同样只存哈希，且可随时在网页上全部吊销
PAIR_TTL_SECONDS = 10 * 60
PAIR_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # 去掉易混的 I O 0 1
PAIR_LENGTH = 8


def new_pair_code() -> str:
    """生成一个人类易读的一次性配对码，形如 K7M2-9QXF。"""
    raw = ''.join(PAIR_ALPHABET[b % len(PAIR_ALPHABET)]
                  for b in os.urandom(PAIR_LENGTH))
    return '%s-%s' % (raw[:4], raw[4:])


def normalize_pair_code(code: str) -> str:
    """去掉分隔符与大小写差异，便于用户随便怎么粘都能对上。"""
    return ''.join(ch for ch in (code or '').upper()
                   if ch in PAIR_ALPHABET)


def hash_pair_code(code: str) -> str:
    return hashlib.sha256(
        ('autocheckin-pair:' + normalize_pair_code(code)).encode('utf-8')).hexdigest()


def hash_api_token(token: str) -> str:
    return hashlib.sha256(
        ('autocheckin-apitoken:' + token).encode('utf-8')).hexdigest()


@dataclass
class PairStore:
    """一次性配对码的内存存储（重启即失效，符合"临时"的定位）。"""

    ttl: int = PAIR_TTL_SECONDS
    _items: Dict[str, float] = field(default_factory=dict)

    def issue(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        self._sweep(now)
        code = new_pair_code()
        self._items[hash_pair_code(code)] = now + self.ttl
        return code

    def redeem(self, code: str, now: Optional[float] = None) -> bool:
        """用掉一个配对码。成功返回 True，并把该码作废。"""
        now = now if now is not None else time.time()
        self._sweep(now)
        h = hash_pair_code(code)
        exp = None
        for k in list(self._items):
            if safe_equal(k, h):
                exp = self._items.pop(k)   # 一次性：无论成功与否都消耗掉
                break
        if exp is None:
            return False
        return exp > now

    def pending(self, now: Optional[float] = None) -> int:
        now = now if now is not None else time.time()
        self._sweep(now)
        return len(self._items)

    def _sweep(self, now: float) -> None:
        for k in [k for k, exp in self._items.items() if exp <= now]:
            self._items.pop(k, None)


def new_api_token() -> str:
    """给浏览器脚本用的长效令牌。"""
    return hashlib.sha256(os.urandom(32)).hexdigest()


@dataclass
class AuthState:
    password_enc: str = ''
    session_hashes: List[str] = field(default_factory=list)
    required: bool = True

    def configured(self) -> bool:
        return bool(self.password_enc)


class RateLimiter:
    """按来源标记记录失败次数（纯内存，重启清空）。"""

    def __init__(self, limit: int = MAX_ATTEMPTS, window: int = ATTEMPT_WINDOW,
                 now_fn=None):
        self.limit = limit
        self.window = window
        self.now_fn = now_fn or time.time
        self._hits: Dict[str, List[float]] = {}

    def too_many(self, key: str) -> bool:
        now = self.now_fn()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        self._hits[key] = hits
        return len(hits) >= self.limit

    def record_failure(self, key: str) -> None:
        self._hits.setdefault(key, []).append(self.now_fn())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def remaining(self, key: str) -> int:
        now = self.now_fn()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        return max(0, self.limit - len(hits))


class AuthManager:
    """把配置里的口令与会话哈希，包装成可用的登录/校验逻辑。"""

    def __init__(self, service, limiter: Optional[RateLimiter] = None,
                 pairs: Optional[PairStore] = None):
        self.service = service
        self.limiter = limiter or RateLimiter()
        self.pairs = pairs or PairStore()

    # ------------------------------------------------------------ 状态
    def required(self) -> bool:
        return bool(self.service.load_config().auth_required)

    def configured(self) -> bool:
        return bool(self.service.load_config().auth_password_enc)

    def needs_setup(self) -> bool:
        """要求认证但还没设口令 → 需要引导用户设置。

        ⚠️ 判据不能只看"口令字段是否为空"：
            config.json 因为断电、并发写坏、被别的服务误写而变成空/坏 JSON 时，
            load_config() 会回退到"要求认证、无口令"的默认配置 —— 于是 setup
            端点对全网开放，局域网里第一个访问的人就能设口令接管容器，
            而且会拿默认配置把用户原有的站点/凭据整体覆盖掉（实测过）。

            所以再加一道判据：**数据目录里已经存在 config.json 文件**
            就说明这台实例早就初始化过了，绝不允许再走"首次设置"。
            文件存在是文件系统的客观事实，不会因为内容损坏而消失。

        另外 web.py 的 setup 端点还会校验同源与 Content-Type，
        防止被跨站页面抢先触发。
        """
        cfg = self.service.load_config()
        if not cfg.auth_required:
            return False
        if cfg.auth_password_enc:
            return False
        # 配置**存在但读不出来**（损坏/半截/被写成非对象）→ 绝不能开放 setup，
        # 否则局域网第一个访问者就能设口令接管，还会用默认配置覆盖用户数据。
        store = getattr(self.service, 'store', None)
        if getattr(store, 'config_unreadable', False):
            return False
        # 文件系统上的**持久证据**：config.json / .broken.* / .unreadable 哨兵
        # 任一存在，就说明这台实例初始化过 → 不再走首次设置流程。
        # ⚠️ 这里必须问文件系统而不是只看内存标记：内存标记重启即丢，
        # 而坏文件会被改名成 .broken.<ts>，只看"config.json 是否存在"
        # 会在重启后翻成"全新安装" —— 实测可被匿名接管。
        return not self._config_file_exists()

    def _config_file_exists(self) -> bool:
        """配置文件是否曾经存在过（含改名后的残留与持久哨兵）。"""
        try:
            from .store import was_ever_initialized
            store = getattr(self.service, 'store', None)
            path = getattr(store, 'config_path', None)
            if not path:
                return False
            return was_ever_initialized(path)
        except Exception:                                       # noqa: BLE001
            # 判定失败时**保守**地认为"已初始化"（返回 True → 不开放 setup）。
            # fail-open 在这里等于把容器交出去，绝不能那样。
            return True

    def password_matches(self, password: str) -> bool:
        cfg = self.service.load_config()
        if not cfg.auth_password_enc:
            return False
        plain, err = self.service.box.decrypt_checked(cfg.auth_password_enc)
        if err:
            return False
        return safe_equal(plain, password or '')

    # ------------------------------------------------------- 口令与会话
    def set_password(self, password: str) -> None:
        p = (password or '').strip()
        if len(p) < MIN_PASSWORD_LEN:
            raise ValueError('口令至少 %d 位' % MIN_PASSWORD_LEN)
        cfg = self.service.load_config()
        cfg.auth_password_enc = self.service.box.encrypt(p)
        # 改口令后让所有旧会话失效
        cfg.session_hashes = []
        self.service.save_config(cfg)

    def disable_auth(self) -> None:
        cfg = self.service.load_config()
        cfg.auth_required = False
        cfg.session_hashes = []
        self.service.save_config(cfg)

    def enable_auth(self) -> None:
        cfg = self.service.load_config()
        cfg.auth_required = True
        self.service.save_config(cfg)

    def login(self, password: str, source: str = '') -> Optional[str]:
        """校验口令并返回新令牌；失败返回 None。"""
        key = source or 'unknown'
        if self.limiter.too_many(key):
            raise PermissionError('尝试次数过多，请稍后再试（%d 秒窗口）'
                                  % self.limiter.window)
        if not self.password_matches(password):
            self.limiter.record_failure(key)
            return None
        self.limiter.reset(key)
        token = new_token()
        cfg = self.service.load_config()
        cfg.session_hashes = (list(cfg.session_hashes or []) + [hash_token(token)])[-20:]
        self.service.save_config(cfg)
        return token

    def is_valid(self, token: str) -> bool:
        if not token:
            return False
        cfg = self.service.load_config()
        h = hash_token(token)
        return any(safe_equal(h, x) for x in (cfg.session_hashes or []))

    def logout(self, token: str) -> None:
        if not token:
            return
        cfg = self.service.load_config()
        h = hash_token(token)
        cfg.session_hashes = [x for x in (cfg.session_hashes or [])
                              if not safe_equal(h, x)]
        self.service.save_config(cfg)

    def revoke_all(self) -> None:
        cfg = self.service.load_config()
        cfg.session_hashes = []
        self.service.save_config(cfg)

    # -------------------------------------------------- 浏览器脚本的令牌
    def issue_pair_code(self) -> str:
        """生成一次性配对码（供用户在浏览器脚本里输入）。"""
        return self.pairs.issue()

    def pending_pair_codes(self) -> int:
        return self.pairs.pending()

    def redeem_pair_code(self, code: str) -> Optional[str]:
        """用配对码换一个脚本专用令牌。码无效/过期/已用过都返回 None。"""
        if not self.pairs.redeem(code):
            return None
        token = new_api_token()
        cfg = self.service.load_config()
        cfg.api_token_hashes = (list(cfg.api_token_hashes or [])
                                + [hash_api_token(token)])[-20:]
        self.service.save_config(cfg)
        return token

    def api_token_valid(self, token: str) -> bool:
        if not token:
            return False
        cfg = self.service.load_config()
        h = hash_api_token(token)
        return any(safe_equal(h, x) for x in (cfg.api_token_hashes or []))

    def revoke_api_tokens(self) -> int:
        """吊销全部脚本令牌（你自己的登录不受影响）。返回吊销个数。"""
        cfg = self.service.load_config()
        n = len(cfg.api_token_hashes or [])
        cfg.api_token_hashes = []
        self.service.save_config(cfg)
        return n

    def api_token_count(self) -> int:
        return len(self.service.load_config().api_token_hashes or [])


# ------------------------------------------------------------------ Cookie

def parse_cookies(header: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (header or '').split(';'):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def make_cookie(token: str, max_age: int = SESSION_TTL_SECONDS) -> str:
    # HttpOnly 防脚本读取；SameSite=Lax 防跨站请求伪造触发写操作
    return '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax' % (
        COOKIE_NAME, token, max_age)


def clear_cookie() -> str:
    return '%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax' % COOKIE_NAME


# 公开端点：未登录也必须能访问（否则没法登录）
PUBLIC_PATHS = {
    '/healthz',
    '/api/auth/state',
    '/api/auth/login',
    '/api/auth/setup',
    # 浏览器脚本用配对码换令牌。这个端点本身需要出示有效配对码，
    # 而配对码必须由已登录的用户在网页上生成，所以不会形成"免认证后门"。
    '/api/pair/redeem',
}

# 只读端点：未登录时可访问的"安全信息"白名单（不含任何凭据）
PUBLIC_READ_PATHS = {
    '/api/auth/state',
}

# 配套浏览器脚本被允许访问的端点。
# 刻意收紧：脚本只需要"写入某站点的 Cookie"和"问一下自己配对成功没"。
# 不能让它改代理、删站点、触发签到、读配置 —— 即使脚本令牌泄露，
# 损失也仅限于"有人能覆盖站点的 Cookie"，而不是整个容器被接管。
SCRIPT_ALLOWED_PATHS = {
    ('POST', '/api/site/cookie'),
    ('POST', '/api/site/cookie/check'),
    ('GET', '/api/site/cookie'),
    ('GET', '/api/sites'),
}
