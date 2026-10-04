"""站点与全局配置模型。

设计要点：
  * 每个站点既能用内置模板（V2EX / Chiphell / NodeSeek），也能用"录制"出来的自定义流程
  * 每站点独立控制：调度方式、随机偏移、重试、AI 触发、通知策略、证书校验
  * 全部可序列化成 dict，便于 WebDAV 备份与恢复
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .schedule import MODE_DAILY, MODE_SUCCESS_BASED, SiteSchedule

# 代理策略（通知渠道也复用这套取值）
PROXY_INHERIT = 'inherit'   # 跟随主代理设置
PROXY_DIRECT = 'direct'     # 直连，不走代理
PROXY_CUSTOM = 'custom'     # 使用单独填写的代理地址

# 通知策略
NOTIFY_ALL = 'all'          # 全部通知
NOTIFY_FAIL_ONLY = 'fail'   # 仅失败通知
NOTIFY_SUCCESS_ONLY = 'success'  # 仅成功通知
NOTIFY_CUSTOM = 'custom'    # 自定义（每站点单独开关）
NOTIFY_NONE = 'none'        # 不通知

NOTIFY_MODES = (NOTIFY_ALL, NOTIFY_FAIL_ONLY, NOTIFY_SUCCESS_ONLY,
                NOTIFY_CUSTOM, NOTIFY_NONE)

# 站点类型
KIND_TEMPLATE = 'template'   # 内置模板
KIND_CUSTOM = 'custom'       # 用户新增（录制/手填选择器）

# 一个"步骤"的动作类型（自定义站点用）
ACTION_GOTO = 'goto'
ACTION_CLICK = 'click'
ACTION_FILL = 'fill'
ACTION_WAIT = 'wait'
ACTION_WAIT_FOR = 'wait_for'
ACTION_ASSERT_TEXT = 'assert_text'
ACTION_EXTRACT = 'extract'


@dataclass
class Step:
    """自定义签到流程里的一步。"""

    action: str
    # 选择器（CSS）或 URL
    target: str = ''
    value: str = ''          # fill 的内容（可含 {username} {password} 占位符）
    timeout_ms: int = 15000
    # 成功判定：出现该选择器/文本即视为成功
    optional: bool = False
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'Step':
        return Step(
            action=d.get('action', ACTION_CLICK),
            target=d.get('target', ''),
            value=d.get('value', ''),
            timeout_ms=int(d.get('timeout_ms', 15000)),
            optional=bool(d.get('optional', False)),
            note=d.get('note', ''),
        )


@dataclass
class SiteConfig:
    """单个签到站点的完整配置。"""

    id: str
    name: str
    enabled: bool = True
    kind: str = KIND_TEMPLATE
    # 内置模板名（v2ex / chiphell / nodeseek）
    template: str = ''
    homepage: str = ''
    # 需要浏览器（Cloudflare / 复杂交互）时开启
    need_browser: bool = False
    # 该站点是否放宽证书校验（实测 chiphell 需要）
    verify_ssl: bool = True

    # --- 调度 ---
    mode: str = MODE_DAILY
    daily_hour: int = 3
    daily_minute: int = 0
    jitter_enabled: bool = False
    jitter_seconds: int = 3600
    retry_enabled: bool = True
    retry_count: int = 3
    retry_interval_minutes: int = 30
    ai_enabled: bool = True
    ai_after_failures: int = 3

    # --- 通知 ---
    notify: str = ''      # 留空 = 跟随全局策略
    notify_override: Optional[str] = None

    # --- 自定义流程 ---
    steps: List[Step] = field(default_factory=list)
    # 成功关键词（页面出现即算成功）
    success_keywords: List[str] = field(default_factory=list)
    # 失败关键词（出现即算失败，优先于成功判定）
    fail_keywords: List[str] = field(default_factory=list)

    # --- 自定义请求头 ---
    # 很多站点的接口会校验 Origin / Referer / Accept-Language 等，
    # 缺了就返回 403（实测 NodeSeek 的 /api/attendance 正是如此：
    # 只带 Cookie 是 403，补上 Origin 与 Referer 才 200）。
    # 这里按站点配置，回放时合并进请求头。
    headers: Dict[str, str] = field(default_factory=dict)

    # --- 站点级代理策略 ---
    # inherit = 跟随全局设置（默认）
    # direct  = 这个站点强制直连（比如国内站点不需要代理）
    # custom  = 这个站点用 proxy_url 里的地址
    proxy_mode: str = PROXY_INHERIT
    proxy_url: str = ''

    # --- 凭据（加密后存） ---
    username: str = ''
    password_enc: str = ''
    # Cookie 头字符串（如 "session=abc; pjwt=xyz"），Fernet 加密后存这里。
    # 用户粘贴什么格式都行，由 app.cookies 负责解析成这个名字->值的样子。
    cookie_enc: str = ''
    # 记录 cookie 的来源与更新时间，便于界面显示与"效期提醒"
    cookie_updated_at: int = 0
    cookie_source: str = ''      # 例如 'cURL 命令' / 'Cookie 串'
    # cookie 失效检查的结果：0=未检查过 1=有效 2=失效
    cookie_status: int = 0
    cookie_checked_at: int = 0
    cookie_error: str = ''       # 失效原因（供界面与通知展示）

    # --- 运行状态 ---
    state: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def to_schedule(self) -> SiteSchedule:
        """转成调度引擎用的对象（含从 state 恢复的运行状态）。"""
        st = self.state or {}
        return SiteSchedule(
            site_id=self.id,
            mode=self.mode,
            daily_hour=self.daily_hour,
            daily_minute=self.daily_minute,
            jitter_enabled=self.jitter_enabled,
            jitter_seconds=self.jitter_seconds,
            retry_enabled=self.retry_enabled,
            retry_count=self.retry_count,
            retry_interval_minutes=self.retry_interval_minutes,
            ai_enabled=self.ai_enabled,
            ai_after_failures=self.ai_after_failures,
            last_success_at=st.get('last_success_at'),
            last_attempt_at=st.get('last_attempt_at'),
            consecutive_failures=int(st.get('consecutive_failures') or 0),
            retries_done=int(st.get('retries_done') or 0),
            ai_analyzed_for_streak=int(st.get('ai_analyzed_for_streak') or 0),
        )

    def apply_schedule_state(self, sched: SiteSchedule) -> None:
        """把调度引擎的状态写回，便于持久化。"""
        self.state = dict(self.state or {})
        self.state.update({
            'last_success_at': sched.last_success_at,
            'last_attempt_at': sched.last_attempt_at,
            'consecutive_failures': sched.consecutive_failures,
            'retries_done': sched.retries_done,
            'ai_analyzed_for_streak': sched.ai_analyzed_for_streak,
        })

    def effective_notify(self, global_mode: str) -> str:
        """本站点实际生效的通知策略。"""
        if self.notify_override:
            return self.notify_override
        if self.notify:
            return self.notify
        return global_mode or NOTIFY_ALL

    def to_dict(self, include_secrets: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        d['steps'] = [s.to_dict() if isinstance(s, Step) else s for s in self.steps]
        if not include_secrets:
            for k in ('password_enc', 'cookie_enc'):
                d.pop(k, None)
        else:
            # 给了密文就没必要把明文 Cookie 一起带出去
            d.pop('cookie_header', None)
        # 界面需要的"是否已配置"标记（密文本身不外泄）
        d['has_cookie'] = bool(self.cookie_enc)
        d['has_password'] = bool(self.password_enc)
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'SiteConfig':
        steps = [Step.from_dict(s) if isinstance(s, dict) else s
                 for s in (d.get('steps') or [])]
        return SiteConfig(
            id=d['id'],
            name=d.get('name') or d['id'],
            enabled=bool(d.get('enabled', True)),
            kind=d.get('kind', KIND_TEMPLATE),
            template=d.get('template', ''),
            homepage=d.get('homepage', ''),
            need_browser=bool(d.get('need_browser', False)),
            verify_ssl=bool(d.get('verify_ssl', True)),
            mode=d.get('mode', MODE_DAILY),
            daily_hour=int(d.get('daily_hour', 3)),
            daily_minute=int(d.get('daily_minute', 0)),
            jitter_enabled=bool(d.get('jitter_enabled', False)),
            jitter_seconds=int(d.get('jitter_seconds', 3600)),
            retry_enabled=bool(d.get('retry_enabled', True)),
            retry_count=int(d.get('retry_count', 3)),
            retry_interval_minutes=int(d.get('retry_interval_minutes', 30)),
            ai_enabled=bool(d.get('ai_enabled', True)),
            ai_after_failures=int(d.get('ai_after_failures', 3)),
            notify=d.get('notify', '') or '',
            notify_override=d.get('notify_override'),
            steps=steps,
            success_keywords=list(d.get('success_keywords') or []),
            fail_keywords=list(d.get('fail_keywords') or []),
            headers=dict(d.get('headers') or {}),
            proxy_mode=str(d.get('proxy_mode') or PROXY_INHERIT),
            proxy_url=str(d.get('proxy_url') or ''),
            username=d.get('username', ''),
            password_enc=d.get('password_enc', ''),
            cookie_enc=d.get('cookie_enc', ''),
            cookie_updated_at=int(d.get('cookie_updated_at', 0) or 0),
            cookie_source=d.get('cookie_source', '') or '',
            cookie_status=int(d.get('cookie_status', 0) or 0),
            cookie_checked_at=int(d.get('cookie_checked_at', 0) or 0),
            cookie_error=d.get('cookie_error', '') or '',
            state=dict(d.get('state') or {}),
        )


def resolve_site_proxy(site: 'SiteConfig', global_proxy: str,
                       global_mode: str = PROXY_DIRECT) -> str:
    """算出某个站点最终该用哪个代理地址。

    返回空串表示直连。规则：
        inherit -> 跟随全局（全局是 direct 就用空；是 custom 就用全局地址）
        direct  -> 强制直连（返回空串）
        custom  -> 用站点自己的 proxy_url

    单独抽成函数是因为它决定"这个站点走不走代理"，
    是排查"为什么这个站点连不上"时第一个要看的东西，
    必须只有一处实现、可单测。
    """
    mode = (site.proxy_mode or PROXY_INHERIT).strip().lower()
    if mode == PROXY_DIRECT:
        return ''
    if mode == PROXY_CUSTOM:
        return (site.proxy_url or '').strip()
    # inherit
    if (global_mode or '').strip().lower() == PROXY_DIRECT:
        return ''
    return (global_proxy or '').strip()


def effective_global_proxy(proxy: str, mode: str) -> str:
    """全局代理的实际生效值：模式为直连时一律返回空串。"""
    if (mode or '').strip().lower() == PROXY_DIRECT:
        return ''
    return (proxy or '').strip()



@dataclass
class NotifyConfig:
    """通知渠道配置（可多选，同时推送）。"""

    # 全局策略
    mode: str = NOTIFY_ALL
    # 推送哪些渠道
    channels: List[str] = field(default_factory=list)

    # --- 代理策略（按用户要求：每个渠道可选走不走代理）---
    # 全局默认：'inherit' 跟随主代理设置 / 'direct' 直连 / 'custom' 用下面的地址
    proxy_mode: str = PROXY_INHERIT
    proxy_url: str = ''
    # 单渠道覆盖：{'telegram': {'mode': 'custom', 'url': 'http://...'}}
    channel_proxy: Dict[str, Dict[str, str]] = field(default_factory=dict)

    # pushplus（实测 200）
    pushplus_token: str = ''
    # Server 酱（实测 200）
    serverchan_key: str = ''
    # 企业微信机器人（需把出口 IP 加白名单，实测 403）
    wecom_webhook: str = ''
    # Telegram（需走代理，实测 200）
    tg_bot_token: str = ''
    tg_chat_id: str = ''

    # 只在这些站点上启用通知（mode=custom 时用）
    custom_sites: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'NotifyConfig':
        base = NotifyConfig()
        for k in base.to_dict():
            if k in (d or {}):
                setattr(base, k, d[k])
        # 兼容旧配置：channel_proxy 必须是 dict
        if not isinstance(base.channel_proxy, dict):
            base.channel_proxy = {}
        if not isinstance(base.channels, list):
            base.channels = []
        if not isinstance(base.custom_sites, list):
            base.custom_sites = []
        return base


@dataclass
class WebdavConfig:
    enabled: bool = False
    url: str = ''
    username: str = ''
    password_enc: str = ''
    # 自动备份间隔（分钟），0 = 关闭
    auto_interval_minutes: int = 0
    # 是否把"加密密钥"也一并备份到云端。
    # 默认关闭：密钥 + 备份文件 = 全部明文密码，属于安全取舍，必须由用户主动开启。
    backup_key: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'WebdavConfig':
        base = WebdavConfig()
        for k in base.to_dict():
            if k in (d or {}):
                setattr(base, k, d[k])
        return base


@dataclass
class AiConfig:
    """DeepSeek 分析设置（连续失败达到阈值时调用）。

    防烧 token 的完整策略（按用户要求设计）：
        每天每个站点最多尝试 N 次（max_attempts_per_day）；
        用完 N 次后当天只问 AI 一次（ai_calls_per_day）；
        AI 也没解决 -> 标记"问题站点"，当天不再尝试，并通知；
        第二天重新开始；若第二天 AI 同样没能解决 -> 暂停该站点并通知。
        另外还有全局每日调用总量上限（global_max_calls_per_day），
        防止"很多站点同时坏掉"把额度一次烧光。
    """

    enabled: bool = True
    api_key_enc: str = ''
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-chat'
    timeout: int = 60
    # 每天每站点最多分析多少次（防止无限烧 token）
    max_calls_per_day: int = 20
    # 是否允许 AI 直接改写站点流程（false 时只给建议，需人工确认）
    auto_apply: bool = True

    # --- 调用 AI 本身也失败时的处理 ---
    # 连续多少次"AI 调用失败"后，自动停止该站点的签到
    fail_threshold: int = 2
    # 是否启用"AI 也救不回来就停掉该站点"（并推送通知）
    auto_disable: bool = True

    # --- 每天尝试次数上限（用户要求：避免无限重试把额度烧光）---
    # 启用后，每个站点每天最多尝试这么多次；用完就等第二天。
    daily_limit_enabled: bool = True
    max_attempts_per_day: int = 3
    # 每天允许问 AI 的次数（默认 1 次：试完 N 次后问一次，问不出结果就等明天）
    ai_calls_per_day: int = 1
    # 全局每日调用总量上限（跨所有站点）。0 = 不限制。
    global_max_calls_per_day: int = 20

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'AiConfig':
        base = AiConfig()
        for k in base.to_dict():
            if k in (d or {}):
                setattr(base, k, d[k])
        return base


@dataclass
class AppConfig:
    """全局配置。"""

    version: int = 1
    # 全局代理开关：'direct' = 全部直连（不需要填地址）；
    #              'custom' = 走下面填的代理地址。
    # 为什么把"直连"也做成显式选项：以前只靠"地址留空"表示直连，
    # 用户从模板复制来的占位符地址会被当成真实代理，报错还是
    # "域名解析失败"，看起来像 DNS 故障（实测踩过）。
    proxy_mode: str = PROXY_DIRECT
    # 全局代理地址（proxy_mode='custom' 时才使用）
    proxy: str = ''
    timezone: str = 'Asia/Shanghai'
    # 内置浏览器可执行文件路径；留空则自动探测
    browser_path: str = ''
    # 无头模式
    headless: bool = True
    # 是否允许通过 CDP 连接外部已有的 Chrome（如 NAS 上的 Kasm Chrome）
    remote_cdp_url: str = ''

    # --- 访问控制 ---
    # 网页界面是否需要登录。
    # 为什么默认开启：接口能改代理（可劫持流量）、删站点、触发签到、取回密钥，
    # 若不加认证，同一局域网内任何人都能操作。详见 docs/SECURITY.md。
    auth_required: bool = True
    # 登录口令（加密存储）。留空表示"尚未设置"，此时首次访问会让你设置。
    auth_password_enc: str = ''
    # 已登录会话令牌的哈希（不存令牌本身）
    session_hashes: List[str] = field(default_factory=list)
    # 配套浏览器脚本（油猴）用的令牌哈希。
    # 与浏览器会话令牌分开存：这样可以"只吊销脚本访问权"而不影响你自己的登录，
    # 也便于在界面上单独看到"当前有几个脚本已授权"。
    api_token_hashes: List[str] = field(default_factory=list)

    notify: NotifyConfig = field(default_factory=NotifyConfig)
    webdav: WebdavConfig = field(default_factory=WebdavConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    sites: List[SiteConfig] = field(default_factory=list)

    def site(self, site_id: str) -> Optional[SiteConfig]:
        for s in self.sites:
            if s.id == site_id:
                return s
        return None

    def to_dict(self, include_secrets: bool = True) -> Dict[str, Any]:
        d = {
            'version': self.version,
            'proxy_mode': self.proxy_mode,
            'proxy': self.proxy,
            'timezone': self.timezone,
            'browser_path': self.browser_path,
            'headless': self.headless,
            'remote_cdp_url': self.remote_cdp_url,
            'auth_required': self.auth_required,
            'auth_password_enc': self.auth_password_enc,
            'session_hashes': list(self.session_hashes or []),
            'api_token_hashes': list(self.api_token_hashes or []),
            'notify': self.notify.to_dict(),
            'webdav': self.webdav.to_dict(),
            'ai': self.ai.to_dict(),
            'sites': [s.to_dict(include_secrets) for s in self.sites],
        }
        if not include_secrets:
            # 凭据字段一律不带出
            d['webdav'].pop('password_enc', None)
            d['ai'].pop('api_key_enc', None)
            d.pop('auth_password_enc', None)
            d.pop('session_hashes', None)
            d.pop('api_token_hashes', None)
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'AppConfig':
        # 旧配置没有 proxy_mode：有地址就视为"走代理"，没地址就直连。
        # 这样升级后行为不变，不会突然把已有代理关掉。
        raw_proxy = d.get('proxy', '') or ''
        mode = d.get('proxy_mode')
        if not mode:
            mode = PROXY_CUSTOM if raw_proxy.strip() else PROXY_DIRECT
        return AppConfig(
            version=int(d.get('version', 1)),
            proxy_mode=str(mode),
            proxy=raw_proxy,
            timezone=d.get('timezone', 'Asia/Shanghai'),
            browser_path=d.get('browser_path', '') or '',
            headless=bool(d.get('headless', True)),
            remote_cdp_url=d.get('remote_cdp_url', '') or '',
            auth_required=bool(d.get('auth_required', True)),
            auth_password_enc=d.get('auth_password_enc', '') or '',
            session_hashes=list(d.get('session_hashes') or []),
            api_token_hashes=list(d.get('api_token_hashes') or []),
            notify=NotifyConfig.from_dict(d.get('notify') or {}),
            webdav=WebdavConfig.from_dict(d.get('webdav') or {}),
            ai=AiConfig.from_dict(d.get('ai') or {}),
            sites=[SiteConfig.from_dict(s) for s in (d.get('sites') or [])],
        )


def default_config() -> AppConfig:
    """带三个内置站点的默认配置。"""
    from .templates import builtin_sites

    cfg = AppConfig()
    cfg.sites = builtin_sites()
    return cfg
