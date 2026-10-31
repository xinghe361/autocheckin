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

    def __init__(self, service, limiter: Optional[RateLimiter] = None):
        self.service = service
        self.limiter = limiter or RateLimiter()

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
}

# 只读端点：未登录时可访问的"安全信息"白名单（不含任何凭据）
PUBLIC_READ_PATHS = {
    '/api/auth/state',
}
