"""站点与全局配置模型。

设计要点：
  * 每个站点既能用内置模板（V2EX / Chiphell / NodeSeek），也能用"录制"出来的自定义流程
  * 每站点独立控制：调度方式、随机偏移、重试、AI 触发、通知策略、证书校验
  * 全部可序列化成 dict，便于 WebDAV 备份与恢复
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schedule import MODE_DAILY, MODE_SUCCESS_BASED, SiteSchedule


# --------------------------------------------------------------------------
# 读配置时的类型收敛
#
# 为什么必须有这些：config.json 是用户可以手改、也可能来自旧版本或备份的
# 文件。以前 from_dict 直接 `int(d.get(...))` / `dict(d.get(...))`，一旦
# 类型不对就抛异常 —— 而这发生在**启动阶段**，后果是整个容器起不来：
#     sites 是字符串      -> AttributeError: 'str' object has no attribute 'get'
#     ai 是 list          -> TypeError
#     headers 是 list     -> TypeError
#     state.json 是 list  -> AttributeError
# （以上都是实测出来的。）容器起不来时极空间 Docker 会按 restart 策略
# 反复重启，用户看到的现象就是"更新了镜像但版本一直是旧的"——
# 因为真正在跑的仍是旧容器，而新容器一直没起来。
#
# 所以这里全部改成"读不出来就用默认值 + 不抛异常"，宁可少一个字段，
# 也不能让整个服务无法启动。
# --------------------------------------------------------------------------
def _as_dict(value: Any) -> Dict[str, Any]:
    """只接受 dict；其它（list/str/None…）一律当空。"""
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    """只接受 list/tuple；其它一律当空。（字符串会被当单元素会很意外，所以排除。）"""
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_str(value: Any, default: str = '') -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return default


def _as_int(value: Any, default: int = 0, lo: Optional[int] = None,
            hi: Optional[int] = None) -> int:
    """转 int；非法值用默认；可选上下限夹取。布尔也接受（True->1）。"""
    try:
        if value is None or value == '':
            n = default
        else:
            n = int(value)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _as_bool(value: Any, default: bool = False) -> bool:
    """转 bool —— 刻意**不用** bool(value)。

    因为 bool("false") 是 True：用户手改配置写 "false" 会被当成开启，
    与直觉相反。这里认得 "false"/"0"/"no"/"off"/空 都算假。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('', '0', 'false', 'no', 'off', 'n', 'f'):
            return False
        if s in ('1', 'true', 'yes', 'on', 'y', 't'):
            return True
        return default
    return default


def _as_str_list(value: Any, limit: int = 200) -> List[str]:
    """字符串列表；逐项转字符串并丢掉空项。"""
    out = []
    for x in _as_list(value)[:limit]:
        if x is None:
            continue
        s = x if isinstance(x, str) else str(x)
        if s.strip():
            out.append(s)
    return out

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
        d = _as_dict(d)
        return Step(
            action=_as_str(d.get('action'), ACTION_CLICK),
            target=_as_str(d.get('target'), ''),
            value=_as_str(d.get('value'), ''),
            timeout_ms=_as_int(d.get('timeout_ms'), 15000, 0, 600000),
            optional=_as_bool(d.get('optional'), False),
            note=_as_str(d.get('note'), ''),
        )


SITE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$')


def safe_site_id(raw, fallback_name: str = '') -> str:
    """把站点标识收敛到安全字符集。

    为什么必须在**数据入口**做：站点 id 会被拼进页面的内联事件属性
    （`onclick="runSite('ID')"`）。那里是 **JS 字符串上下文**，
    HTML 实体转义（把 ' 变成 &#39;）不够 —— 浏览器会先把实体解码回 '
    再交给 JS 解析器，于是 id 里的引号能闭合字符串并注入任意代码。
    实测：id = `x'),alert(1),('y` 渲染出来点一下就执行。

    所以在写入/读取时就把 id 限制成 [A-Za-z0-9_-]。
    三条链路（保存、录制、从备份恢复）都经过 from_dict，堵这里最稳。

    非法时**不抛异常**：配置里已经存在脏 id 时抛异常会让整份配置读不出来。
    改成按名字生成一个合法 id（或退化为 site），并保证非空。
    """
    s = str(raw or '').strip()
    if SITE_ID_RE.match(s):
        return s
    base = re.sub(r'[^A-Za-z0-9_\-]+', '-', str(fallback_name or '').strip())
    base = base.strip('-')[:64]
    if base and not base[0].isalnum():
        base = 's' + base
    if base:
        return base[:64]
    # 兜底：取原值里合法字符拼一个（至少保证非空且字符集安全）
    cleaned = re.sub(r'[^A-Za-z0-9_\-]+', '', s)[:64]
    if cleaned and cleaned[0].isalnum():
        return cleaned
    return ('s' + cleaned)[:64] if cleaned else 'site'


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
    # mode=MODE_SUCCESS_BASED 时：距上次签到成功的间隔（分钟）。
    # 默认 1440 = 24 小时。以前这里是硬编码的 86400 秒，
    # 界面上显示的"上次成功后 XX 分钟"其实是重试间隔，显示与实际不符。
    success_interval_minutes: int = 1440
    # 【上次签到成功时间】模式的**初始基准时刻**（从当天 00:00 起的分钟数）。
    #
    # 为什么需要单独一个字段：用户要求"选这个模式时，把那一刻填的时刻
    # 锁定为基准"。不能复用 daily_hour/minute —— 那是"每天固定时刻"模式的
    # 配置，两个模式共用会导致切模式时互相覆盖。
    # 也不用 state 里的 last_success_at：那个每次运行结束都会被调度状态
    # 写回（apply_schedule_state），基准会被冲掉。
    #
    # 语义：基准 = max(这个初始锚点, 上次成功时间)。
    # 也就是首次成功之前按锚点算，首次成功之后锚点自然被成功时间取代。
    # -1 = 还没设置过（老配置/新建站点），此时按"现在 + 间隔"排第一次。
    success_anchor_minutes: int = -1
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
    def anchor_time_of_day(self) -> Optional[Tuple[int, int]]:
        """初始基准时刻 (小时, 分钟)；没设置过返回 None。"""
        m = int(getattr(self, 'success_anchor_minutes', -1) or -1)
        if m < 0:
            return None
        return (m // 60, m % 60)

    def set_anchor_from_time_of_day(self, hour: int, minute: int) -> None:
        """把初始基准设成指定的时刻（用于"选模式时锁定当前填的时间"）。"""
        h = max(0, min(23, int(hour)))
        mi = max(0, min(59, int(minute)))
        self.success_anchor_minutes = h * 60 + mi

    def to_schedule(self) -> SiteSchedule:
        """转成调度引擎用的对象（含从 state 恢复的运行状态）。"""
        st = self.state or {}
        return SiteSchedule(
            site_id=self.id,
            mode=self.mode,
            daily_hour=self.daily_hour,
            daily_minute=self.daily_minute,
            success_interval_minutes=self.success_interval_minutes,
            success_anchor_minutes=int(
                getattr(self, 'success_anchor_minutes', -1) or -1),
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
        d = _as_dict(d)                       # 非 dict（None/str…）当空配置
        steps_raw = []
        for s in _as_list(d.get('steps')):
            if isinstance(s, dict):
                steps_raw.append(Step.from_dict(s))
            elif isinstance(s, Step):
                steps_raw.append(s)
            # 其它类型直接丢掉：宁可少一步，也不要让整份配置读不出来
        return SiteConfig(
            id=safe_site_id(d.get('id'), d.get('name')),
            name=_as_str(d.get('name')) or _as_str(d.get('id')),
            enabled=_as_bool(d.get('enabled'), True),
            kind=_as_str(d.get('kind'), KIND_TEMPLATE),
            template=_as_str(d.get('template'), ''),
            homepage=_as_str(d.get('homepage'), ''),
            need_browser=_as_bool(d.get('need_browser'), False),
            verify_ssl=_as_bool(d.get('verify_ssl'), True),
            mode=_as_str(d.get('mode'), MODE_DAILY),
            daily_hour=_as_int(d.get('daily_hour'), 3, 0, 23),
            daily_minute=_as_int(d.get('daily_minute'), 0, 0, 59),
            success_interval_minutes=_as_int(
                d.get('success_interval_minutes'), 1440, 60, 10080),
            # -1 表示"还没设置过"；0..1439 是有效时刻
            success_anchor_minutes=_as_int(
                d.get('success_anchor_minutes'), -1, -1, 1439),
            jitter_enabled=_as_bool(d.get('jitter_enabled'), False),
            jitter_seconds=_as_int(d.get('jitter_seconds'), 3600, 0, 86400),
            retry_enabled=_as_bool(d.get('retry_enabled'), True),
            retry_count=_as_int(d.get('retry_count'), 3, 0, 10),
            retry_interval_minutes=_as_int(
                d.get('retry_interval_minutes'), 30, 1, 1440),
            ai_enabled=_as_bool(d.get('ai_enabled'), True),
            ai_after_failures=_as_int(d.get('ai_after_failures'), 3, 0, 100),
            notify=_as_str(d.get('notify'), ''),
            notify_override=(None if d.get('notify_override') is None
                             else _as_str(d.get('notify_override'))),
            steps=steps_raw,
            success_keywords=_as_str_list(d.get('success_keywords')),
            fail_keywords=_as_str_list(d.get('fail_keywords')),
            headers={str(k): _as_str(v)
                     for k, v in _as_dict(d.get('headers')).items()},
            proxy_mode=_as_str(d.get('proxy_mode'), PROXY_INHERIT),
            proxy_url=_as_str(d.get('proxy_url'), ''),
            username=_as_str(d.get('username'), ''),
            password_enc=_as_str(d.get('password_enc'), ''),
            cookie_enc=_as_str(d.get('cookie_enc'), ''),
            cookie_updated_at=_as_int(d.get('cookie_updated_at'), 0, 0),
            cookie_source=_as_str(d.get('cookie_source'), ''),
            cookie_status=_as_int(d.get('cookie_status'), 0),
            cookie_checked_at=_as_int(d.get('cookie_checked_at'), 0, 0),
            cookie_error=_as_str(d.get('cookie_error'), ''),
            state=_as_dict(d.get('state')),
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
    # 代理地址的密文版（代理常带 user:pass，明文落盘等于凭据泄露）。
    # 加载时由 Service 解密回上面的明文属性；保存时加密、明文不落盘。
    proxy_url_enc: str = ''

    # pushplus（实测 200）
    pushplus_token: str = ''
    # Server 酱（实测 200）
    serverchan_key: str = ''
    # 企业微信机器人（需把出口 IP 加白名单，实测 403）
    wecom_webhook: str = ''
    # Telegram（需走代理，实测 200）
    tg_bot_token: str = ''
    tg_chat_id: str = ''

    # --- 上面这些凭据的密文版 ---
    # 为什么单独列一组而不是直接改明文属性名：这样
    #   * 内存里仍用明文属性（notify.py 一行都不用改）
    #   * 磁盘上只存密文（明文在 to_dict 时被剥掉）
    #   * 老配置（只有明文）能平滑迁移：读出来照常用，下次保存自动加密
    pushplus_token_enc: str = ''
    serverchan_key_enc: str = ''
    wecom_webhook_enc: str = ''
    tg_bot_token_enc: str = ''
    tg_chat_id_enc: str = ''
    # 渠道代理地址打包后的密文（形如 "telegram\thttp://u:p@h:8" 的多行文本）
    channel_proxy_urls_enc: str = ''

    # 只在这些站点上启用通知（mode=custom 时用）
    custom_sites: List[str] = field(default_factory=list)

    # 落盘时需要加密的字段（明文属性名 -> 密文属性名）
    SECRET_FIELDS = {
        'pushplus_token': 'pushplus_token_enc',
        'serverchan_key': 'serverchan_key_enc',
        'wecom_webhook': 'wecom_webhook_enc',
        'tg_bot_token': 'tg_bot_token_enc',
        'tg_chat_id': 'tg_chat_id_enc',
        'proxy_url': 'proxy_url_enc',
        # 渠道代理地址打包后的那一份（见 plain_secrets）
        '_channel_proxy_urls': 'channel_proxy_urls_enc',
    }

    def to_dict(self) -> Dict[str, Any]:
        """导出配置。

        ⚠️ 内存里这些凭据是明文（notify.py 直接用），但**落盘时一律剥掉
        明文**，只留密文 —— 密文由 Service 在保存前调用 seal 填好。
        这样"忘了加密"最坏也只是丢字段，绝不会把明文写出去。
        """
        d = asdict(self)
        # _channel_proxy_urls 只是给加密用的中间键，不是真实字段
        for plain in self.SECRET_FIELDS:
            if plain.startswith('_'):
                continue
            d.pop(plain, None)
        # channel_proxy 里的 url 同样常带 user:pass，一并剥掉明文
        cp = d.get('channel_proxy')
        if isinstance(cp, dict):
            for _ch, v in cp.items():
                if isinstance(v, dict):
                    v.pop('url', None)
        return d

    def plain_secrets(self) -> Dict[str, str]:
        """当前内存里的明文凭据（供 Service 加密落盘）。

        注意 channel_proxy 的 url 在 to_dict() 里会被剥掉，所以这里
        必须直接从**明文属性**取，不能走 to_dict。
        """
        out = {}
        for k in self.SECRET_FIELDS:
            if k.startswith('_'):
                continue
            out[k] = _as_str(getattr(self, k, ''), '')
        cp = {ch: _as_str((v or {}).get('url'), '')
              for ch, v in (self.channel_proxy or {}).items()}
        out['_channel_proxy_urls'] = '\n'.join(
            '%s\t%s' % (k, v) for k, v in sorted(cp.items()) if v)
        return out

    def apply_secrets(self, plain: Dict[str, str]) -> None:
        """把解密出来的明文写回内存属性（供 Service 加载后调用）。"""
        for k, v in plain.items():
            if k.startswith('_'):
                continue
            if v:
                setattr(self, k, v)
        raw = plain.get('_channel_proxy_urls') or ''
        if raw:
            for line in raw.split('\n'):
                if '\t' not in line:
                    continue
                ch, url = line.split('\t', 1)
                ch, url = ch.strip(), url.strip()
                if not ch or not url:
                    continue
                self.channel_proxy.setdefault(ch, {})['url'] = url

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'NotifyConfig':
        # 逐字段按类型收敛，不再 setattr 原始值。
        # 以前是 `setattr(base, k, d[k])` —— 配置里 notify 写成字符串时
        # 会把整个 NotifyConfig 变成字符串属性，后续 .get() 直接崩，
        # 而且崩在启动阶段（容器起不来）。实测踩过。
        d = _as_dict(d)
        base = NotifyConfig()
        base.mode = _as_str(d.get('mode'), base.mode)
        base.channels = _as_str_list(d.get('channels'))
        base.custom_sites = _as_str_list(d.get('custom_sites'))
        # proxy_mode / proxy_url 已不再参与"走不走代理"的判定
        # （现在只看 channel_proxy），但字段仍在数据类里，
        # 旧配置里可能留着值 —— 必须原样往返，不能读一次就丢掉，
        # 否则用户保存一次设置就把历史值抹掉了（属于数据丢失）。
        base.proxy_mode = _as_str(d.get('proxy_mode'), base.proxy_mode)
        base.proxy_url = _as_str(d.get('proxy_url'), '')
        # 密文版字段：原样读入，解密由 Service 负责（models 不该依赖 secrets）。
        # ⚠️ 名单从 SECRET_FIELDS 派生，不手写 —— 手写过一次，
        # 漏了 channel_proxy_urls_enc，结果渠道代理地址读了就丢。
        for enc_name in NotifyConfig.SECRET_FIELDS.values():
            setattr(base, enc_name, _as_str(d.get(enc_name), ''))
        base.pushplus_token = _as_str(d.get('pushplus_token'), '')
        base.serverchan_key = _as_str(d.get('serverchan_key'), '')
        base.wecom_webhook = _as_str(d.get('wecom_webhook'), '')
        base.tg_bot_token = _as_str(d.get('tg_bot_token'), '')
        base.tg_chat_id = _as_str(d.get('tg_chat_id'), '')
        base.proxy_url = _as_str(d.get('proxy_url'), '')
        # channel_proxy: {渠道: {mode, url}} —— 逐层校验
        cp = {}
        for ch, v in _as_dict(d.get('channel_proxy')).items():
            v = _as_dict(v)
            cp[str(ch)] = {'mode': _as_str(v.get('mode'), 'inherit'),
                           'url': _as_str(v.get('url'), '')}
        base.channel_proxy = cp
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
        d = _as_dict(d)
        base = WebdavConfig()
        base.enabled = _as_bool(d.get('enabled'), False)
        base.url = _as_str(d.get('url'), '')
        base.username = _as_str(d.get('username'), '')
        base.password_enc = _as_str(d.get('password_enc'), '')
        base.auto_interval_minutes = _as_int(
            d.get('auto_interval_minutes'), 0, 0)
        base.backup_key = _as_bool(d.get('backup_key'), False)
        return base


@dataclass
class AiConfig:
    """DeepSeek 分析设置（连续失败达到阈值时调用）。

    防烧 token 的策略（统一逻辑，各字段各管一件事）：
        * 签到重试次数完全由【站点设置】的 retry_count 决定；
        * 全局 ai_after_failures：每连续失败几次调用一次 AI 分析
          （在它的整数倍处调用）。0 = 不启用 AI 分析。
        * 全局 max_calls_per_day：每天最多分析几次的**硬上限**，
          到了这个次数即使倍数规则还要求调用也不调用。0 = 不限制。
        * 全局 fail_threshold：连续几次"分析本身失败"后暂停该站点。
          0 = 不限制（永不停用）。

    这样"某个站点会被调用几次 AI"是可以直接算出来的：
        calls = ceil(重试次数 / ai_after_failures)，再受 max_calls_per_day 封顶。
    """

    enabled: bool = True
    api_key_enc: str = ''
    base_url: str = 'https://api.deepseek.com'
    model: str = 'deepseek-chat'
    timeout: int = 60
    # 是否允许 AI 直接改写站点流程（false 时只给建议，需人工确认）
    auto_apply: bool = True

    # --- 什么时候调用 AI 分析（全局）---
    # 每连续失败几次调用一次（在整数倍处），0 = 不启用 AI 分析。
    # 以前这个值放在每个站点上（site.ai_after_failures），
    # 用户要求统一成全局设置，站点只负责重试次数。
    ai_after_failures: int = 3
    # 每天最多分析几次（全局硬上限）。0 = 不限制。
    # 达到这个次数后，即使倍数规则还要求调用也不再调用。
    max_calls_per_day: int = 20

    # --- 调用 AI 本身也失败时的处理 ---
    # 连续多少次"AI 调用失败"后自动暂停该站点。
    #   0 = 不限制（每天都失败也照样继续问 AI，站点不会被停用）
    #   1 = 第一次 AI 失败就暂停
    # 只用一个数字表达"要不要停用 + 几次后停用"，不再单独设开关 ——
    # 之前 auto_disable 只是这个数字的开关，语义重复（用户指出）。
    fail_threshold: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> 'AiConfig':
        base = AiConfig()
        src = _as_dict(d)          # 非 dict（list/str/None）当空配置

        # --- 旧字段迁移（逻辑统一前存在过的那些）---
        # ai_calls_per_day（每站点每天调用 AI 几次）现在由
        # ai_after_failures 的倍数 + max_calls_per_day 共同决定。
        # 旧配置里如果显式设过，就把它当作"每天最多分析几次"的上限，
        # 这样用户原来的意图（少调用）不会丢。
        legacy_per_day = src.pop('ai_calls_per_day', None)
        src.pop('daily_limit_enabled', None)      # 已移除：不再限制每天尝试次数
        src.pop('max_attempts_per_day', None)     # 已移除：重试次数由站点决定
        legacy_global = src.pop('global_max_calls_per_day', None)
        # 更早的版本里 max_calls_per_day 存在过又被删过，语义相同，直接用

        # 逐字段按类型收敛，不再 setattr 原始值（否则 ai 写成 list/字符串
        # 会把配置搞坏，而且崩在启动阶段 -> 容器起不来）
        base.enabled = _as_bool(src.get('enabled'), base.enabled)
        base.api_key_enc = _as_str(src.get('api_key_enc'), '')
        base.base_url = _as_str(src.get('base_url'), base.base_url)
        base.model = _as_str(src.get('model'), base.model)
        base.timeout = _as_int(src.get('timeout'), base.timeout, 1, 600)
        base.auto_apply = _as_bool(src.get('auto_apply'), base.auto_apply)
        base.ai_after_failures = _as_int(
            src.get('ai_after_failures'), base.ai_after_failures, 0, 1000)
        base.max_calls_per_day = _as_int(
            src.get('max_calls_per_day'), base.max_calls_per_day, 0, 100000)
        base.fail_threshold = _as_int(
            src.get('fail_threshold'), base.fail_threshold, 0, 1000)

        if legacy_per_day is not None:
            n = _as_int(legacy_per_day, -1, 0)
            # 只在用户没自己设过上限（仍是默认 20）时迁移，避免覆盖新选择
            if n >= 0 and int(base.max_calls_per_day) == 20:
                base.max_calls_per_day = n
        elif legacy_global is not None:
            n = _as_int(legacy_global, -1, 0)
            if n >= 0 and int(base.max_calls_per_day) == 20:
                base.max_calls_per_day = n
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
    # 站点列表是否已被"显式管理"过（新增/删除站点、或加载过带站点的配置）。
    # 用途：区分两种"站点为空" ——
    #   False：全新配置或老配置，从没管理过 → 启动时补上内置站点
    #   True ：用户主动把站点全删了 → **绝不能**让内置站点复活
    # 只判断"站点是否为空"分不出这两者，实测过站点会自己回来继续签到。
    sites_initialized: bool = False

    # --- 站点模板插件同步（从 GitHub / https 地址拉模板）---
    # 需求原文：「自动可以设定几点拉一次，默认间隔为天，自动加上开关」
    # 关掉就只剩手动「立即同步」。
    plugin_sync_enabled: bool = False
    # 来源：owner/repo[/目录] 或 https 地址（**只允许 https**）
    plugin_sync_source: str = ''
    # 每天几点拉（'HH:MM'），配合下面的间隔天数
    plugin_sync_time: str = '03:00'
    plugin_sync_interval_days: int = 1

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
            'sites_initialized': bool(self.sites_initialized),
            'plugin_sync_enabled': bool(self.plugin_sync_enabled),
            'plugin_sync_source': self.plugin_sync_source,
            'plugin_sync_time': self.plugin_sync_time,
            'plugin_sync_interval_days': int(self.plugin_sync_interval_days),
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
        # 顶层也全部走类型收敛：这是启动路径，任何一处抛异常都会让
        # 容器起不来（用户看到的现象就是"换了镜像但版本没变"）。
        d = _as_dict(d)
        # 旧配置没有 proxy_mode：有地址就视为"走代理"，没地址就直连。
        # 这样升级后行为不变，不会突然把已有代理关掉。
        raw_proxy = _as_str(d.get('proxy'), '')
        mode = _as_str(d.get('proxy_mode'), '')
        if not mode:
            mode = PROXY_CUSTOM if raw_proxy.strip() else PROXY_DIRECT
        sites_raw = []
        for s in _as_list(d.get('sites')):
            if isinstance(s, dict):
                sites_raw.append(SiteConfig.from_dict(s))
            elif isinstance(s, SiteConfig):
                sites_raw.append(s)
            # 其它类型（None / str / 数字）直接跳过，不让它拖垮整份配置
        return AppConfig(
            version=_as_int(d.get('version'), 1),
            proxy_mode=mode,
            proxy=raw_proxy,
            timezone=_as_str(d.get('timezone'), 'Asia/Shanghai'),
            browser_path=_as_str(d.get('browser_path'), ''),
            headless=_as_bool(d.get('headless'), True),
            remote_cdp_url=_as_str(d.get('remote_cdp_url'), ''),
            # 老配置没有这个键 → 看它有没有站点：有站点就说明管过站点列表了。
            # 两者都没有时保持 False，首次启动仍会补上内置站点（行为不变）。
            sites_initialized=_as_bool(d.get('sites_initialized'), False)
            or bool(sites_raw),
            plugin_sync_enabled=_as_bool(d.get('plugin_sync_enabled'), False),
            plugin_sync_source=_as_str(d.get('plugin_sync_source'), ''),
            plugin_sync_time=_as_str(d.get('plugin_sync_time'), '03:00'),
            plugin_sync_interval_days=_as_int(
                d.get('plugin_sync_interval_days'), 1, 1, 365),
            auth_required=_as_bool(d.get('auth_required'), True),
            auth_password_enc=_as_str(d.get('auth_password_enc'), ''),
            session_hashes=_as_str_list(d.get('session_hashes')),
            api_token_hashes=_as_str_list(d.get('api_token_hashes')),
            notify=NotifyConfig.from_dict(d.get('notify')),
            webdav=WebdavConfig.from_dict(d.get('webdav')),
            ai=AiConfig.from_dict(d.get('ai')),
            sites=sites_raw,
        )


def default_config() -> AppConfig:
    """带三个内置站点的默认配置。"""
    from .templates import builtin_sites

    cfg = AppConfig()
    cfg.sites = builtin_sites()
    return cfg
