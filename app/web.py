"""HTTP 层与网页界面（只用标准库，不额外引入框架）。

路由全部是薄转发，真正的逻辑在 service.py，所以业务规则可以脱离 HTTP 单测。
"""

from __future__ import annotations

import gzip
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import auth as AUTH
from . import recorder as RC
from .pluginsync import DEFAULT_TEMPLATE_SOURCE

MAX_BODY = 4 * 1024 * 1024      # 4MB，够放录制事件流
MAX_SITE_ID = 64

# 站点标识只允许这些字符。
# 站点标识会进入 URL 路径与文件名场景，因此限制为字母、数字、下划线、连字符，
# 避免出现含路径分隔符等异常形状的值。
SITE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\-]{0,%d}$' % (MAX_SITE_ID - 1))


def validate_site_id(site_id: str) -> str:
    s = (site_id or '').strip()
    if not s:
        raise ApiError('站点标识不能为空', 400)
    if not SITE_ID_RE.match(s):
        raise ApiError(
            '站点标识不合法：只能用英文字母、数字、下划线、连字符（1-%d 位），'
            '且必须以字母或数字开头' % MAX_SITE_ID, 400)
    return s


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class WebApp:
    """把 service 暴露成 JSON 接口。维护路由表，可脱离 socket 单测。"""

    def __init__(self, service, scheduler=None, version: str = ''):
        self.service = service
        self.scheduler = scheduler
        self.version = version
        self.auth = AUTH.AuthManager(service)
        self.routes: Dict[Tuple[str, str], Callable[[dict, dict], Any]] = {}
        # 每请求上下文放**线程局部**（不是实例字段）。
        # 为什么：WebApp 是单例，而 HTTP 服务一请求一线程；
        # 用实例字段存"本次请求的令牌"会让并发请求互相串用，
        # 实测能让无令牌请求被别人的令牌授权（认证绕过）。
        self._local = threading.local()
        self._register()

    def _require_auth(self, method: str, path: str, req: Dict[str, Any],
                      token: str = '') -> None:
        """未登录时拒绝访问受保护端点。

        ⚠️ 令牌必须**按参数传入**（放在 req 里），绝不能存在 self 上：
            WebApp 是单例、而 ThreadingHTTPServer 是一请求一线程，
            把"本次请求的令牌"写在实例字段上会让并发请求互相串用 ——
            实测一个没有令牌的请求，只要和一个带合法令牌的请求并发，
            就能被对方的令牌授权而返回 200（确定性复现过）。
        """
        if not self.auth.required():
            return
        if path in AUTH.PUBLIC_PATHS:
            return
        if self.auth.needs_setup():
            # 还没设口令：只允许去设置，其它一律拒绝
            raise ApiError('尚未设置访问口令，请先完成初始化', 403)
        if self.auth.is_valid(token):
            return
        raise ApiError('未登录或登录已失效，请重新登录', 401)

    # ------------------------------------------------------------------
    def _register(self) -> None:
        r = self.routes

        def route(method: str, path: str):
            def deco(fn):
                r[(method.upper(), path)] = fn
                return fn
            return deco

        # --- 认证（公开）---
        @route('GET', '/api/auth/state')
        def _auth_state(q, body):
            return {
                'required': self.auth.required(),
                'configured': self.auth.configured(),
                'needs_setup': self.auth.needs_setup(),
                'feature_hint': '首次使用请设置访问口令；'
                                '口令只用于进入本界面，与各站点账号无关。',
            }

        @route('POST', '/api/auth/setup')
        def _auth_setup(q, body):
            # 只在"要求认证但还没设口令"时允许设置，避免被人抢先设一个。
            # needs_setup() 还额外排除了"配置损坏"的情况（那绝不能开放 setup）。
            if not self.auth.needs_setup():
                raise ApiError('口令已设置，不能重复初始化', 403)
            pw = str(body.get('password') or '')
            self.auth.set_password(pw)
            token = self.auth.login(pw, source='setup')
            if not token:
                raise ApiError('口令设置后登录失败，请重试', 500)
            self.current_request()['new_token'] = token
            return {'ok': True}

        @route('POST', '/api/auth/login')
        def _auth_login(q, body):
            source = (self.current_request().get('source') or 'unknown')
            try:
                token = self.auth.login(str(body.get('password') or ''),
                                        source=source)
            except PermissionError as e:
                raise ApiError(str(e), 429) from e
            if not token:
                raise ApiError('口令不正确', 401)
            self.current_request()['new_token'] = token
            return {'ok': True}

        @route('POST', '/api/auth/logout')
        def _auth_logout(q, body):
            ctx = self.current_request()
            self.auth.logout(ctx.get('token') or '')
            ctx['do_logout'] = True
            return {'ok': True}

        @route('POST', '/api/auth/password')
        def _auth_password(q, body):
            old = str(body.get('old') or '')
            new = str(body.get('new') or '')
            if not self.auth.password_matches(old):
                raise ApiError('当前口令不正确', 401)
            self.auth.set_password(new)
            return {'ok': True, 'note': '口令已更改，其它设备上的登录已失效'}

        @route('POST', '/api/auth/disable')
        def _auth_disable(q, body):
            if not self.auth.password_matches(str(body.get('password') or '')):
                raise ApiError('请输入正确的当前口令以确认', 401)
            self.auth.disable_auth()
            return {'ok': True,
                    'warning': '已关闭访问控制。建议只在完全可信的网络里这样做。'}

        @route('POST', '/api/auth/enable')
        def _auth_enable(q, body):
            """重新开启访问控制。

            这里**不要求口令**：开启本身不是危险操作（它只会让访问更严）。
            但要注意它**不会**重新打开"首次设置口令"的入口 ——
            AuthManager.needs_setup() 要求配置文件不存在，所以
            "先 enable 让 needs_setup 变真、再抢注口令"这条路走不通。
            """
            self.auth.enable_auth()
            return {'ok': True}

        # --- 概览 ---
        @route('GET', '/api/overview')
        def _overview(q, body):
            data = self.service.overview()
            if self.scheduler:
                data['scheduler'] = self.scheduler.status()
            return data

        # --- 站点 ---
        @route('GET', '/api/sites')
        def _sites(q, body):
            return {'sites': self.service.list_sites()}

        @route('GET', '/api/site')
        def _site(q, body):
            from .templates import site_option_specs, site_options
            sid = q.get('id', [''])[0]
            site = self.service.get_site(sid)
            if not site:
                raise ApiError('站点不存在：%s' % sid, 404)
            # 不返回任何凭据明文
            out = site.to_dict(include_secrets=False)
            # 模板可选项：表单要按它渲染下拉框。
            # 必须挂在**这个**接口上，因为编辑表单取的是 /api/site 而不是列表接口
            # （只加在 list_sites 里的话，界面上永远看不到这一栏 —— 踩过）。
            out['option_specs'] = site_option_specs(site)
            out['options'] = site_options(site)
            return {'site': out}

        @route('POST', '/api/site')
        def _site_save(q, body):
            # 站点标识会进 URL 路径与文件名场景，必须校验
            body = dict(body or {})
            body['id'] = validate_site_id(body.get('id', ''))
            return {'site': self.service.upsert_site(body).to_dict(include_secrets=False)}

        @route('POST', '/api/site/delete')
        def _site_delete(q, body):
            ok = self.service.delete_site(body.get('id', ''))
            if not ok:
                raise ApiError('站点不存在', 404)
            return {'ok': True}

        @route('POST', '/api/site/toggle')
        def _site_toggle(q, body):
            ok = self.service.set_site_enabled(body.get('id', ''),
                                               bool(body.get('enabled', True)))
            if not ok:
                raise ApiError('站点不存在', 404)
            return {'ok': True}

        # --- 站点登录 Cookie ---
        @route('GET', '/api/site/cookie')
        def _cookie_info(q, body):
            sid = q.get('id', [''])[0]
            try:
                return self.service.cookie_info(sid)
            except ValueError as e:
                raise ApiError(str(e), 404)

        @route('POST', '/api/site/cookie')
        def _cookie_set(q, body):
            """保存 Cookie。用户粘贴的格式由后端自动识别。"""
            sid = str(body.get('id') or '')
            raw = str(body.get('cookie') or '')
            if not raw.strip():
                raise ApiError('请先粘贴 Cookie 内容')
            try:
                return self.service.set_site_cookie(sid, raw)
            except ValueError as e:
                # 解析失败属于用户输入问题，返回 400 而不是 500
                raise ApiError(str(e), 400)

        @route('POST', '/api/site/cookie/clear')
        def _cookie_clear(q, body):
            if not self.service.clear_site_cookie(str(body.get('id') or '')):
                raise ApiError('站点不存在', 404)
            return {'ok': True}

        @route('POST', '/api/site/cookie/check')
        def _cookie_check(q, body):
            """检查 Cookie 是否有效。只读，不会触发签到。"""
            cfg = self.service.load_config()
            try:
                return self.service.check_site_cookie(str(body.get('id') or ''),
                                                      cfg.proxy or '')
            except ValueError as e:
                raise ApiError(str(e), 400)

        # --- 立即执行 ---
        @route('POST', '/api/run')
        def _run(q, body):
            return self.service.run_now(body.get('id') or None)

        # --- 执行记录（日历）---
        @route('GET', '/api/history')
        def _history(q, body):
            # id 为空 = 全部站点合并显示；month 为空 = 当前月
            return self.service.history_calendar(
                q.get('id', [''])[0] or None, q.get('month', [''])[0])

        # --- 录制（新增站点）---
        @route('POST', '/api/record/start')
        def _rec_start(q, body):
            return self.service.start_recording(
                str(body.get('id') or '').strip(),
                str(body.get('name') or ''),
                str(body.get('start_url') or '').strip())

        @route('POST', '/api/record/events')
        def _rec_events(q, body):
            events = body.get('events') or []
            sid = str(body.get('id') or '').strip()
            n = self.service.push_events(sid, events)
            return {'accepted': n}

        @route('GET', '/api/record/status')
        def _rec_status(q, body):
            return self.service.recording_status(q.get('id', [''])[0])

        @route('POST', '/api/record/stop')
        def _rec_stop(q, body):
            return self.service.stop_recording(
                str(body.get('id') or '').strip(),
                save=bool(body.get('save', True)),
                append=bool(body.get('append', True)))

        @route('POST', '/api/record/cancel')
        def _rec_cancel(q, body):
            return {'ok': self.service.cancel_recording(str(body.get('id') or ''))}

        @route('GET', '/api/record/script.js')
        def _rec_script(q, body):
            return Raw(RC.RECORDER_SCRIPT, 'application/javascript; charset=utf-8')

        # --- 设置 ---
        @route('GET', '/api/settings')
        def _settings(q, body):
            cfg = self.service.load_config()
            box = self.service.box
            data = cfg.to_dict(include_secrets=False)

            # 通知渠道的 token/webhook 是明文存的（不像 WebDAV/AI 走加密），
            # 所以必须在这里显式剥掉，只回"是否已配置"和打码形式。
            # 踩过的坑：只依赖 include_secrets=False 会让 pushplus token 明文外泄。
            notify_out = data['notify']
            for k in ('pushplus_token', 'serverchan_key', 'wecom_webhook',
                      'tg_bot_token', 'tg_chat_id'):
                notify_out.pop(k, None)
            notify_out['has_credentials'] = {
                'pushplus': bool(cfg.notify.pushplus_token),
                'serverchan': bool(cfg.notify.serverchan_key),
                'wecom': bool(cfg.notify.wecom_webhook),
                'telegram': bool(cfg.notify.tg_bot_token and cfg.notify.tg_chat_id),
            }
            notify_out['pushplus_token_masked'] = box.mask(cfg.notify.pushplus_token)
            notify_out['serverchan_key_masked'] = box.mask(cfg.notify.serverchan_key)
            notify_out['tg_bot_token_masked'] = box.mask(cfg.notify.tg_bot_token)

            data['webdav']['has_password'] = bool(cfg.webdav.password_enc)
            data['ai']['has_key'] = bool(cfg.ai.api_key_enc)
            return data

        @route('POST', '/api/settings')
        def _settings_save(q, body):
            cfg = self.service.load_config()
            self._apply_settings(cfg, body)
            self.service.save_config(cfg)
            return {'ok': True}

        # --- 备份 ---
        @route('POST', '/api/backup/now')
        def _backup(q, body):
            return self.service.backup_now()

        @route('POST', '/api/backup/restore')
        def _restore(q, body):
            return self.service.restore_from_cloud()

        @route('POST', '/api/backup/test')
        def _backup_test(q, body):
            return self.service.test_webdav(
                url=str(body.get('url') or ''),
                username=str(body.get('username') or ''),
                password=str(body.get('password') or ''))

        # --- 密钥 ---
        @route('GET', '/api/key')
        def _key_info(q, body):
            return self.service.key_info()

        @route('POST', '/api/key/backup')
        def _key_backup(q, body):
            return self.service.backup_key_now()

        @route('POST', '/api/key/restore')
        def _key_restore(q, body):
            return self.service.restore_key_from_cloud()

        # --- 通知测试 ---
        @route('POST', '/api/ai/models')
        def _ai_models(q, body):
            """列出这个 key 可用的 DeepSeek 模型（顺带验证 key）。"""
            return self.service.list_ai_models(str(body.get('api_key') or ''))

        @route('POST', '/api/notify/test')
        def _notify_test(q, body):
            return self.service.test_notify(str(body.get('channel') or ''))

        @route('POST', '/api/notify/alert-test')
        def _alert_test(q, body):
            return self.service.test_alert()

        # --- 站点模板与元数据（供界面下拉）---
        @route('GET', '/api/templates')
        def _templates(q, body):
            """可选站点模板（内置 + 插件），标注是否已加入。

            插件来自 DATA_DIR/templates/*.json —— 丢个文件进去就能多一个
            可选站点，**不用重建镜像**。
            """
            return {'templates': self.service.list_templates(),
                    'dir': self.service.plugin_dir(),
                    'errors': self.service.plugins.errors(),
                    'sync': self.service.plugin_sync_status()}

        @route('POST', '/api/templates/sync')
        def _templates_sync(q, body):
            """立即同步一次（手动就是立即）。"""
            from .pluginsync import SyncError
            src = str(body.get('source') or '').strip()
            try:
                rep = self.service.sync_plugins(src)
            except SyncError as e:
                raise ApiError('同步失败：%s' % e, 400) from e
            return rep

        @route('GET', '/api/templates/sync/status')
        def _templates_sync_status(q, body):
            return self.service.plugin_sync_status()

        @route('POST', '/api/templates/install')
        def _templates_install(q, body):
            """把选中的模板加入站点（幂等）。"""
            tid = str(body.get('id') or '').strip()
            if not tid:
                raise ApiError('缺少模板 id', 400)
            try:
                return self.service.install_template(tid)
            except ValueError as e:
                raise ApiError(str(e), 400) from e

        @route('POST', '/api/templates/reload')
        def _templates_reload(q, body):
            """重新扫描插件目录（用户手动放文件后用）。"""
            return self.service.reload_plugins()

        @route('GET', '/api/meta')
        def _meta(q, body):
            from .models import (NOTIFY_MODE_LABELS, NOTIFY_MODES,
                                 PROXY_INHERIT, PROXY_DIRECT, PROXY_CUSTOM)
            from .notify import ALL_CHANNELS
            # 用 service 的列表：它包含"镜像自带但还没落盘"的模板，
            # 也会把示例文档（example）排除掉。
            return {
                'version': self.version,
                'templates': [{'id': t.get('id'), 'name': t.get('name'),
                               'need_browser': t.get('need_browser'),
                               'verify_ssl': t.get('verify_ssl', True),
                               'note': t.get('note', ''),
                               'installed': bool(t.get('installed'))}
                              for t in self.service.list_templates()],
                'notify_modes': list(NOTIFY_MODES),
                'notify_mode_labels': dict(NOTIFY_MODE_LABELS),
                'channels': list(ALL_CHANNELS),
                'proxy_modes': [PROXY_INHERIT, PROXY_DIRECT, PROXY_CUSTOM],
                # 首次安装时「模板来源」输入框默认填这个，省得用户去翻文档
                'default_template_source': DEFAULT_TEMPLATE_SOURCE,
                'actions': ['goto', 'click', 'fill', 'wait', 'wait_for',
                            'assert_text', 'extract'],
            }

    # ------------------------------------------------------------------
    def _apply_settings(self, cfg, body: Dict[str, Any]) -> None:
        """把界面提交的设置写进配置；空字符串表示"不修改"凭据。"""
        if 'proxy_mode' in body:
            mode = str(body.get('proxy_mode') or '').lower()
            cfg.proxy_mode = mode if mode in ('direct', 'custom') else 'direct'
        if 'proxy' in body:
            cfg.proxy = str(body.get('proxy') or '')
        # 选了"直连"却还留着地址：不报错，但地址不参与生效
        # （effective_global_proxy 会按模式返回空串）。
        # 界面那边会把地址框隐藏起来，所以正常不会出现这种情况。
        if 'headless' in body:
            cfg.headless = bool(body['headless'])
        if 'remote_cdp_url' in body:
            cfg.remote_cdp_url = str(body.get('remote_cdp_url') or '')
        if 'browser_path' in body:
            cfg.browser_path = str(body.get('browser_path') or '')

        # --- 站点模板同步 ---
        if 'plugin_sync_enabled' in body:
            cfg.plugin_sync_enabled = bool(body['plugin_sync_enabled'])
        if 'plugin_sync_source' in body:
            # 只存来源字符串；非法写法在同步时才会被 pluginsync 拒绝并给出原因
            # （保存时不拦，否则用户没法保存"填了一半"的设置）
            cfg.plugin_sync_source = str(body.get('plugin_sync_source') or '')[:300]
        if 'plugin_sync_time' in body:
            from .pluginsync import parse_hhmm
            h, mi = parse_hhmm(str(body.get('plugin_sync_time') or ''))
            cfg.plugin_sync_time = '%02d:%02d' % (h, mi)
        if 'plugin_sync_interval_days' in body:
            try:
                v = int(body.get('plugin_sync_interval_days'))
            except (TypeError, ValueError):
                v = 1
            cfg.plugin_sync_interval_days = max(1, min(365, v))

        n = body.get('notify') or {}
        if n:
            nc = cfg.notify
            if 'mode' in n:
                nc.mode = str(n['mode'])
            if 'channels' in n and isinstance(n['channels'], list):
                nc.channels = [str(x) for x in n['channels']]
            if 'proxy_mode' in n:
                nc.proxy_mode = str(n['proxy_mode'])
            if 'proxy_url' in n:
                nc.proxy_url = str(n['proxy_url'])
            if 'channel_proxy' in n and isinstance(n['channel_proxy'], dict):
                nc.channel_proxy = n['channel_proxy']
            if 'custom_sites' in n and isinstance(n['custom_sites'], list):
                nc.custom_sites = [str(x) for x in n['custom_sites']]
            # 每日汇总
            if 'digest_enabled' in n:
                nc.digest_enabled = bool(n['digest_enabled'])
            if 'digest_time' in n:
                from .pluginsync import parse_hhmm
                h, mi = parse_hhmm(str(n.get('digest_time') or ''))
                nc.digest_hour, nc.digest_minute = h, mi
            # 凭据：非空才覆盖
            for field, key in (('pushplus_token', 'pushplus_token'),
                               ('serverchan_key', 'serverchan_key'),
                               ('wecom_webhook', 'wecom_webhook'),
                               ('tg_bot_token', 'tg_bot_token'),
                               ('tg_chat_id', 'tg_chat_id')):
                if n.get(key):
                    setattr(nc, field, str(n[key]))

        w = body.get('webdav') or {}
        if w:
            wc = cfg.webdav
            if 'url' in w:
                wc.url = str(w.get('url') or '')
            if 'username' in w:
                wc.username = str(w.get('username') or '')
            if 'enabled' in w:
                wc.enabled = bool(w['enabled'])
            if 'backup_key' in w:
                wc.backup_key = bool(w['backup_key'])
            if w.get('password'):
                wc.password_enc = self.service.box.encrypt(str(w['password']))

        a = body.get('ai') or {}
        if a:
            ac = cfg.ai
            if 'enabled' in a:
                ac.enabled = bool(a['enabled'])
            if 'model' in a:
                ac.model = str(a.get('model') or ac.model)
            if 'base_url' in a:
                ac.base_url = str(a.get('base_url') or ac.base_url)
            if 'auto_apply' in a:
                ac.auto_apply = bool(a['auto_apply'])
            # --- 什么时候调用 AI 分析（统一逻辑）---
            # 每连续失败几次调用一次；0 = 不启用 AI 分析
            if 'ai_after_failures' in a and str(a['ai_after_failures']) != '':
                ac.ai_after_failures = max(0, int(a['ai_after_failures']))
            # 每天调用次数的硬上限；0 = 不限制
            if 'max_calls_per_day' in a and str(a['max_calls_per_day']) != '':
                ac.max_calls_per_day = max(0, int(a['max_calls_per_day']))
            # AI 分析本身失败：连续几次后暂停该站点（0 = 不限制）
            if 'fail_threshold' in a and str(a['fail_threshold']) != '':
                ac.fail_threshold = max(0, int(a['fail_threshold']))
            # AI 分析时是否附带失败页面片段（默认关；见 AiConfig 的说明）
            if 'include_page_excerpt' in a:
                ac.include_page_excerpt = bool(a['include_page_excerpt'])
            if a.get('api_key'):
                ac.api_key_enc = self.service.box.encrypt(str(a['api_key']))

    # ------------------------------------------------------------------
    def dispatch(self, method: str, path: str,
                 query: Dict[str, list], body: Optional[dict] = None,
                 token: str = '', source: str = ''):
        """路由分发。返回 (状态码, 内容类型, 字节) 或 (状态码, dict)。

        token/source 由 HTTP 层从 Cookie 与请求头解析后传入。

        ⚠️ 这里**不使用任何实例字段**保存"本次请求"的状态（令牌、来源、
        待下发的 Cookie 标志）。WebApp 是单例，而 HTTP 服务是多线程的，
        用 self 存每请求状态会让并发请求互相串用 —— 实测可绕过认证：
        一个没有令牌的请求，只要和一个带合法令牌的请求并发，就能被对方的
        令牌授权而返回 200（确定性复现过）。
        所以每请求状态全部走局部变量，并在返回值里显式回传给 HTTP 层。
        """
        # 每请求独立的上下文（局部变量，不碰 self）
        req = {
            'token': token or '',
            'source': source or '',
            'new_token': '',
            'do_logout': False,
        }
        # ⚠️ 必须**立刻**挂到线程局部：端点处理函数会在执行过程中
        # 通过 current_request() 往里写（例如登录成功后写 new_token）。
        # 如果放到 dispatch 末尾才挂，handler 拿到的就是另一个临时字典，
        # 写入会被丢掉、Set-Cookie 发不出去（踩过这个坑）。
        # 唯一例外：外层（测试）已经塞了上下文，就不要覆盖它。
        if getattr(self._local, 'req', None) is None:
            self._local.req = req
        else:
            # 复用外层已挂的上下文（测试/同线程连续请求），但**必须重置**
            # new_token / do_logout —— 否则同一个 keep-alive 连接的后续请求
            # 会继承上一个请求的令牌：实测"登录后随便再来一次普通请求"
            # 会重复下发同一个会话 Cookie，甚至"登出"会把会话又塞回去。
            req = self._local.req
            req.update({'token': token or '', 'source': source or '',
                        'new_token': '', 'do_logout': False})

        fn = self.routes.get((method.upper(), path))
        if fn is None:
            alt = path.rstrip('/') or '/'
            fn = self.routes.get((method.upper(), alt))
            if fn is not None:
                path = alt
        if fn is None:
            raise ApiError('未知接口：%s %s' % (method, path), 404)

        # 访问控制在这里统一做，避免每个端点各自实现时漏掉
        self._require_auth(method.upper(), path, req, token or '')

        try:
            out = fn(query or {}, body or {})
        except ApiError:
            raise
        except ValueError as e:
            raise ApiError(str(e), 400) from e
        except PermissionError as e:
            raise ApiError(str(e), 429) from e
        except Exception as e:  # noqa: BLE001
            # 只给一句可读的话，不把内部路径/异常细节回显给客户端
            raise ApiError('服务器内部错误，请查看容器日志', 500) from e

        # 把本次请求的上下文挂到 **线程局部存储** 上（HTTP 层据此决定是否
        # 下发/清除 Cookie）。不能用 self.xxx：WebApp 是单例、服务是多线程，
        # 共享字段会让并发请求互相串用（实测能绕过认证）。
        # ThreadingHTTPServer 是一请求一线程，所以线程局部 == 请求局部。
        # （上面的 req 已经挂好了，这里不用再挂，只是保留这个说明。）
        if isinstance(out, Raw):
            return 200, out.content_type, out.data

        return 200, 'application/json; charset=utf-8', out

    def current_request(self) -> Dict[str, Any]:
        """取**本线程**正在处理的请求上下文（没有则返回空 dict）。"""
        return getattr(self._local, 'req', None) or {}

    @property
    def _new_token(self) -> str:
        """本次请求要下发的会话令牌（只读，取自线程局部）。

        保留这个名字只是为了兼容既有调用方/测试。
        ⚠️ 它**绝不参与认证判定** —— 认证只认 dispatch() 传入的令牌参数；
        任何"用 self 存令牌再拿来做鉴权"的写法都会在多线程下被绕过。
        """
        return self.current_request().get('new_token') or ''


class Raw:
    """已是最终字节的响应（用于返回注入脚本等）。"""

    def __init__(self, text: str, content_type: str = 'text/plain; charset=utf-8'):
        self.data = text.encode('utf-8')
        self.content_type = content_type


# ------------------------------------------------------------------ HTTP 服务器

def make_handler(webapp: WebApp, page_html: str):
    """构造请求处理器。"""
    # 首页是恒定内容（登录页/主界面由前端查 /api/auth/state 决定），
    # 提前编码并压好一份放这里：每次打开都要重传这个页面，而页面是
    # no-store 不能缓存，所以不能每次都花时间重新压。
    page_bytes = page_html.encode('utf-8')
    try:
        page_gzip = gzip.compress(page_bytes, 6)
    except Exception:                                           # noqa: BLE001
        page_gzip = b''

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AutoCheckin'
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):      # 静音：避免刷屏
            pass

        # -- 工具 --
        # 低于这个体积不压：gzip 自带约 20 字节的头尾，小响应压完反而更大。
        GZIP_MIN = 1024
        # 只压文本：图片/字体之类本来就已经压过，再压是白烧 CPU。
        GZIP_TYPES = ('text/', 'application/json', 'application/javascript',
                      'image/svg+xml')

        def _wants_gzip(self) -> bool:
            """客户端是否接受 gzip。

            整个页面（HTML+CSS+JS）内联在同一个响应里，约 120KB，压完 39KB。
            页面本身是 no-store（含会话状态，不能缓存），所以每次打开都要重传
            —— 手机上走公网时这 80KB 的差别是实打实的几百毫秒。
            """
            enc = (self.headers.get('Accept-Encoding') or '').lower()
            if 'gzip' not in enc:
                return False
            # `gzip;q=0` 是明确拒绝；顺手去掉空格好比对写法差异
            return 'gzip;q=0' not in enc.replace(' ', '')

        def _compressible(self, content_type: str) -> bool:
            ct = (content_type or '').lower()
            return any(ct.startswith(t) for t in self.GZIP_TYPES)

        def _send(self, status: int, content_type: str, payload: bytes,
                  extra_headers=None):
            headers = dict(extra_headers or {})
            if (len(payload) >= self.GZIP_MIN
                    and self._compressible(content_type)
                    and 'Content-Encoding' not in headers
                    and self._wants_gzip()):
                payload = gzip.compress(payload, 6)
                headers['Content-Encoding'] = 'gzip'
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            # 响应体随 Accept-Encoding 变化，必须声明，否则中间的代理可能
            # 把压缩过的版本发给不支持 gzip 的客户端
            self.send_header('Vary', 'Accept-Encoding')
            # 基础安全响应头
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _client_source(self) -> str:
            """限速用的来源标识（取不到就统一归到一个桶里）。"""
            return self.client_address[0] if self.client_address else 'unknown'

        def _session_token(self) -> str:
            return AUTH.parse_cookies(self.headers.get('Cookie', '')).get(
                AUTH.COOKIE_NAME, '')

        def _send_json(self, status: int, obj: Any, extra_headers=None):
            payload = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self._send(status, 'application/json; charset=utf-8', payload,
                       extra_headers)

        def _read_body(self) -> dict:
            # 反走私：这是 HTTP/1.1 keep-alive 服务，前后端如果对"请求体多长"
            # 解释不一致，残留字节会被当成下一个请求（前置反代时就是请求走私面）。
            # 三道闸门：
            #   1) 出现 Transfer-Encoding 直接拒绝（本服务只认 Content-Length）
            #   2) Content-Length 必须唯一（重复的会被 Message.get 取第一个）
            #   3) 超长时读掉并关闭连接，不能把 body 留在 socket 里
            if self.headers.get('Transfer-Encoding'):
                self.close_connection = True
                raise ApiError('不支持 Transfer-Encoding，请用 Content-Length', 400)
            raw_lens = self.headers.get_all('Content-Length') or []
            if len(raw_lens) > 1:
                self.close_connection = True
                raise ApiError('Content-Length 重复', 400)
            try:
                length = int(raw_lens[0]) if raw_lens else 0
            except (TypeError, ValueError):
                self.close_connection = True
                raise ApiError('Content-Length 不是合法数字', 400)
            if length < 0:
                self.close_connection = True
                raise ApiError('Content-Length 非法', 400)
            if length == 0:
                return {}
            if length > MAX_BODY:
                # 读掉再丢弃，保证连接上不残留半个 body
                self._drain(length)
                self.close_connection = True
                raise ApiError('请求体过大', 413)
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode('utf-8'))
            except ValueError as e:
                # 不回显 stdlib 的解析细节，只给一句可读的话
                raise ApiError('请求体不是合法 JSON') from e
            return data if isinstance(data, dict) else {'value': data}

        def _reject_body_on_bodyless_method(self) -> None:
            """GET/DELETE 带了请求体：排空并关闭连接。

            这类畸形请求是请求走私的原料（前端按自己的规则定 body 长度、
            后端把"body"当新请求解析）。本服务不处理它们，但要保证
            残留字节不会留在 keep-alive 连接上被当成下一个请求。
            """
            raw_lens = self.headers.get_all('Content-Length') or []
            if self.headers.get('Transfer-Encoding') or raw_lens:
                self.close_connection = True
                try:
                    n = int(raw_lens[0]) if raw_lens else 0
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    self._drain(n)

        def _drain(self, length: int, limit: int = 8 * 1024 * 1024) -> None:
            """把连接上剩余的请求体读掉丢弃（最多读 limit 字节，避免被拖死）。"""
            remaining = min(length, limit)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        # -- 方法 --
        def do_GET(self):
            self._handle('GET')

        def do_POST(self):
            self._handle('POST')

        def do_DELETE(self):
            self._handle('DELETE')

        def _handle(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == '/healthz':
                self._send_json(200, {'ok': True})
                return
            if path in ('/', '/index.html'):
                # 登录页还是主界面由前端查 /api/auth/state 决定，这里只发同一个页面
                if page_gzip and self._wants_gzip():
                    self._send(200, 'text/html; charset=utf-8', page_gzip,
                               {'Content-Encoding': 'gzip'})
                else:
                    self._send(200, 'text/html; charset=utf-8', page_bytes)
                return

            token = self._session_token()
            extra = None
            try:
                if method in ('POST', 'PUT'):
                    body = self._read_body()
                else:
                    # GET/DELETE 不该带请求体。带了就是畸形请求（走私原料），
                    # 直接排空并关闭连接，而不是"顺便读掉当正常"。
                    self._reject_body_on_bodyless_method()
                    body = {}
                status, ctype, out = webapp.dispatch(method, path, query, body,
                                                    token=token,
                                                    source=self._client_source())
                # 登录成功 → 下发会话 Cookie；登出 → 清除 Cookie。
                # 从**本线程**的请求上下文取，不能从共享实例字段取。
                ctx = webapp.current_request()
                if ctx.get('new_token'):
                    extra = {'Set-Cookie': AUTH.make_cookie(ctx['new_token'])}
                elif ctx.get('do_logout'):
                    extra = {'Set-Cookie': AUTH.clear_cookie()}
                if ctype.startswith('application/json'):
                    self._send_json(status, out, extra)
                else:
                    self._send(status, ctype, out, extra)
            except ApiError as e:
                self._send_json(e.status, {'error': e.message}, extra)
            except Exception:  # noqa: BLE001
                # 不把内部细节回显给客户端；细节只进日志
                self._send_json(500, {'error': '服务器内部错误，请查看容器日志'}, extra)

    return Handler


def serve(webapp: WebApp, host: str = '0.0.0.0', port: int = 8080,
          page_html: str = '', block: bool = True):
    """启动 HTTP 服务。返回 server 对象（block=False 时不阻塞）。"""
    handler = make_handler(webapp, page_html or INDEX_HTML)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    if block:
        httpd.serve_forever()
    else:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
    return httpd


# ------------------------------------------------------------------ 内置页面

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>自动签到</title>
<style>
:root{--bd:#e3e6ea;--fg:#2b2f36;--mut:#7a828c;--pri:#2f6ae8;--ok:#1e8a4c;--err:#c0392b}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     color:var(--fg);background:#f6f7f9}
header{background:#fff;border-bottom:1px solid var(--bd);padding:12px 20px;
       display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0}
header .mut{color:var(--mut);font-size:12px}
nav{margin-left:auto;display:flex;gap:6px}
nav button{border:1px solid var(--bd);background:#fff;border-radius:5px;
           padding:5px 12px;cursor:pointer;font-size:13px}
nav button.on{background:var(--pri);border-color:var(--pri);color:#fff}
main{padding:18px 20px;max-width:1080px;margin:0 auto}
.card{background:#fff;border:1px solid var(--bd);border-radius:8px;padding:14px 16px;
      margin-bottom:14px}
.card h2{font-size:14px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #f0f2f5}
th{color:var(--mut);font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}
button.b{border:1px solid var(--bd);background:#fff;border-radius:5px;padding:4px 10px;
         cursor:pointer;font-size:12px}
button.b:hover{background:#f2f5fa}
button.pri{background:var(--pri);border-color:var(--pri);color:#fff}
button.danger{color:var(--err);border-color:#f0c4c0}
label{display:block;font-size:12px;color:var(--mut);margin:10px 0 3px}
input,select,textarea{width:100%;padding:6px 9px;border:1px solid var(--bd);
                      border-radius:5px;font-size:13px;font-family:inherit}
textarea{min-height:96px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>div{flex:1;min-width:180px}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
     background:#eef1f5;color:#5a636e}
.tag.ok{background:#e4f6ea;color:var(--ok)}
.tag.err{background:#fdecea;color:var(--err)}
.tag.evo{background:#fde8ef;color:#c2185b}
.hint{font-size:12px;color:var(--mut);margin-top:6px}
.msg{margin-top:10px;padding:8px 11px;border-radius:6px;font-size:12.5px;
     white-space:pre-wrap;display:none}
.msg.ok{display:block;background:#e9f7ee;border:1px solid #bfe4cb;color:#1e6b3a}
.msg.err{display:block;background:#fdecea;border:1px solid #f5c2bd;color:#a03a30}
.hide{display:none!important}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}

/* ===== 站点卡片（替代表格：编辑表单要能内联展开在卡片下方） ===== */
.site{background:#fff;border:1px solid var(--bd);border-radius:8px;
      padding:12px 14px;margin-bottom:10px}
.site.open{border-color:var(--pri);box-shadow:0 2px 10px rgba(45,106,220,.10)}
.site .hd{display:flex;justify-content:space-between;align-items:flex-start;
          gap:12px;flex-wrap:wrap}
.site .nm{font-weight:600;font-size:13.5px}
.site .meta{font-size:12px;color:var(--mut);margin-top:4px}
.site .acts{display:flex;gap:6px;flex-wrap:wrap}
.site form{border-top:1px dashed var(--bd);margin-top:12px;padding-top:4px}
/* 编辑时只有配置项滚动：卡片头的名称/标签/下次运行/按钮保持不动。
   用内层滚动而不是让整页滚，滚轮停在表单上时页面不会跟着跑；
   overscroll-behavior 防止滑到底后"传染"给页面。 */
.site.open > div[id^="form-"]{
  max-height:calc(100vh - 230px);overflow-y:auto;overscroll-behavior:contain;
  padding-right:6px}
.site.open > div[id^="form-"]::-webkit-scrollbar{width:8px}
.site.open > div[id^="form-"]::-webkit-scrollbar-thumb{
  background:#d6dbe4;border-radius:4px}
.fsec{margin-top:12px;padding-top:10px;border-top:1px solid #f0f2f5}
.fsec:first-of-type{border-top:none;padding-top:0}
.fsec>h4{font-size:12px;color:var(--mut);margin:0 0 6px;font-weight:600}
.fgrid{display:flex;gap:14px;flex-wrap:wrap}
.fgrid>div{flex:1;min-width:170px}
/* 滑动开关 */
.sw{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none;
    margin-top:8px}
.sw input{display:none}
.sw .track{width:38px;height:21px;border-radius:11px;background:#cfd6e0;
           position:relative;transition:background .16s;flex:none}
.sw .track::after{content:'';position:absolute;top:2px;left:2px;width:17px;height:17px;
                  border-radius:50%;background:#fff;transition:left .16s;
                  box-shadow:0 1px 3px rgba(0,0,0,.25)}
.sw input:checked+.track{background:var(--pri)}
.sw input:checked+.track::after{left:19px}
.sw .lb{font-size:12.5px;color:#3a424d}
.sw.off .lb{color:var(--mut)}
/* 单选行 */
.radios{display:flex;gap:16px;flex-wrap:wrap;margin-top:6px}
.radios label{display:inline-flex;align-items:center;gap:5px;margin:0;
              font-size:12.5px;color:#3a424d;cursor:pointer}
.radios input{width:auto;margin:0}
/* 内联小输入 */
.inline{display:inline-flex;align-items:center;gap:5px;font-size:12.5px}
.inline input{width:66px;padding:3px 6px}
.inline select{width:auto;min-width:96px}
/* Cookie 区 */
.ckstat{font-size:12.5px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ckbox{background:#f7f9fc;border:1px solid var(--bd);border-radius:6px;padding:10px;
       margin-top:8px}
.ckbox textarea{min-height:70px;background:#fff}
.ckbox .tip{font-size:11.5px;color:var(--mut);line-height:1.6;margin-top:6px}
.ckbox .tip b{color:#3a424d}
.formfoot{display:flex;gap:8px;margin-top:14px;align-items:center;flex-wrap:wrap}
.formfoot .grow{flex:1}
/* 设置页顶部的保存条：吸顶，长表单滚动时始终能点到。
   加背景与下边框，避免滚动时和下面的卡片糊在一起。
   top 用 header 的高度（header 本身也是 sticky，z-index 同为 5，
   这里放 top:0 会滑到 header 下面，所以往下让一点）。 */
.settings-save{position:sticky;top:62px;z-index:4;margin:0 0 12px;
  padding:10px 12px;background:#f6f7f9;border-bottom:1px solid var(--bd)}
.kvrow{display:flex;gap:6px;margin-bottom:6px}
.kvrow input:first-child{flex:0 0 34%}
.kvrow input{flex:1}
.paths{font-size:12px;color:var(--mut);margin-top:4px}

/* ===== 执行记录（日历） =====
   配色只用"绿=全部成功 / 红=有失败 / 灰=没跑过"三态。
   刻意不加黄/橙：一眼扫过去要能立刻分出"这天有没有问题"，
   中间态会让人每天都要点开确认一次。 */
.calbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.calbar select{width:auto;min-width:170px}
.calbar .mtitle{font-weight:600;font-size:13.5px;min-width:104px;text-align:center}
.calgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-top:12px}
.calhead{font-size:11.5px;color:var(--mut);text-align:center;padding:2px 0}
.calcell{position:relative;border:1px solid var(--bd);border-radius:6px;
  min-height:58px;padding:5px 6px;cursor:pointer;background:#fff;
  font-size:12px;transition:box-shadow .12s}
.calcell:hover{box-shadow:0 1px 6px rgba(0,0,0,.13)}
.calcell.ok{background:#e8f8ee;border-color:#b7e3c6}
.calcell.fail{background:#fdecea;border-color:#f3c0bb}
.calcell.none{background:#fafbfc;color:#a7aeb8}
.calcell.future{opacity:.45}
.calcell.sel{outline:2px solid var(--pri);outline-offset:1px}
.calcell.empty{background:transparent;border:none;cursor:default;min-height:0}
.calcell .dnum{font-weight:600;font-size:12.5px}
.calcell.ok .dnum{color:var(--ok)}
.calcell.fail .dnum{color:var(--err)}
.calcell .cmeta{font-size:10.5px;line-height:1.35;margin-top:2px;color:#5a636e}
.calcell.today::after{content:'';position:absolute;top:4px;right:4px;width:5px;
  height:5px;border-radius:50%;background:var(--pri)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);
  margin-top:10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;
  border:1px solid var(--bd);margin-right:4px;vertical-align:-1px}
.daylist{margin-top:12px}
.daylist .dhead{font-size:12.5px;font-weight:600;margin-bottom:4px}
.daylist .it{display:flex;gap:10px;padding:6px 2px;border-bottom:1px solid #f2f4f7;
  font-size:12.5px;align-items:baseline}
.daylist .it:last-child{border-bottom:none}
.daylist .tm{color:var(--mut);font-family:ui-monospace,Consolas,monospace;
  font-size:11.5px;flex:none;width:42px}
.daylist .st{flex:none;width:46px;font-size:11.5px}
.daylist .bd{flex:1;min-width:0;word-break:break-word}
/* 执行步骤（点开某条明细才展开）。
   默认收起：大部分时候只想知道"成没成"，只有要排查时才需要看走了哪几步。 */
.itstep{margin:-2px 0 8px 52px;font-size:11.5px;color:var(--mut)}
.itstep summary{cursor:pointer;color:#5b6470;outline:none;user-select:none}
.itstep summary:hover{color:var(--pri)}
.itstep ol{margin:4px 0 0;padding-left:18px;line-height:1.7}
.itstep li{word-break:break-all;font-family:ui-monospace,Consolas,monospace;
  font-size:11px}
/* 失败原因：明细里的小色块，以及下方的「按大类汇总」。
   颜色跟着大类走（登录/拦截/网络/站点/规则），不用读文字也能分出该找谁。 */
.rchip{display:inline-block;padding:0 6px;border-radius:8px;font-size:11px;
  border:1px solid currentColor;line-height:1.7;margin-right:3px;
  cursor:help;white-space:nowrap}
.rhead{font-size:12.5px;font-weight:600;margin:16px 0 8px}
.rgroup{margin-bottom:11px}
.rg{display:flex;align-items:center;gap:6px;font-size:12px;color:#3a424d}
.rdot{width:7px;height:7px;border-radius:50%;flex:none;display:inline-block}
.rg .rcnt{margin-left:auto;color:var(--mut);font-size:11.5px}
.rbar{height:5px;border-radius:3px;background:#f0f2f5;margin:4px 0 1px;
  overflow:hidden}
.rbar i{display:block;height:100%;border-radius:3px}
.rrow{display:flex;gap:10px;align-items:baseline;font-size:12px;padding:4px 6px;
  border-radius:5px;cursor:pointer}
.rrow:hover{background:#f7f9fc}
.rrow.on{background:#f2f5fa}
/* 给原因名一个最小宽度：各行说明文字的起点才能对齐，
   否则"被限流"和"被拒绝（403）"下面那一列会参差不齐。 */
.rrow .rname{font-weight:600;flex:none;min-width:96px}
.rrow .rsite{color:var(--mut);font-size:11.5px;flex:1;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rrow .rcnt{color:var(--mut);font-size:11.5px;flex:none;min-width:76px;
  text-align:right}
.rrow .rlast{color:#a7aeb8;font-size:11px;flex:none;text-align:right;
  min-width:80px}
.revlist{margin:2px 0 6px 13px;padding-left:10px;border-left:2px solid #eef1f5}
.revlist .it{border-bottom:none;padding:3px 2px;gap:12px}
.revlist .rt{color:var(--mut);font-size:11px;flex:none;min-width:78px;
  font-family:ui-monospace,Consolas,monospace}
/* 窄屏：上面那套"四列一行"在 390px 上会把说明文字压成只剩一两个字。
   改成两行 —— 第一行原因+次数+时间，第二行整行放站点/原文。 */
@media (max-width:560px){
  .rrow{flex-wrap:wrap;gap:3px 10px}
  .rrow .rname{min-width:0;flex:1 1 auto;order:1}
  .rrow .rcnt{min-width:0;order:2}
  .rrow .rlast{min-width:0;order:3}
  .rrow .rsite{flex:1 1 100%;order:4;padding-left:2px}
  .revlist{margin-left:6px}
  .calcell .cmeta{font-size:9.5px}
}
</style>
</head>
<body>
<header>
  <h1>自动签到</h1>
  <span class="mut" id="ver"></span>
  <nav id="mainNav">
    <button data-tab="sites" class="on">站点</button>
    <button data-tab="history">记录</button>
    <button data-tab="add">新增站点</button>
    <button data-tab="settings">设置</button>
    <button id="btnLogout" style="margin-left:8px">退出登录</button>
  </nav>
</header>

<!-- ============ 登录 / 初始化口令 ============ -->
<section id="tab-login" class="hide">
  <div class="card" style="max-width:460px;margin:40px auto">
    <h2 id="loginTitle">需要登录</h2>
    <div class="hint" id="loginHint">
      本界面可以修改代理、查看/恢复密钥、删除站点，因此默认需要口令。
      该口令只用于进入本界面，与你的各站点账号无关。
    </div>
    <label>访问口令</label>
    <input id="loginPw" type="password" placeholder="至少 6 位"
           onkeydown="if(event.key==='Enter')doLogin()">
    <label id="loginPw2Wrap" class="hide">再输一次确认</label>
    <input id="loginPw2" type="password" class="hide"
           onkeydown="if(event.key==='Enter')doLogin()">
    <div style="margin-top:12px">
      <button class="b pri" onclick="doLogin()" id="loginBtn">登录</button>
    </div>
    <div id="loginMsg" class="msg"></div>
    <div class="hint" style="margin-top:14px">
      忘了口令？在 NAS 上执行下面任一操作即可重置：<br>
      · 关闭访问控制：把 config.json 里的 <span class="mono">auth_required</span> 改成 false<br>
      · 或删除 <span class="mono">auth_password_enc</span> 字段后重启容器（会重新引导设置）
    </div>
  </div>
</section>

<main id="mainBody" class="hide">

<!-- ============ 站点 ============ -->
<section id="tab-sites">
  <div class="card">
    <h2>状态概览</h2>
    <div id="overview" class="mono mut">加载中…</div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="b pri" onclick="runNow()">立即执行到期站点</button>
      <button class="b" onclick="loadSites()">刷新</button>
    </div>
    <div id="runMsg" class="msg"></div>
  </div>
  <div class="card">
    <h2>签到站点</h2>
    <div id="siteRows"><div class="mut">加载中…</div></div>
    <div class="hint">点「编辑」会在该站点下方展开设置表单（开关 / 下拉 / 时间），
    保存后收起。<b>每个站点都需要先配置登录 Cookie</b>，否则签到只会拿到未登录页面。</div>
  </div>
</section>

<!-- ============ 执行记录（日历） ============ -->
<section id="tab-history" class="hide">
  <div class="card">
    <h2>执行记录</h2>
    <div class="calbar">
      <select id="hisSite" onchange="hisPickSite()"></select>
      <button class="b" onclick="hisMonth(-1)">&lsaquo; 上一月</button>
      <span class="mtitle" id="hisTitle">—</span>
      <button class="b" onclick="hisMonth(1)">下一月 &rsaquo;</button>
      <button class="b" onclick="loadHistory()">刷新</button>
    </div>
    <div class="hint" id="hisSummary"></div>
    <div class="calgrid" id="hisGrid"></div>
    <div class="legend">
      <span><i style="background:#e8f8ee;border-color:#b7e3c6"></i>当天全部成功</span>
      <span><i style="background:#fdecea;border-color:#f3c0bb"></i>当天有失败</span>
      <span><i style="background:#fafbfc"></i>没跑过</span>
      <span>点某一天看当天明细</span>
    </div>
    <div id="hisDay" class="daylist"></div>
    <div id="hisReasons"></div>
    <div class="hint">记录从升级到本版本后开始积累（旧版本没存历史，补不回来）。
      只保留最近 <span id="hisKeep">180</span> 天。</div>
  </div>
</section>

<!-- ============ 新增站点 ============ -->
<section id="tab-add" class="hide">
  <div class="card">
    <h2>第一步：填站点信息</h2>
    <div class="row">
      <div><label>站点标识（英文，用于文件名）</label>
        <input id="ns_id" placeholder="mysite"></div>
      <div><label>显示名称</label>
        <input id="ns_name" placeholder="我的站点"></div>
    </div>
    <label>主页地址（录制从这里开始）</label>
    <input id="ns_url" placeholder="https://example.com/">
    <div class="hint">也可以照着现成模板改（到「设置 → 可选站点」点「加入」即可）。
      当前可选：<span id="newSiteTplList" class="mono"></span></div>
  </div>

  <div class="card">
    <h2>第二步：录制我的签到操作</h2>
    <div class="hint">
      点「开始录制」后，脚本会在下面这个浏览器窗口里打开站点并监听你的操作。
      你像平时一样登录并完成一次签到，操作会被记录成步骤，之后每天照此执行。
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="b pri" onclick="startRec()">开始录制</button>
      <button class="b" onclick="stopRec(true)">结束并保存</button>
      <button class="b" onclick="stopRec(false)">放弃</button>
      <button class="b" onclick="refreshRec()">刷新已记录</button>
    </div>
    <div id="recMsg" class="msg"></div>
    <div id="recFrameWrap" class="hide" style="margin-top:12px">
      <div class="hint">浏览器画面（录制中可直接在这里操作）：</div>
      <iframe id="recFrame" style="width:100%;height:520px;border:1px solid var(--bd);
              border-radius:6px;background:#fff"></iframe>
    </div>
    <label>已记录步骤（可直接编辑，保存时以此为准）</label>
    <textarea id="recSteps" spellcheck="false"></textarea>
  </div>
</section>

<!-- ============ 设置 ============ -->
<section id="tab-settings" class="hide">
  <!-- 保存按钮放最上面（用户要求）：设置项很多，原来在最底部，
       每次改一项都要滚到底，很费事。这里放在顶部并吸顶，
       滚动时始终可见；底部也保留一个入口，方便"改完最后一项"顺手保存。 -->
  <div class="formfoot settings-save">
    <button class="b pri" onclick="saveSettings()">保存全部设置</button>
    <span class="grow"></span>
    <span class="hint" style="margin:0">改完随时点这里</span>
  </div>
  <div id="setMsg" class="msg"></div>

  <div class="card">
    <h2>代理设置</h2>
    <label>联网方式</label>
    <div class="radios">
      <label><input type="radio" name="set_pmode" value="direct"
        onchange="swGlobalProxy()">直连（不走代理）</label>
      <label><input type="radio" name="set_pmode" value="custom"
        onchange="swGlobalProxy()">走代理</label>
    </div>
    <div id="set_proxyBox" style="margin-top:10px">
      <label>代理地址</label>
      <input id="set_proxy" placeholder="http://10.0.0.1:7890">
      <div class="hint">填了才生效。留空或选「直连」都表示不走代理。</div>
    </div>
    <div class="hint">个别站点可以单独设置走不走代理 —— 到「站点」页点该站点的
      「编辑」，在「联网方式」里改。</div>
    <div class="fsec">
      <h4>页面自动刷新</h4>
      <label class="sw" for="set_autoref">
        <input type="checkbox" id="set_autoref" onchange="toggleAutoRefresh()">
        <span class="track"></span><span class="lb">定时刷新站点列表</span>
      </label>
      <div class="hint" style="margin-top:6px">
        <b>默认关闭</b>（推荐）：填表单时不会被刷新打断。
        需要时点「刷新」按钮即可。<br>
        打开后在编辑表单期间仍会自动跳过刷新，不会冲掉你填的内容。
        <span id="set_autorefHint"></span>
      </div>
    </div>
    <div class="row">
      <div><label>浏览器无头模式</label>
        <select id="set_headless"><option value="1">开启（推荐）</option>
        <option value="0">关闭（可看画面）</option></select></div>
      <div><label>连接已有 Chrome（CDP，可选）</label>
        <input id="set_cdp" placeholder="http://browser:9222"></div>
    </div>
  </div>

  <div class="card">
    <h2>可选站点同步（从 GitHub 拉模板）</h2>
    <div class="hint">
      把仓库里某个目录下的 <code>.json</code> 站点模板同步进来，**不用重建镜像**。
      同步只是把模板变成「可选」，还要在上面「可选站点」里选中才会真正签到。<br>
      <b>来源已默认填好本项目地址</b>，直接点「立即同步」即可；想用别人的模板库再改成对方地址。
    </div>
    <div class="row">
      <div><label>来源</label>
        <input id="set_sync_src" placeholder="用户名/仓库名/目录，或 https 地址"
               value=""></div>
      <div><label>每天同步时刻</label>
        <input id="set_sync_time" placeholder="03:00" style="max-width:120px"></div>
      <div><label>间隔（天）</label>
        <input id="set_sync_days" type="number" min="1" max="365" value="1"
               style="max-width:110px"></div>
    </div>
    <label class="sw" style="margin-top:8px">
      <input type="checkbox" id="set_sync_on">
      <span class="track"></span><span class="lb">自动同步</span>
    </label>
    <div class="hint" style="margin-top:6px">
      关掉就只剩手动。<b>手动同步是立即执行</b>；自动则到上面那个时刻、
      且间隔天数已到时才拉（打开开关后会先立刻拉一次）。
      只支持 <b>https</b>；来源示例：<code>用户名/仓库名/目录名</code>。
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="b pri" onclick="syncNow()">立即同步</button>
      <button class="b" onclick="loadSyncStatus()">查看状态</button>
    </div>
    <div id="syncMsg" class="msg"></div>
  </div>

  <div class="card">
    <h2>可选站点</h2>
    <div class="hint">
      下面是可以加入的站点模板。**只有点「加入」的才会出现在站点列表里**
      并参与签到；没加入的只是"可选"。
      <span id="plugDir"></span>
    </div>
    <div class="hint" style="margin-top:4px">
      列表里既有<b>镜像自带</b>的模板，也有<b>同步进来</b>的；点「加入」会直接落盘并生效，不需要先同步。
    </div>
    <div id="plugErrors"></div>
    <div id="tplList" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <h2>通知</h2>
    <div class="row">
      <div><label>通知策略</label><select id="set_nmode"></select></div>
    </div>
    <div class="hint">通知默认跟随「设置 → 网络」里的主代理；
      需要单独指定的渠道，可以在下面每个渠道自己那行里选"直连 / 自定义"。</div>
    <div id="chanBox"></div>
    <div class="fsec" style="margin-top:14px">
      <h4>每日汇总</h4>
      <label class="sw" id="set_digestLbl">
        <input type="checkbox" id="set_digest">
        <span class="track"></span><span class="lb">每天推一条汇总（代替每站点各推一条）</span>
      </label>
      <div class="row" style="margin-top:6px">
        <div>
          <label>推送时刻</label>
          <input id="set_digest_time" placeholder="09:00" style="width:110px">
        </div>
      </div>
      <div class="hint">汇总的是<b>前一天</b>的结果：签到大多在凌晨跑完，
        早上推"今天"只会是残缺的。内容为成功/失败次数、失败的站点与原因。
        同一天只会推一条；某天完全没有记录时不推。</div>
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="b" onclick="testNotify()">发送测试通知</button>
      <button class="b" onclick="testAlert()">测试"分析失败"告警</button>
    </div>
    <div id="notifyMsg" class="msg"></div>
  </div>

  <div class="card">
    <h2>DeepSeek（连续失败后自动分析）</h2>
    <label>API Key（留空＝不修改已保存的）</label>
    <input id="set_aikey" type="password" placeholder="sk-…">
    <div class="row">
      <div><label>模型</label>
        <select id="set_model">
          <option value="deepseek-flash">deepseek-flash（快、便宜，推荐）</option>
          <option value="deepseek-v4-pro">deepseek-v4-pro（更强、更贵）</option>
        </select></div>
      <div><label>&nbsp;</label>
        <button class="b" onclick="listAiModels()">测试连接 / 查看可用模型</button></div>
    </div>
    <div class="hint" id="set_modelHint" style="margin-top:6px">
      点右边按钮会真去打一次 DeepSeek 的 <code>/models</code>：既能验证 Key，
      也会列出<b>你账号真实可用</b>的模型名。<br>
      ⚠️ 旧名字（如 <code>deepseek-chat</code>）可能仍能调用，但会被服务端
      <b>静默映射</b>成别的模型 —— 填 chat 实际跑的可能是 flash，价格与能力都不同。
      升级后请用这个按钮确认一次。<br>
      每次分析完成后，通知里也会附上<b>服务端实际返回的模型名</b>，可据此核对。
    </div>
    <div id="aiModelMsg" class="msg"></div>

    <div class="fsec">
      <h4>什么情况下调用 AI 分析</h4>
      <div class="row">
        <div><label>每连续失败几次调用一次 AI（0＝不启用 AI）</label>
          <input id="set_aiafter" type="number" value="3" min="0"
            oninput="calcAiCalls()"></div>
        <div><label>每天最多分析几次（0＝不限制）</label>
          <input id="set_maxai" type="number" value="20" min="0"
            data-needs-sync="1" data-sid=""></div>
      </div>
      <div class="hint" style="margin-top:6px">
        重试次数在「站点 → 编辑 → 失败重试」里设；这里只控制<b>何时调用 AI</b>：
        在连败次数的<b>整数倍</b>处调一次（重试 3 次、这里填 3 → 一轮结束时分析一次）。
        填 <b>0</b> ＝ 不启用 AI。<br>
        「每天最多分析几次」是<b>硬上限</b>，填 <b>0</b> ＝ 不限制。
        <span id="set_aicalc"></span>
      </div>
    </div>

    <div class="fsec">
      <h4>AI 分析本身失败时的处理</h4>
      <div><label>连续几次分析失败后暂停该站点（0＝不限制，永不停用）</label>
        <input id="set_aifail" type="number" value="2" min="0"></div>
      <div class="hint" style="margin-top:6px">
        <b>0</b> = 不限制，分析每次都失败也照样继续、站点不会被停用；<br>
        <b>1</b> = 第一次分析失败就暂停；<b>2</b>（默认）= 连续两次后暂停。<br>
        暂停后不会再消耗 AI 额度；修好后到「站点」页手动「启用」。
      </div>
    </div>

    <div class="fsec">
      <h4>AI 分析时附带页面片段</h4>
      <label style="margin-top:2px">
        <input type="checkbox" id="set_aiexcerpt"> 把失败页面的正文片段一起发给 AI（默认关）
      </label>
      <div class="hint" style="margin-top:6px">
        「判定不了结果」时最有用的线索：让模型看到页面原文，而不是只凭关键词猜。<br>
        发送前自动脱敏（去标签、抹 Cookie/token/邮箱/长数字，截断 4000 字符）。<br>
        <b>开启后页面内容会发给你配置的 DeepSeek 服务</b>；介意就别开。
      </div>
    </div>

    <div class="hint">分析成功后会把建议（新的选择器/关键词/步骤）保存并用于之后的签到。
      若分析本身也连续失败，会停止该站点签到，并推送一条"分析失败"告警。</div>
  </div>

  <div class="card">
    <h2>WebDAV 备份</h2>
    <label>地址</label><input id="set_wdurl" placeholder="https://dav.example.com/dav/">
    <div class="row">
      <div><label>用户名</label><input id="set_wduser"></div>
      <div><label>密码（留空＝不修改）</label><input id="set_wdpw" type="password"></div>
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="b" onclick="testWd()">测试连接</button>
      <button class="b pri" onclick="backupNow()">立即备份</button>
      <button class="b" onclick="restoreNow()">从云端恢复</button>
    </div>
    <div id="wdMsg" class="msg"></div>
  </div>

  <div class="card">
    <h2>访问控制</h2>
    <div class="hint">
      本界面能改代理、取回密钥、删除站点，所以默认需要口令。
      <b>该口令只用于进入本界面，与你的各站点账号无关。</b>
      更多说明见项目里的 <span class="mono">docs/SECURITY.md</span>。
    </div>
    <div id="authState" class="mono mut" style="margin-top:8px"></div>
    <div class="row" style="margin-top:10px">
      <div><label>当前口令</label><input id="set_pwold" type="password"></div>
      <div><label>新口令（至少 6 位）</label><input id="set_pwnew" type="password"></div>
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="b" onclick="changePw()">修改口令</button>
      <button class="b" onclick="disableAuth()">关闭访问控制</button>
      <button class="b" onclick="enableAuth()">开启访问控制</button>
    </div>
    <div class="hint" style="color:#a05a00;margin-top:8px">
      关闭后本界面不再需要口令。建议只在完全可信的网络里这样做。
    </div>
    <div id="authMsg" class="msg"></div>
  </div>

  <div class="card">
    <h2>凭据加密密钥</h2>
    <div class="hint">
      站点密码、DeepSeek Key、WebDAV 密码都是用这个密钥加密后保存的。
      <b>密钥丢了就解不开已保存的密码</b>，所以建议抄一份存好。
    </div>
    <div id="keyInfo" class="mono mut" style="margin-top:8px"></div>
    <label style="margin-top:10px">
      <input type="checkbox" id="set_wdkey"> 备份数据时，把密钥也一并存到 WebDAV
    </label>
    <div class="hint" style="color:#a05a00">
      开启后密钥会存到你的 WebDAV 目录里，请确保那个目录只有你自己能访问。
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="b" onclick="backupKey()">立即备份密钥到 WebDAV</button>
      <button class="b" onclick="restoreKey()">从 WebDAV 取回密钥</button>
    </div>
    <div id="keyMsg" class="msg"></div>
  </div>

  <div style="display:flex;gap:8px">
    <button class="b pri" onclick="saveSettings()">保存全部设置</button>
    <span class="hint" style="margin:0">（顶部也有一个，不用往上翻）</span>
  </div>
</section>

</main>
<script>
var META = {}, SETTINGS = {}, REC = {id: '', active: false};

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function show(el, text, cls){
  var n = $(el); n.className = 'msg ' + (cls||''); n.textContent = text || '';
}
function fmtTime(ts){
  if(!ts) return '—';
  var d = new Date(ts*1000);
  function p(n){ return (n<10?'0':'')+n; }
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+
         p(d.getHours())+':'+p(d.getMinutes());
}
function api(method, path, body){
  return fetch(path, {
    method: method,
    headers: {'Content-Type':'application/json'},
    body: body===undefined?undefined:JSON.stringify(body)
  }).then(function(r){
    return r.json().then(function(j){
      if(!r.ok) throw new Error((j && j.error) || ('HTTP '+r.status));
      return j;
    });
  });
}

/* ---------------- 标签切换 ---------------- */
function switchTab(name){
  document.querySelectorAll('#mainNav button[data-tab]').forEach(function(x){
    x.className = (x.dataset.tab === name) ? 'on' : '';
  });
  ['sites','add','settings','history'].forEach(function(t){
    $('tab-'+t).className = (t === name) ? '' : 'hide';
  });
  if(name === 'settings'){ loadSettings(); loadAutoRefreshSetting(); }
  if(name === 'history'){ loadHistory(); }
}
document.querySelectorAll('#mainNav button[data-tab]').forEach(function(b){
  b.onclick = function(){ switchTab(b.dataset.tab); };
});

/* ==================== 执行记录（日历） ====================
   数据全部由服务端算好再返回：哪天是哪天、这月从周几开始，都不在
   浏览器里算 —— 容器时区与访问设备的时区可能不同，两边各算一遍
   会出现"日历上今天高亮在昨天"这种对不上的问题。 */
var HIS = {month:'', site:'', data:null, day:'', rcode:''};

function pad2(n){ return (n < 10 ? '0' : '') + n; }

function hisTime(ts){
  /* 按**服务端**时区格式化，不用浏览器本地时间。
     容器和浏览器时区不同（容器 UTC、手机 +8 很常见）时，
     昨天 23:30 的记录会显示成"今天 07:30"，和它所在的日历格子对不上。 */
  var p = hisDateParts(ts);
  return pad2(p.hh) + ':' + pad2(p.mi) + ':' + pad2(p.ss);
}

/* 把时间戳换算成"服务端时区的年月日时分秒"。
   做法是先加上服务端偏移、再用 UTC 取值 —— 这样不受浏览器时区影响。 */
function hisDateParts(ts){
  var off = (HIS.data && HIS.data.tz_offset_minutes) || 0;
  var d = new Date((Number(ts) + off * 60) * 1000);
  return { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, dd: d.getUTCDate(),
           hh: d.getUTCHours(), mi: d.getUTCMinutes(), ss: d.getUTCSeconds() };
}

function hisPickSite(){
  HIS.site = $('hisSite').value || '';
  HIS.day = '';
  HIS.rcode = '';
  /* 没选过月份时留空，让服务端按容器时钟给当前月（见 loadHistory 的说明） */
  loadHistory();
}

function hisMonth(delta){
  /* 基准月优先用服务端给的那个，绝不回退到浏览器时钟 */
  var m = (HIS.data && HIS.data.month) || HIS.month;
  if(!m){ loadHistory(); return; }
  var y = parseInt(m.slice(0, 4), 10);
  var mo = parseInt(m.slice(5, 7), 10) - 1 + delta;
  while(mo < 0){ mo += 12; y -= 1; }
  while(mo > 11){ mo -= 12; y += 1; }
  HIS.month = y + '-' + pad2(mo + 1);
  HIS.day = '';
  HIS.rcode = '';
  loadHistory();
}

function loadHistory(){
  /* 月份交给服务端决定。
     浏览器时区和容器可能不一致（容器 UTC、手机 +8 是常见组合），
     前端自己算"当前月"会问到一个还没有数据的月份 —— 用户打开记录页
     看到一片空白，以为从来没跑过。所以 HIS.month 为空时不带参数，
     由服务端按容器时钟给当前月，再用响应里的 month 回填。 */
  var url = '/api/history?id=' + encodeURIComponent(HIS.site || '');
  if(HIS.month){
    url += '&month=' + encodeURIComponent(HIS.month);
  }
  api('GET', url).then(function(d){
    HIS.data = d;
    HIS.month = d.month;
    renderSitePicker(d.sites || []);
    renderCalendar(d);
    $('hisKeep').textContent = d.retention_days || 180;
  }).catch(function(e){
    $('hisGrid').innerHTML = '';
    $('hisSummary').innerHTML = '<span style="color:#c0392b">读取失败：'
      + esc(e.message) + '</span>';
  });
}

/* 站点下拉：只有站点集合变了才重建，否则每次刷新都会把用户的选择冲掉 */
function renderSitePicker(sites){
  var sel = $('hisSite');
  if(!sel) return;
  var want = HIS.site || '';
  var sig = sites.map(function(s){ return s.id; }).join(',');
  if(sel.getAttribute('data-sig') !== sig){
    var html = '<option value="">全部站点</option>';
    sites.forEach(function(s){
      html += '<option value="' + esc(s.id) + '">' + esc(s.name || s.id) + '</option>';
    });
    sel.innerHTML = html;
    sel.setAttribute('data-sig', sig);
  }
  sel.value = want;
  // 行注释而不是 /* */：docs/check_webjs.py 与安全回归测试都用"块注释 +
  // function"的正则从本文件里抽函数，内联块注释会把中间的代码一起吞掉。
  if(sel.value !== want){        // 站点被删了：退回"全部"
    sel.value = '';
    HIS.site = '';
  }
}

function renderCalendar(d){
  $('hisTitle').textContent = d.month;
  var s = d.summary || {}, r = d.recent || {};
  var bits = ['本月成功 ' + (s.ok || 0) + ' 次、失败 ' + (s.fail || 0) + ' 次'
    + '；全绿 ' + (s.green_days || 0) + ' 天，有问题 ' + (s.problem_days || 0) + ' 天'];
  if(r.total){
    bits.push('近 ' + r.days + ' 天成功率 ' + r.rate + '%（' + r.ok + '/' + r.total + '）');
  } else {
    bits.push('近 ' + (r.days || 30) + ' 天还没有记录');
  }
  if(r.top_reason){
    bits.push('最近最常见：' + r.top_reason.label + ' ×' + r.top_reason.count);
  }
  $('hisSummary').textContent = bits.join(' · ');

  var rmeta = d.reason_meta || {};
  var heads = '';
  ['一', '二', '三', '四', '五', '六', '日'].forEach(function(x){
    heads += '<div class="calhead">' + x + '</div>';
  });
  var blanks = '';
  for(var i = 0; i < (d.first_weekday || 0); i++){
    blanks += '<div class="calcell empty"></div>';
  }
  var cells = (d.days || []).map(function(x){
    var cls = 'calcell ' + x.status;
    if(x.future) cls += ' future';
    if(x.is_today) cls += ' today';
    if(HIS.day === x.date) cls += ' sel';
    var meta = '';
    if(x.total){
      meta = (x.ok ? '成功 ' + x.ok : '')
           + (x.ok && x.fail ? ' / ' : '')
           + (x.fail ? '失败 ' + x.fail : '');
    }
    // 悬停就看到"这天为什么红"，不用点开
    var tip = x.date + '：';
    if(!x.total){
      tip += '没有执行记录';
    } else {
      tip += '成功 ' + x.ok + ' 次、失败 ' + x.fail + ' 次';
      var rm = rmeta[x.top_reason];
      if(rm) tip += '；主要原因：' + rm.label;
    }
    return '<div class="' + cls + '" data-day="' + esc(x.date) + '" title="'
      + esc(tip) + '">'
      + '<div class="dnum">' + x.day + '</div>'
      + '<div class="cmeta">' + esc(meta) + '</div></div>';
  }).join('');
  $('hisGrid').innerHTML = heads + blanks + cells;
  renderDay(d);
  renderReasons(d);
}

function renderDay(d){
  var box = $('hisDay');
  if(!d || !HIS.day){
    box.innerHTML = '';
    return;
  }
  var day = null;
  (d.days || []).forEach(function(x){ if(x.date === HIS.day) day = x; });
  if(!day){
    box.innerHTML = '';
    return;
  }
  if(!day.total){
    box.innerHTML = '<div class="hint">' + esc(day.date) + ' 这天没有执行记录。</div>';
    return;
  }
  var html = '<div class="dhead">' + esc(day.date) + '　共 ' + day.total
    + ' 次（成功 ' + day.ok + '，失败 ' + day.fail + '）</div>';
  var rmeta = d.reason_meta || {};
  (day.items || []).forEach(function(it){
    var bits = [];
    if(!HIS.site && it.site_name) bits.push('<b>' + esc(it.site_name) + '</b>');
    // 失败先说"是哪一类"再说站点原话：原话常常只有"未登录"三个字，
    // 归好类才能一眼看出该去改 Cookie、该看网络、还是该重录。
    if(!it.ok) bits.push(hisChip(it.reason, rmeta));
    bits.push(it.msg ? esc(it.msg) : (it.ok ? '成功' : '（没有说明）'));
    var tail = [];
    if(it.trigger === 'manual') tail.push('手动执行');
    if(it.attempt > 1) tail.push('第 ' + it.attempt + ' 次尝试');
    if(it.duration_ms) tail.push((it.duration_ms / 1000).toFixed(1) + 's');
    if(it.error_kind) tail.push(it.error_kind);
    if(tail.length) bits.push('<span style="color:#7a828c">'
                              + esc(tail.join(' · ')) + '</span>');
    html += '<div class="it">'
      + '<span class="tm">' + esc(hisTime(it.ts)) + '</span>'
      + '<span class="st">' + (it.ok ? '<span class="tag ok">成功</span>'
                                     : '<span class="tag err">失败</span>') + '</span>'
      + '<span class="bd">' + bits.join(' · ') + '</span>'
      + '</div>';
    /* 步骤链默认收起：看"成没成"不需要它，排查时才展开。
       用原生 details，不占 JS 状态，翻月份重渲染也不会丢。 */
    if(it.steps && it.steps.length){
      html += '<details class="itstep"><summary>执行步骤（'
        + it.steps.length + ' 步）</summary><ol>'
        + it.steps.map(function(x){ return '<li>' + esc(x) + '</li>'; }).join('')
        + '</ol></details>';
    }
  });
  box.innerHTML = html;
}

function hisChip(code, rmeta){
  var m = (rmeta || {})[code] || {};
  var col = m.color || '#7a828c';
  return '<span class="rchip" style="color:' + col + ';background:' + col
    + '14" title="' + esc(m.hint || '') + '">'
    + esc(m.label || code || '失败') + '</span>';
}

function hisDate(ts){
  var p = hisDateParts(ts);
  return pad2(p.mo) + '-' + pad2(p.dd) + ' ' + pad2(p.hh) + ':' + pad2(p.mi);
}

/* 失败原因分布：先按大类（登录/拦截/网络/站点/规则）汇总量级，
   再在组内列出具体原因。只按 code 平铺会有十几行、每行 1 次，
   看不出"这个月到底是哪类问题在拖后腿"。 */
function renderReasons(d){
  var box = $('hisReasons');
  if(!box) return;
  var list = (d && d.reasons) || [];
  var meta = (d && d.reason_meta) || {};
  if(!list.length){
    box.innerHTML = '';
    return;
  }
  var total = 0;
  list.forEach(function(x){ total += x.count; });

  var groups = {}, order = [];
  list.forEach(function(x){
    var m = meta[x.code] || {};
    var cat = m.category || 'other';
    if(!groups[cat]){
      groups[cat] = {label: m.category_label || '其它',
                     color: m.color || '#7a828c', count: 0, items: []};
      order.push(cat);
    }
    groups[cat].count += x.count;
    groups[cat].items.push({code: x.code, count: x.count, last_ts: x.last_ts,
                            sites: x.sites || [], last_msg: x.last_msg || '',
                            meta: m});
  });
  order.sort(function(a, b){ return groups[b].count - groups[a].count; });

  var html = '<div class="rhead">失败原因分布 · 本月共 ' + total
    + ' 次失败（点一条看它最近几次的原文）</div>';
  order.forEach(function(cat){
    var g = groups[cat];
    var gpct = total ? Math.round(g.count * 100 / total) : 0;
    html += '<div class="rgroup">'
      + '<div class="rg"><span class="rdot" style="background:' + g.color
      + '"></span>' + esc(g.label)
      + '<span class="rcnt">' + g.count + ' 次 · ' + gpct + '%</span></div>'
      + '<div class="rbar"><i style="width:' + Math.max(gpct, 2)
      + '%;background:' + g.color + '"></i></div>';
    g.items.forEach(function(x){
      var m = x.meta || {};
      var on = (HIS.rcode === x.code);
      var pct = total ? Math.round(x.count * 100 / total) : 0;
      // 已按站点筛选时站点名是冗余的，换成最近一次的原文更有用
      var sub = HIS.site ? (x.last_msg || '') : (x.sites || []).join('、');
      html += '<div class="rrow' + (on ? ' on' : '') + '" data-rcode="'
        + esc(x.code) + '" title="' + esc(m.hint || '') + '">'
        + '<span class="rname" style="color:' + (m.color || g.color) + '">'
        + esc(m.label || x.code) + '</span>'
        + '<span class="rsite">' + esc(sub) + '</span>'
        + '<span class="rcnt">' + x.count + ' 次 · ' + pct + '%</span>'
        + '<span class="rlast">' + esc(x.last_ts ? hisDate(x.last_ts) : '')
        + '</span></div>';
      if(on) html += hisReasonRecent(d, x.code);
    });
    html += '</div>';
  });
  box.innerHTML = html;
}

/* 某原因在本月的最近几次（数据就来自本月明细，不额外请求接口） */
function hisReasonRecent(d, code){
  var rows = [];
  (d.days || []).forEach(function(day){
    (day.items || []).forEach(function(it){
      if(!it.ok && it.reason === code) rows.push(it);
    });
  });
  rows.sort(function(a, b){ return b.ts - a.ts; });
  rows = rows.slice(0, 6);
  if(!rows.length){
    return '<div class="revlist"><div class="hint">本月没有更详细的记录。</div></div>';
  }
  var html = '<div class="revlist">';
  rows.forEach(function(it){
    var bits = [];
    if(!HIS.site && it.site_name) bits.push('<b>' + esc(it.site_name) + '</b>');
    bits.push(esc(it.msg || '（没有说明）'));
    html += '<div class="it"><span class="rt">' + esc(hisDate(it.ts)) + '</span>'
      + '<span class="bd">' + bits.join(' · ') + '</span></div>';
  });
  return html + '</div>';
}

document.addEventListener('click', function(e){
  var t = e.target;
  if(!t || !t.closest || !HIS.data) return;
  var row = t.closest('[data-rcode]');
  if(row){
    var code = row.getAttribute('data-rcode') || '';
    HIS.rcode = (HIS.rcode === code) ? '' : code;   // 再点一次收起
    renderReasons(HIS.data);
    return;
  }
  var cell = t.closest('[data-day]');
  if(!cell) return;
  var day = cell.getAttribute('data-day') || '';
  HIS.day = (HIS.day === day) ? '' : day;      // 再点一次收起
  renderCalendar(HIS.data);
});

/* 从站点卡片点「记录」进来：锁定该站点 + 回到本月。
   "本月"不在这里算 —— 交给服务端，避免浏览器与容器时区不一致时落到空月份。 */
function openSiteHistory(sid){
  HIS.site = sid || '';
  HIS.month = '';
  HIS.day = '';
  HIS.rcode = '';
  switchTab('history');
}

/* ---------------- 登录 ---------------- */
var AUTH = {required: true, configured: false, needs_setup: false};

function showLogin(state){
  $('tab-login').className = '';
  $('mainBody').className = 'hide';
  $('mainNav').className = 'hide';
  AUTH = state || {};
  if(state.needs_setup){
    $('loginTitle').textContent = '首次使用：设置访问口令';
    $('loginHint').textContent =
      '本界面能修改代理、查看/恢复密钥、删除站点，因此需要口令保护。'
      + '请设置一个只用于本界面的口令（与各站点账号无关）。';
    $('loginPw2Wrap').className = '';
    $('loginPw2').className = '';
    $('loginBtn').textContent = '设置并进入';
  } else {
    $('loginTitle').textContent = '需要登录';
    $('loginPw2Wrap').className = 'hide';
    $('loginPw2').className = 'hide';
    $('loginBtn').textContent = '登录';
  }
  $('loginPw').value = ''; $('loginPw2').value = '';
  $('loginPw').focus();
}

function showMain(){
  $('tab-login').className = 'hide';
  $('mainBody').className = '';
  $('mainNav').className = '';
  loadMeta();
  refreshAll();
  /* 挂上对 QD「Cookies获取助手」扩展的监听（只挂一次） */
  listenQdExtension();
  /* 恢复用户上次选择的自动刷新设置（默认关闭） */
  applyAutoRefresh();
}

function doLogin(){
  var pw = $('loginPw').value;
  if(!pw){ show('loginMsg','请输入口令','err'); return; }
  if(AUTH.needs_setup){
    if(pw.length < 6){ show('loginMsg','口令至少 6 位','err'); return; }
    if(pw !== $('loginPw2').value){ show('loginMsg','两次输入不一致','err'); return; }
    api('POST','/api/auth/setup',{password:pw}).then(function(){
      show('loginMsg','设置成功，正在进入…','ok');
      setTimeout(boot, 300);
    }).catch(function(e){ show('loginMsg', e.message, 'err'); });
    return;
  }
  api('POST','/api/auth/login',{password:pw}).then(function(){
    show('loginMsg','登录成功，正在进入…','ok');
    setTimeout(boot, 300);
  }).catch(function(e){ show('loginMsg', e.message, 'err'); });
}

function doLogout(){
  api('POST','/api/auth/logout',{}).then(function(){
    location.reload();
  }).catch(function(){ location.reload(); });
}
document.getElementById('btnLogout').onclick = doLogout;

function boot(){
  // 口令是否生效由服务端说了算：先查状态，被拒就显示登录页
  api('GET','/api/auth/state').then(function(st){
    if(!st.required){ showMain(); return; }
    if(st.needs_setup){ showLogin(st); return; }
    // 已设口令：试探一个受保护接口，401 就说明没登录
    return api('GET','/api/sites').then(function(){ showMain(); })
      .catch(function(e){
        if(/未登录|失效|口令/.test(e.message)) showLogin(st);
        else showMain();
      });
  }).catch(function(){ showMain(); });
}

/* ---------------- 站点列表 ---------------- */
function loadOverview(){
  api('GET','/api/overview').then(function(d){
    $('ver').textContent = 'v' + (d.version||'');
    var sch = d.scheduler || {};
    var lines = [
      '站点 '+d.site_count+' 个（启用 '+d.enabled_count+'）',
      '代理：' + (d.proxy_configured?'已配置':'未配置'),
      '通知：' + (d.notify_channels.length? d.notify_channels.join('、') : '未配置'),
      'WebDAV：' + (d.webdav_configured?'已配置':'未配置'),
      'DeepSeek：' + (d.ai_configured?'已配置':'未配置'),
      '上次执行：' + fmtTime(d.last_run_at),
      '调度循环：' + (sch.running?('运行中，每 '+sch.interval_seconds+' 秒检查'):'未启动')
        + (sch.last_error? ('，最近错误：'+sch.last_error):'')
    ];
    $('overview').textContent = lines.join('\n');
  }).catch(function(e){ $('overview').textContent = '读取失败：'+e.message; });
}

/* ==================== 站点列表（卡片式 + 内联编辑表单） ==================== */

/* 当前展开编辑的站点 id；空 = 全部收起 */
var OPEN_SITE = '';
/* 缓存的站点详情（含 headers 与 cookie 元信息） */
var SITE_CACHE = {};

/* 两种调度模式的说明文案。
   单独抽成函数而不是写成行内三元：行内三元和字符串拼接混在一起时
   JS 解析容易出问题（这里踩过一次 "Unexpected token '?'"）。 */
function modeHintHtml(isDaily, s){
  if(isDaily){
    return '每天在下面设定的时刻签到一次；今天该时刻已过就顺延到明天。';
  }
  var locked = s && !(isNaN(parseInt(s.success_anchor_minutes,10))
                      || parseInt(s.success_anchor_minutes,10) < 0);
  var hd = '每隔下面设定的间隔签到一次，基准是<b>上一次签到成功的时间</b>'
    + '（不是固定钟点）。<br>例：今天 03:21 成功、间隔 24 小时 '
    + '&rarr; 下次明天 03:21 左右。<br>';
  if(locked){
    return hd + '这个站点的初始基准已锁定（见下面那个时刻）。'
      + '第一次签到成功后，基准自动换成真实的成功时间。';
  }
  return hd + '还没设置过：先在上面填好时刻，再选这个模式 —— '
    + '那一刻便会<b>锁定为初始基准</b>，之后不能修改。'
    + '首次成功后就自动以成功时间为基准了。';
}

/* 基准是否已锁定 */
function anchorLocked(s){
  var a = parseInt(s && s.success_anchor_minutes, 10);
  return !(isNaN(a) || a < 0);
}
/* 时刻输入框是否该禁用。
   ⚠️ 不能只看"是不是 daily 模式"：那样一来"选了成功模式但还没锁定"
   也会被禁用，用户就永远没机会把默认的 03:00 改成自己要的 09:35
   （实测发现的缺陷）。正确规则：
     * daily 模式 -> 可编辑（这是"每天固定时刻"的配置）
     * 成功模式 + 未锁定 -> 可编辑（让用户先填，再选模式锁定）
     * 成功模式 + 已锁定 -> 禁用（用户要求"锁定后人工不能再改"）
   isDaily 必须由调用方显式传入：初次渲染时用表单数据里的模式，
   切换时用实时 radio 状态 —— 不能在这里查 DOM（渲染时查不到）。 */
function timeLocked(s, isDaily){
  if(isDaily) return false;
  return anchorLocked(s);
}

/* 基准时刻的取值：优先用已锁定的基准；没锁定过就用当前 daily 时刻。
   用户语义："设置这个模式的那一刻，把那一刻填的时刻锁定为初始基准。"
   所以没设置过时，界面上显示的就是即将被锁定的那个值。 */
function anchorMinutes(s){
  var a = parseInt(s && s.success_anchor_minutes, 10);
  if(isNaN(a) || a < 0){
    // 没设置过 -> 用 daily 时刻作为"即将锁定"的值
    return ((parseInt(s.daily_hour||3,10)%24)*60) + (parseInt(s.daily_minute||0,10)%60);
  }
  return a % 1440;
}
function anchorHour(s){ return Math.floor(anchorMinutes(s)/60); }
function anchorMinute(s){ return anchorMinutes(s)%60; }

/* 时刻输入框下面那行说明 */
function timeHintHtml(s){
  var a = parseInt(s && s.success_anchor_minutes, 10);
  var locked = !(isNaN(a) || a < 0);
  if(locked){
    var hh = String(Math.floor(a/60)).padStart(2,'0');
    var mm = String(a%60).padStart(2,'0');
    return '已锁定为 <b>'+hh+':'+mm+'</b>：这是首次签到成功前的基准时刻，'
      + '不可再修改。<br>第一次签到成功后会<b>自动换成真实的成功时间</b>'
      + '（例如 09:41 成功，之后就以 09:41 为基准）。';
  }
  return '填好时刻后选「以【上次签到成功时间】为基准」，'
    + '这个时刻会被<b>锁定为初始基准</b>，之后不能再改。';
}

/* 模式切换时处理"锁定初始基准"。
   从"每天固定时刻"切到"以成功时间为基准"时，把当时填的时刻定为基准。
   已经锁定过的不会因为再次点击而被改写（用户要求"设置的那一刻直接锁定"）。 */
function onModeChange(id){
  var p = 'f_'+id+'_';
  var daily = document.querySelector('input[name="'+p+'mode"][value="daily"]');
  var isDaily = !!(daily && daily.checked);
  var hidden = $(p+'anchor');
  if(!isDaily && hidden){
    var already = hidden.getAttribute
      && hidden.getAttribute('data-locked') === '1';
    if(!already){
      // 首次切到这个模式 -> 锁定当前输入框里的时刻
      var h = parseInt(($(p+'hour')||{}).value || '3', 10);
      var m = parseInt(($(p+'min')||{}).value || '0', 10);
      if(isNaN(h)) h = 3;
      if(isNaN(m)) m = 0;
      var locked = (Math.max(0,Math.min(23,h))*60) + Math.max(0,Math.min(59,m));
      hidden.value = locked;
      if(hidden.setAttribute) hidden.setAttribute('data-locked', '1');
      $(p+'hour').value = String(Math.floor(locked/60)).padStart(2,'0');
      $(p+'min').value = String(locked%60).padStart(2,'0');
      var hint = $(p+'timeHint');
      if(hint) hint.innerHTML = timeHintHtml({success_anchor_minutes: locked});
    }
  }
  swSync(id);
}

/* 把分钟数说成人话：1440 -> "24 小时" */
function intervalText(minutes){
  var v = parseInt(minutes || 0, 10);
  if(!v || v <= 0) return '24 小时';
  if(v % 60 === 0) return (v/60) + ' 小时';
  if(v < 60) return v + ' 分钟';
  return Math.floor(v/60) + ' 小时 ' + (v%60) + ' 分钟';
}

/* 今天的日期（与后端 _today 的格式一致：YYYY-MM-DD）。
   用于判断"今天已停止重试"这类带日期的标记是否仍然有效。 */
function todayStr(){
  var d = new Date();
  var m = String(d.getMonth() + 1).padStart(2, '0');
  var day = String(d.getDate()).padStart(2, '0');
  return d.getFullYear() + '-' + m + '-' + day;
}

function cookieTag(s){
  if(!s.has_cookie) return '<span class="tag err">未配置登录</span>';
  if(s.cookie_status===2) return '<span class="tag err">登录已失效</span>';
  if(s.cookie_status===1) return '<span class="tag ok">登录有效</span>';
  /* 检查过却判不出来（status=0 且有 checked_at）：别一直显示"已配置"，
     否则用户以为检查没生效。 */
  if(s.cookie_checked_at) return '<span class="tag">无法判断</span>';
  return '<span class="tag ok">已配置登录</span>';
}

/* 站点列表的结构快照。
   为什么需要它：loadSites 原来每次都 `innerHTML = ...` 把整个列表
   （连同打开的编辑表单）销毁重建 —— 即使数据没变也一样。
   后果是页面闪烁、输入焦点丢失、已打开的 select 被关掉、滚动位置重置。
   用户反馈的"动不动就刷新、填不了"就是这个原因。
   现在只有结构真的变了才重建；只是"下次运行时间"这类变动就地更新。 */
var _sitesSig = '';
/* 用户主动操作（切换编辑、恢复备份等）后要强制重建一次。
   GET /api/sites 里的 next_run_at 每次都变，所以不能拿它算签名。 */
var _forceSitesRefresh = false;

/* 这些字段变了不值得重建整个列表（地图/表单会被销毁重建）：
   下次运行时间每分钟都在变；登录检查结果有 cookieTag() 就地刷新。
   cookie_status/cookie_error 一度不在这个名单里 —— 结果就是"点检查有效期，
   消息刚写进提示区就被整行重建冲掉了"，表现成"点了没反应"（已修）。 */
var VOLATILE_FIELDS = ['next_run_at', 'last_success_at', 'cookie_checked_at',
                       'cookie_status', 'cookie_error'];

function sitesSignature(sites){
  var stable = (sites||[]).map(function(s){
    var c = {};
    for(var k in s){
      if(Object.prototype.hasOwnProperty.call(s,k) && VOLATILE_FIELDS.indexOf(k)<0){
        c[k] = s[k];
      }
    }
    return c;
  });
  return JSON.stringify(stable);
}

function loadSites(force){
  if(force) _forceSitesRefresh = true;
  api('GET','/api/sites').then(function(d){
    var sites = d.sites || [];
    var sig = sitesSignature(sites);
    var same = (sig === _sitesSig) && !_forceSitesRefresh;

    if(same){
      /* 结构没变：只就地更新"下次运行"和登录状态标签，
         绝不碰 DOM 结构 —— 正在编辑的表单、焦点、滚动位置全都保住。 */
      sites.forEach(function(s){
        var el = $('site-'+s.id+'_next');
        if(el) el.textContent = '下次 ' + fmtTime(s.next_run_at);
      });
      updateCookieTags(sites);
      return;
    }
    _sitesSig = sig;
    _forceSitesRefresh = false;
    renderSiteRows(sites);
  }).catch(function(e){
    $('siteRows').innerHTML = '<div class="err">'+esc(e.message)+'</div>';
  });
}

/* 就地刷新卡片上的登录状态标签（不重建 DOM） */
function updateCookieTags(sites){
  sites.forEach(function(s){
    var el = $('site-'+s.id+'_ck');
    if(el) el.innerHTML = cookieTag(s);
  });
}

function renderSiteRows(sites){
  var cards = sites.map(function(s){
    var open = (OPEN_SITE === s.id);
    var state = s.enabled? '<span class="tag ok">启用</span>'
                         : '<span class="tag">停用</span>';
    var sch = s.mode==='success_based'
      ? ('签到成功后 ' + intervalText(s.success_interval_minutes))
      : ('每天 ' + String(s.daily_hour).padStart(2,'0') + ':' +
         String(s.daily_minute).padStart(2,'0'));
    if(s.jitter_enabled) sch += '（±随机）';
    var badges = '';
    if(s.need_browser) badges += ' <span class="tag">浏览器</span>';
    if(!s.verify_ssl) badges += ' <span class="tag">放宽证书</span>';
    if(s.kind==='custom') badges += ' <span class="tag">自建</span>';
    if(s.step_count) badges += ' <span class="tag">'+s.step_count+' 步</span>';
    if(s.headers && Object.keys(s.headers).length)
      badges += ' <span class="tag">自定义头 '+Object.keys(s.headers).length+'</span>';
    var pm = s.proxy_mode || 'inherit';
    if(pm === 'direct') badges += ' <span class="tag">直连</span>';
    else if(pm === 'custom') badges += ' <span class="tag">独立代理</span>';

    var fail = '';
    if(s.consecutive_failures)
      fail += ' <span class="tag err">连败 '+s.consecutive_failures+'</span>';
    /* 疑似模板过期：连续失败且失败原因基本是"判定不了/步骤失败"这类规则问题，
       而不是网络或登录。此时让用户一眼看到该去查模板，而不是干等重试。 */
    if(s.template_stale)
      fail += '<div class="hint" style="color:#c0392b">⚠️ 疑似站点改版：'
            + '最近多次失败都是"判定不了结果"。建议检查模板关键词，'
            + '或点「编辑」后手动触发 AI 分析。</div>';
    if(s.disabled_reason)
      fail += '<div class="hint" style="color:#c0392b">'+esc(s.disabled_reason)+'</div>';
    if(s.ai_consecutive_failures)
      fail += '<div class="hint">AI 分析失败 '+s.ai_consecutive_failures+' 次'
            + (s.last_ai_error? ('：'+esc(s.last_ai_error)) : '') + '</div>';

    return '<div class="site'+(open?' open':'')+'" id="site-'+esc(s.id)+'">'
      + '<div class="hd"><div>'
      +   '<div class="nm">'+esc(s.name)+' <span id="site-'+esc(s.id)+'_ck">'
      +     cookieTag(s)+'</span></div>'
      +   '<div class="meta">'+esc(s.id)+badges+'</div>'
      +   '<div class="meta">'+state+' · '+esc(sch)
      +     ' · <span id="site-'+esc(s.id)+'_next">下次 '+esc(fmtTime(s.next_run_at))
      +     '</span>'+fail+'</div>'
      + '</div><div class="acts">'
      +   '<button class="b" data-act="run" data-id="'+esc(s.id)+'">立即</button>'
      +   '<button class="b" data-act="history" data-id="'+esc(s.id)+'">记录</button>'
      +   '<button class="b" data-act="toggle" data-id="'+esc(s.id)+'"'
      +     ' data-on="'+(s.enabled?0:1)+'">'+(s.enabled?'停用':'启用')+'</button>'
      +   '<button class="b'+(open?' pri':'')+'" data-act="edit" data-id="'
      +     esc(s.id)+'">'+(open?'收起':'编辑')+'</button>'
      +   '<button class="b danger" data-act="del" data-id="'+esc(s.id)+'">删除</button>'
      + '</div></div>'
      + '<div id="form-'+esc(s.id)+'">'+(open? '<div class="hint">加载表单…</div>':'')
      + '</div></div>';
  }).join('');
  $('siteRows').innerHTML = cards || '<div class="mut">还没有站点</div>';
  if(OPEN_SITE) renderSiteForm(OPEN_SITE);
}

/* 把站点地址渲染成链接，但**只允许 http/https**。

   为什么必须校验协议：esc() 只做 HTML 实体转义，不做协议白名单。
   站点 homepage 可以被填成（或从录制里带进）`javascript:...`，
   那样管理员点一下这个链接就会在**本页面同源**下执行脚本 ——
   可直接调用全部管理接口（改 WebDev 地址、上传加密密钥）。
   纯文本或非 http(s) 时退化为不可点的文本，避免任何协议执行。 */
function safeLink(url){
  var u = String(url == null ? '' : url).trim();
  if(!/^https?:\/\//i.test(u)){
    return '<b>' + esc(u || '该网站') + '</b>';
  }
  return '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">'
    + esc(u) + '</a>';
}

/* 把已保存的模型名填进下拉框。

   如果它不在候选里（比如旧配置里的 deepseek-chat），就临时插一个选项并选中 ——
   既不让用户看不出自己配的是什么，也不擅自把设置改成别的模型。 */
function setModelSelect(current){
  var sel = $('set_model');
  if(!sel) return;
  var want = (current || '').trim();
  if(!want) return;
  var found = false;
  for(var i = 0; i < sel.options.length; i++){
    if(sel.options[i].value === want){ found = true; break; }
  }
  if(!found){
    var o = document.createElement('option');
    o.value = want;
    o.textContent = want + '（当前已保存，可能已被服务端映射）';
    sel.appendChild(o);
  }
  sel.value = want;
}

/* 问后端列出可用模型（顺带验证 Key）。 */
function listAiModels(){
  show('aiModelMsg', '正在查询…', '');
  api('POST','/api/ai/models',{api_key: $('set_aikey').value}).then(function(d){
    if(!d.ok){
      show('aiModelMsg', '查询失败：' + (d.error || '未知原因'), 'err');
      return;
    }
    var lines = ['Key 有效。你账号可用的模型：'];
    (d.models || []).forEach(function(m){
      lines.push('  · ' + m + (m === d.default ? '（默认）' : ''));
    });
    if((d.models || []).length === 0){
      lines.push('  （接口没返回任何模型名，请到 DeepSeek 控制台确认账号状态）');
    }
    var cur = ($('set_model') || {}).value || '';
    lines.push('');
    lines.push('当前选择：' + (cur || '(未选择)'));
    if(d.models && d.models.length && d.models.indexOf(cur) < 0){
      lines.push('⚠️ 当前选择不在可用清单里。它可能仍能调用（被服务端映射），');
      lines.push('   但建议从上面清单里选一个明确的模型名。');
    }
    show('aiModelMsg', lines.join('\n'), 'ok');
  }).catch(function(e){ show('aiModelMsg', e.message, 'err'); });
}

/* 卡片按钮的点击处理：用事件委托 + data 属性，而不是把 id 拼进
   onclick 字符串。

   为什么必须这样：onclick="runSite('ID')" 里 ID 处于 **JS 字符串上下文**，
   HTML 实体转义（esc 把 ' 变成 &#39;）挡不住 —— 浏览器会先把实体解码回 '
   再交给 JS 解析器，于是 id 里的引号能闭合字符串并注入代码。
   实测 id = `x'),alert(1),('y` 点一下就执行。
   data-id="..." 是纯 HTML 属性上下文，esc() 在那里是完备的。 */
document.addEventListener('click', function(e){
  var btn = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
  if(!btn) return;
  var act = btn.getAttribute('data-act');
  var sid = btn.getAttribute('data-id') || '';
  if(act === 'run') runSite(sid);
  else if(act === 'history') openSiteHistory(sid);
  else if(act === 'toggle') toggleSite(sid, btn.getAttribute('data-on') === '1');
  else if(act === 'edit') toggleEdit(sid);
  else if(act === 'del') delSite(sid);
});

/* 站点表单里各按钮的点击处理（同上：不把 id 拼进 onclick） */
document.addEventListener('click', function(e){
  var btn = e.target && e.target.closest ? e.target.closest('[data-fact]') : null;
  if(!btn) return;
  var fact = btn.getAttribute('data-fact');
  var sid = btn.getAttribute('data-sid') || '';
  if(fact === 'cookieHelper') openCookieHelper(sid);
  else if(fact === 'saveCookie') saveCookie(sid);
  else if(fact === 'checkCookie') checkCookie(sid);
  else if(fact === 'clearCookie') clearCookie(sid);
  else if(fact === 'addHeader') addHeaderRow(sid);
  else if(fact === 'saveSite') saveSite(sid);
  else if(fact === 'cancel') toggleEdit(sid);
  else if(fact === 'markCookieImport') markCookieImportStart(sid);
});

/* 展开 / 收起某个站点的编辑表单（其他站点自动下推） */
function toggleEdit(id){
  var willOpen = (OPEN_SITE !== id);
  OPEN_SITE = willOpen ? id : '';
  /* 用户主动操作：强制重建一次，让展开状态立刻生效 */
  _forceSitesRefresh = true;
  _formDirty = false;
  _pendingSitesRefresh = false;
  loadSites();
  if(willOpen){
    /* 展开后把这张卡片顶到视口上方，长表单不至于跑到屏幕外 */
    setTimeout(function(){
      var el = $('site-'+id);
      if(el && el.scrollIntoView){
        el.scrollIntoView({block:'start', behavior:'smooth'});
      }
    }, 90);
  }
}

/* 单选组的小工具：按名字取/设值。
   表单里多处用到（联网方式、签到时间），统一在此实现。 */
function radioValue(name){
  var el = document.querySelector('input[name="'+name+'"]:checked');
  return el ? el.value : '';
}
function setRadio(name, value){
  var list = document.querySelectorAll('input[name="'+name+'"]');
  for(var i = 0; i < list.length; i++){
    list[i].checked = (list[i].value === value);
  }
}
/* 代理地址框只在选「走代理」时可用，避免"选了直连却还留着旧地址"的困惑 */
function swGlobalProxy(){
  var mode = radioValue('set_pmode');
  var box = $('set_proxyBox');
  var inp = $('set_proxy');
  var on = (mode === 'custom');
  if(box) box.style.display = on ? '' : 'none';
  if(inp) inp.disabled = !on;
}
/* 估算"这个站点每天会被调用几次 AI"，让用户不用自己算。

   一轮签到的最多尝试次数 = 重试次数 + 1（首次 + 重试）。
   倍数规则下，调用次数 = floor(最大尝试次数 / N)，
   再受"每天最多分析几次"封顶。
   注意这只是估算：站点若签到成功就不会再调用 AI。 */
function calcAiCalls(){
  var el = $('set_aicalc');
  if(!el) return;
  var after = parseInt(($('set_aiafter')||{}).value || '0', 10);
  var cap = parseInt(($('set_maxai')||{}).value || '0', 10);
  if(isNaN(after)) after = 0;
  if(isNaN(cap)) cap = 0;

  // 取所有站点里最大的"重试次数"，给出一个上界提示
  var maxRetry = 0;
  (SITE_CACHE && Object.keys(SITE_CACHE) || []).forEach(function(k){
    var s = SITE_CACHE[k] || {};
    var rc = parseInt(s.retry_count, 10);
    if(!isNaN(rc) && rc > maxRetry) maxRetry = rc;
  });
  if(after <= 0){
    el.innerHTML = '<br><b>当前：不启用 AI 分析。</b>';
    return;
  }
  if(!maxRetry){
    el.innerHTML = '<br>展开某个站点后可以看到按它的重试次数算出的调用次数。';
    return;
  }
  var attempts = maxRetry + 1;                 // 首次 + 重试
  var calls = Math.floor(attempts / after);
  if(cap > 0 && calls > cap) calls = cap;
  el.innerHTML = '<br>以重试 ' + maxRetry + ' 次为例：一轮最多尝试 ' + attempts
    + ' 次 → 每天最多调用 AI <b>' + calls + '</b> 次'
    + (calls === 0 ? '（因为这个倍数在一轮里到不了，若失败会跨轮累计）' : '')
    + '。';
}

/* 站点表单里的同款逻辑 */
function swSiteProxy(id){
  var p = 'f_'+id+'_';
  var mode = radioValue(p+'pmode');
  var box = $(p+'proxyBox');
  var on = (mode === 'custom');
  if(box) box.style.display = on ? '' : 'none';
}

/* 设置页的"定时刷新"开关。存在浏览器本地，不进后端配置。 */
function toggleAutoRefresh(){
  var el = $('set_autoref');
  var on = !!(el && el.checked);
  setAutoRefresh(on ? 60000 : 0);
  refreshAutoRefHint();
}

function loadAutoRefreshSetting(){
  var el = $('set_autoref');
  if(el) el.checked = autoRefreshMs() > 0;
  refreshAutoRefHint();
}

function refreshAutoRefHint(){
  var el = $('set_autorefHint');
  if(!el) return;
  var ms = autoRefreshMs();
  el.textContent = ms > 0
    ? ('（当前：每 ' + Math.round(ms / 1000) + ' 秒刷新一次）')
    : '（当前：不自动刷新）';
}

function _sw(id, label, on, sid){
  /* 第 4 个参数 sid：站点 id（全局开关传空字符串）。
     不再把 id 拼进内联 onchange —— 内联事件属性里的 JS 字符串上下文
     用 HTML 实体转义挡不住引号逃逸（浏览器会先解码再交给 JS 解析）。
     改为打 data-* 标记，由下面的委托监听统一处理。 */
  return '<label class="sw'+(on?'':' off')+'" for="'+id+'">'
    + '<input type="checkbox" id="'+id+'"'+(on?' checked':'')
    + ' data-needs-sync="1" data-sid="'+esc(sid == null ? '' : sid)+'">'
    + '<span class="track"></span><span class="lb">'+esc(label)+'</span></label>';
}

/* 需要"值一变就重算显隐"的控件统一由这里处理（事件委托，捕获阶段）。
   用 data-needs-sync 标记，彻底避免 data → 内联事件的拼接。 */
document.addEventListener('change', function(e){
  var el = e.target;
  if(!el || !el.getAttribute) return;
  if(!el.getAttribute('data-needs-sync')) return;
  var sid = el.getAttribute('data-sid') || '';
  if(sid){
    swSync(sid);
    swSiteProxy(sid);
    swSyncRadios(sid);
  } else {
    /* 全局控件：代理方式单选 / AI 上限输入框。
       ⚠️ 原来的 swAiLimits() 已随"每日尝试上限"一起删除，这里忘了改，
       导致每次勾选站点开关都抛 ReferenceError（控制台报错、用户以为坏了）。
       全局这些控件对应的是 calcAiCalls() 与 swGlobalProxy()。 */
    swGlobalProxy();
    calcAiCalls();
  }
}, true);
document.addEventListener('input', function(e){
  var el = e.target;
  if(!el || !el.getAttribute) return;
  if(!el.getAttribute('data-needs-sync')) return;
  var sid = el.getAttribute('data-sid') || '';
  if(sid) swSync(sid); else calcAiCalls();
}, true);

/* 站点表单里三组单选（运行方式/签到时间/联网方式）切换后重算显隐 */
function swSyncRadios(id){
  swSync(id);
  swSiteProxy(id);
  /* 模式切换可能刚把"初始基准"锁定下来，这里同步一次显隐与提示 */
  onModeChange(id);
}

/* 自定义请求头：一行一个 key/value */
function headerRows(headers){
  var keys = Object.keys(headers||{});
  if(!keys.length) keys = [''];
  return keys.map(function(k){
    return '<div class="kvrow"><input class="hkey" placeholder="Origin" value="'+esc(k)+'">'
      + '<input class="hval" placeholder="https://example.com" value="'
      + esc(k? (headers[k]||'') : '')+'">'
      + '<button type="button" class="b" onclick="this.parentNode.remove()">×</button></div>';
  }).join('');
}

function renderSiteForm(id){
  var box = $('form-'+id);
  if(!box) return;
  api('GET','/api/site?id='+encodeURIComponent(id)).then(function(d){
    var s = d.site; SITE_CACHE[id] = s;
    api('GET','/api/site/cookie?id='+encodeURIComponent(id)).then(function(c){
      box.innerHTML = siteFormHtml(s, c);
      afterFormRender(id);
    }).catch(function(){
      box.innerHTML = siteFormHtml(s, {has_cookie:s.has_cookie,names:[]});
      afterFormRender(id);
    });
  }).catch(function(e){
    box.innerHTML = '<div class="err">读取站点失败：'+esc(e.message)+'</div>';
  });
}

/* 表单渲染完的收尾：快照基准值 + 重置脏标记 + 贴出待显示的提示 */
function afterFormRender(id){
  snapshotForm($('form-'+id));
  _formDirty = false;
  flushCookieMsg(id);
}

/* 表单渲染完后，把之前攒下的提示贴到新的提示区里（只贴一次） */
function flushCookieMsg(id){
  var pend = CK_PENDING_MSG[id];
  if(!pend) return;
  delete CK_PENDING_MSG[id];
  showCookieMsg(id, pend.text, pend.kind);
}

function siteFormHtml(s, c){
  var id = s.id, p = 'f_'+id+'_';
  var isDaily = (s.mode !== 'success_based');
  /* 站点级代理模式。老数据可能是空串，当作"跟随全局"处理 */
  var sitePmode = s.proxy_mode || 'inherit';
  if(['inherit','direct','custom'].indexOf(sitePmode) < 0) sitePmode = 'inherit';

  /* ---- 登录状态区 ---- */
  var ckStatus;
  if(!c.has_cookie){
    ckStatus = '<span class="tag err">未配置</span>'
      + '<span class="hint" style="margin:0">该站点签到需要登录，请按下面的说明粘贴 Cookie</span>';
  }else{
    var when = c.updated_at? fmtTime(c.updated_at) : '未知时间';
    var st = c.status===1? '<span class="tag ok">有效</span>'
           : c.status===2? '<span class="tag err">已失效</span>'
           : (c.checked_at? '<span class="tag">检查过，无法判断</span>'
                          : '<span class="tag">未检查</span>');
    ckStatus = st + '<span class="tag ok">已配置 '+c.count+' 项</span>'
      + '<span class="hint" style="margin:0">更新于 '+esc(when)
      + '（来源：'+esc(c.source||'未知')+'）</span>';
    if(c.error) ckStatus += '<div class="hint" style="color:#c0392b">'+esc(c.error)+'</div>';
  }

  var cookieNames = (c.names&&c.names.length)
    ? '<div class="hint">已保存字段：'+esc(c.names.join('、'))+'</div>' : '';

  /* ---- 模板可选项（目前是 NodeSeek 的签到方式：试试手气 / 鸡腿×5） ----
     由模板声明、由用户按站点选。id 里带 key，所以 key 只允许词字符：
     插件模板是外部 JSON，key 里混进引号会把 DOM id 搞坏，
     宁可不显示这一项，也不要拼出一个选择器都不对的下拉框。 */
  var optSpecs = (s.option_specs || []).filter(function(sp){
    return sp && /^[A-Za-z0-9_]+$/.test(sp.key || '');
  });
  var optSec = '';
  if(optSpecs.length){
    optSec = '<div class="fsec"><h4>签到选项</h4><div class="fgrid">'
      + optSpecs.map(function(sp){
          var cur = (s.options && s.options[sp.key]) || sp.default;
          var items = (sp.choices || []).map(function(ch){
            return '<option value="'+esc(ch.value)+'"'
              + (ch.value === cur ? ' selected' : '') + '>'
              + esc(ch.label) + '</option>';
          }).join('');
          return '<div><label>'+esc(sp.label)+'</label>'
            + '<select id="'+p+'opt_'+esc(sp.key)+'">'+items+'</select>'
            + (sp.note? '<div class="hint">'+esc(sp.note)+'</div>' : '')
            + '</div>';
        }).join('')
      + '</div></div>';
  }

  /* ---- 表单 ---- */
  return '<form onsubmit="return false">'

  + '<div class="fsec"><h4>基本</h4>'
  +   '<div class="fgrid">'
  +     '<div><label>站点名称</label><input id="'+p+'name" value="'+esc(s.name)+'"></div>'
  +     '<div><label>主页地址</label><input id="'+p+'home" value="'+esc(s.homepage)+'"></div>'
  +   '</div>'
  +   _sw(p+'enabled','启用签到', s.enabled, id)
  +   '<div class="fgrid" style="margin-top:4px">'
  +     '<div><label>运行方式</label><select id="'+p+'browser">'
  +       '<option value="0"'+(s.need_browser?'':' selected')+'>纯请求（推荐，快且省资源）</option>'
  +       '<option value="1"'+(s.need_browser?' selected':'')+'>需要浏览器（复杂交互站点）</option>'
  +     '</select></div>'
  +     '<div><label>证书校验</label><select id="'+p+'ssl">'
  +       '<option value="1"'+(s.verify_ssl?' selected':'')+'>正常校验（推荐）</option>'
  +       '<option value="0"'+(s.verify_ssl?'':' selected')+'>放宽校验（证书有问题的站点）</option>'
  +     '</select></div>'
  +   '</div>'
  + '</div>'

  + optSec

  + '<div class="fsec"><h4>签到时间</h4>'
  +   '<div class="radios">'
  +     '<label><input type="radio" name="'+p+'mode" value="daily"'
  +       (isDaily?' checked':'')+' data-needs-sync="1" data-sid="'+esc(id)+'">每天固定时刻</label>'
  +     '<label><input type="radio" name="'+p+'mode" value="success_based"'
  +       (isDaily?'':' checked')+' data-needs-sync="1" data-sid="'+esc(id)+'">'
  +       '以【上次签到成功时间】为基准</label>'
  +   '</div>'
  +   '<div class="hint" style="margin:6px 0 0">' + modeHintHtml(isDaily, s) + '</div>'
  +   '<div class="fgrid" style="margin-top:6px">'
  +     '<div id="'+p+'dailyBox"><label id="'+p+'timeLbl">'
  +       (isDaily? '时刻（24 小时制）'
                : (anchorLocked(s)? '初始基准时刻（已锁定）'
                                   : '初始基准时刻（填好后选上面的模式即可锁定）'))
  +       '</label>'
  +       '<span class="inline"><input type="number" id="'+p+'hour" min="0" max="23" value="'
  +         String(anchorHour(s)).padStart(2,'0')+'"'+(timeLocked(s, isDaily)?' disabled':'')+'> : '
  +       '<input type="number" id="'+p+'min" min="0" max="59" value="'
  +         String(anchorMinute(s)).padStart(2,'0')+'"'+(timeLocked(s, isDaily)?' disabled':'')+'>'
  +       (timeLocked(s, isDaily)? '<span class="hint" style="margin:0">🔒</span>' : '')
  +       '</span>'
  +       '<div class="hint" id="'+p+'timeHint">'+timeHintHtml(s)+'</div></div>'
  +     '<div id="'+p+'ivalBox"><label>间隔（分钟，1440＝24 小时）</label>'
  +       '<input type="number" id="'+p+'ival" min="60" max="10080" value="'
  +         (s.success_interval_minutes||1440)+'">'
  +       '<div class="hint" id="'+p+'ivalHint"></div></div>'
  +     '<div><label>随机延迟上限（分钟）</label>'
  +       '<span class="inline"><input type="number" id="'+p+'jitter" min="0" max="720" value="'
  +         Math.round((s.jitter_seconds||0)/60)+'"></span>'
  +       '<div class="hint">在基准时刻前后随机浮动，避免每天同一秒请求</div></div>'
  +   '</div>'
  +   _sw(p+'jitterOn','启用随机延迟', s.jitter_enabled, id)
  +   '<input type="hidden" id="'+p+'anchor" value="'+anchorMinutes(s)+'"'
  +     ' data-locked="'+(anchorLocked(s)?'1':'0')+'">'
  + '</div>'

  + '<div class="fsec"><h4>失败重试</h4>'
  +   _sw(p+'retryOn','失败后重试', s.retry_enabled, id)
  +   '<div class="fgrid" style="margin-top:6px">'
  +     '<div><label>重试次数</label><input type="number" id="'+p+'rc" min="0" max="10" value="'
  +       s.retry_count+'"></div>'
  +     '<div><label>重试间隔（分钟）</label><input type="number" id="'+p+'ri" min="1" max="1440" value="'
  +       s.retry_interval_minutes+'"></div>'
  +     '<div><label>单次执行超时（秒，0＝不限）</label>'
  +       '<input type="number" id="'+p+'tmo" min="0" max="3600" value="'
  +         (s.timeout_seconds == null ? 300 : s.timeout_seconds)+'">'
  +       '<div class="hint">一次签到最多跑多久。单个请求自己有超时，'
  +         '但很多步各自超时叠加起来会把当天整轮拖住 —— '
  +         '超过这个时间就判失败，继续跑下一个站点。</div></div>'
  +   '</div>'
  + '</div>'

  + '<div class="fsec"><h4>AI 分析</h4>'
  +   _sw(p+'aiOn','这个站点允许调用 AI 分析', s.ai_enabled, id)
  +   '<div class="hint" style="margin:6px 0 0">'
  +     '调用时机（每失败几次调用一次）和每天次数上限都是<b>全局设置</b>，'
  +     '在「设置 → DeepSeek」里改。这里只决定这个站点允不允许被分析。<br>'
  +     '签到重试次数用上面的「失败重试」——那才是决定"一天试几次"的地方。'
  +   '</div>'
  + '</div>'

  + '<div class="fsec"><h4>通知</h4>'
  +   '<div class="fgrid"><div><label>该站点的通知策略</label>'
  +     '<select id="'+p+'notify">'
  +       '<option value=""'+(!s.notify?' selected':'')+'>跟随全局设置</option>'
  +       '<option value="all"'+(s.notify==='all'?' selected':'')+'>每次都通知</option>'
  +       '<option value="fail"'+(s.notify==='fail'?' selected':'')+'>仅失败时通知</option>'
  +       '<option value="success"'+(s.notify==='success'?' selected':'')+'>仅成功时通知</option>'
  +       '<option value="none"'+(s.notify==='none'?' selected':'')+'>不通知</option>'
  +     '</select></div></div>'
  + '</div>'

  + '<div class="fsec"><h4>🔑 登录状态（Cookie）</h4>'
  +   '<div class="ckstat">'+ckStatus+'</div>' + cookieNames
  +   '<div class="ckbox">'
  +     '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;'
  +       'margin-bottom:8px">'
  +       qdButtonHtml(s)
  +       '<button type="button" class="b" data-fact="cookieHelper"'
  +         ' data-sid="'+esc(id)+'">'
  +         '📖 怎么用？</button>'
  +     '</div>'
  +     '<div class="tip" style="margin-bottom:8px">'
  +       '<b>推荐：</b>装一次 QD 的 <b>Cookies获取助手</b> 浏览器扩展，'
  +       '点上面的按钮就能直接读走登录 Cookie（<b>含 HttpOnly 字段</b>，'
  +       '这是唯一能拿到完整登录态的办法）。首次使用要点「怎么用？」看一眼设置。'
  +     '</div>'
  +     '<textarea id="'+p+'cookie" spellcheck="false" placeholder="'
  +       '或者手动粘贴：Copy as cURL 的整段、a=1; b=2 这样的 Cookie 串、'
  +       'DevTools 里一行一个的键值列表、cookies.txt"></textarea>'
  +     '<div class="tip">'
  +       '<b>手动粘贴怎么拿：</b>用你自己的浏览器打开 ' + safeLink(s.homepage) + ' 并登录 → 按 <b>F12</b> → '
  +       '<b>Network</b> 标签 → 按 <b>F5</b> 刷新 → 点列表里第一条请求 → '
  +       '<b>右键 → Copy → Copy as cURL</b> → 把整段粘到上面的框里。<br>'
  +       '不用自己整理格式，粘进来会自动提取。'
  +       '注意 <b>document.cookie 拿不到 HttpOnly 的登录字段</b>，所以别用控制台。'
  +     '</div>'
  +     '<div class="formfoot">'
  +       '<button type="button" class="b pri" data-fact="saveCookie"'
  +         ' data-sid="'+esc(id)+'">保存 Cookie</button>'
  +       '<button type="button" class="b" data-fact="checkCookie"'
  +         ' data-sid="'+esc(id)+'">检查登录状态</button>'
  +       '<button type="button" class="b danger" data-fact="clearCookie"'
  +         ' data-sid="'+esc(id)+'">清除</button>'
  +       '<span class="grow"></span>'
  +     '</div>'
  +     '<div id="'+p+'ckmsg" class="msg"></div>'
  +   '</div>'
  + '</div>'

  + '<div class="fsec"><h4>联网方式</h4>'
  +   '<div class="hint" style="margin:0 0 4px">这个站点单独走不走代理。'
  +     '国内站点一般直连即可；被墙或被污染的域名需要走代理。</div>'
  +   '<div class="radios">'
  +     '<label><input type="radio" name="'+p+'pmode" value="inherit"'
  +       (sitePmode==='inherit'?' checked':'')
  +       ' data-needs-sync="1" data-sid="'+esc(id)+'">跟随全局设置</label>'
  +     '<label><input type="radio" name="'+p+'pmode" value="direct"'
  +       (sitePmode==='direct'?' checked':'')
  +       ' data-needs-sync="1" data-sid="'+esc(id)+'">强制直连</label>'
  +     '<label><input type="radio" name="'+p+'pmode" value="custom"'
  +       (sitePmode==='custom'?' checked':'')
  +       ' data-needs-sync="1" data-sid="'+esc(id)+'">用这个代理</label>'
  +   '</div>'
  +   '<div id="'+p+'proxyBox" style="margin-top:8px;'
  +     (sitePmode==='custom'?'':'display:none')+'">'
  +     '<label>代理地址</label>'
  +     '<input id="'+p+'proxy" placeholder="http://10.0.0.1:7890" value="'
  +       esc(s.proxy_url||'')+'">'
  +   '</div>'
  + '</div>'

  + '<div class="fsec"><h4>自定义请求头</h4>'
  +   '<div class="hint" style="margin:0 0 6px">有些站点的接口会校验来源，'
  +     '缺了会返回 403。模板通常已预置常用的头，一般不用改。</div>'
  +   '<div id="'+p+'hdr">'+headerRows(s.headers)+'</div>'
  +   '<button type="button" class="b" data-fact="addHeader"'
  +     ' data-sid="'+esc(id)+'">+ 增加一行</button>'
  + '</div>'

  + '<div class="formfoot">'
  +   '<button type="button" class="b pri" data-fact="saveSite"'
  +     ' data-sid="'+esc(id)+'">保存</button>'
  +   '<button type="button" class="b" data-fact="cancel"'
  +     ' data-sid="'+esc(id)+'">取消</button>'
  +   '<span class="grow"></span>'
  +   '<span class="hint" style="margin:0">ID：'+esc(id)+'</span>'
  + '</div>'
  + '</form>';
}

/* 切换开关/单选时同步禁用的子项与标签底色 */
function swSync(id){
  var p = 'f_'+id+'_';
  function set(el, on){ if(el){ el.disabled = !on; } }
  function lb(el, on){
    if(!el) return;
    var w = el.closest ? el.closest('.sw') : null;
    if(w) w.className = 'sw' + (on?'':' off');
  }
  var eOn = $(p+'enabled'); lb(eOn, !!(eOn&&eOn.checked));
  var jOn = $(p+'jitterOn'); set($(p+'jitter'), !!(jOn&&jOn.checked)); lb(jOn, !!(jOn&&jOn.checked));
  var rOn = $(p+'retryOn');
  set($(p+'rc'), !!(rOn&&rOn.checked)); set($(p+'ri'), !!(rOn&&rOn.checked));
  lb(rOn, !!(rOn&&rOn.checked));
  var aOn = $(p+'aiOn'); lb(aOn, !!(aOn&&aOn.checked));
  var daily = document.querySelector('input[name="'+p+'mode"][value="daily"]');
  var isDaily = !!(daily && daily.checked);
  /* 时刻框可用性：daily 可编辑；成功模式未锁定也可编辑（先让用户填），
     锁定后才禁用。只看 isDaily 会把"选了成功模式但还没锁定"误禁用。 */
  var aHidden = $(p+'anchor');
  var aVal = aHidden ? parseInt(aHidden.value, 10) : -1;
  var aLocked = aHidden && aHidden.getAttribute
    ? aHidden.getAttribute('data-locked') === '1' : false;
  var locked = !isDaily && aLocked && !(isNaN(aVal) || aVal < 0);
  set($(p+'hour'), !locked); set($(p+'min'), !locked);
  /* 标题与锁图标跟着锁状态变 */
  var lbl = $(p+'timeLbl');
  if(lbl){
    lbl.textContent = isDaily ? '时刻（24 小时制）'
      : (locked ? '初始基准时刻（已锁定）'
                : '初始基准时刻（填好后选上面的模式即可锁定）');
  }
  var box = $(p+'dailyBox'); if(box) box.style.opacity = isDaily? '1':'0.45';
  /* 间隔输入只在"以成功时间为基准"模式下有意义 */
  set($(p+'ival'), !isDaily);
  var ibox = $(p+'ivalBox'); if(ibox) ibox.style.opacity = isDaily? '0.45':'1';
  /* 实时把分钟换算成"几小时"，避免用户对着 1440 猜 */
  var hint = $(p+'ivalHint');
  if(hint){
    var v = parseInt(($(p+'ival')||{}).value || '1440', 10);
    if(isNaN(v) || v <= 0){ hint.textContent = ''; }
    else if(v % 60 === 0){ hint.textContent = '＝ ' + (v/60) + ' 小时'; }
    else { hint.textContent = '＝ ' + Math.floor(v/60) + ' 小时 ' + (v%60) + ' 分钟'; }
  }
}

function addHeaderRow(id){
  var box = $('f_'+id+'_hdr');
  if(!box) return;
  var d = document.createElement('div');
  d.className = 'kvrow';
  d.innerHTML = '<input class="hkey" placeholder="Origin">'
    + '<input class="hval" placeholder="https://example.com">'
    + '<button type="button" class="b" onclick="this.parentNode.remove()">×</button>';
  box.appendChild(d);
}

function collectHeaders(id){
  var box = $('f_'+id+'_hdr');
  var out = {};
  if(!box) return out;
  var rows = box.querySelectorAll('.kvrow');
  for(var i=0;i<rows.length;i++){
    var k = rows[i].querySelector('.hkey').value.trim();
    var v = rows[i].querySelector('.hval').value;
    if(k) out[k] = v;
  }
  return out;
}

function saveSite(id){
  var p = 'f_'+id+'_';
  var daily = document.querySelector('input[name="'+p+'mode"][value="daily"]');
  var isDaily = !!(daily && daily.checked);

  /* 模板可选项（NodeSeek 的签到方式等）。
     ⚠️ 只在**确实取到值**时才提交：表单没展开时这些 select 根本不在 DOM 里，
       传一个空对象上去会被后端当成"用户清空了设置"，把选好的值冲回默认。 */
  var optPatch = null;
  var specs = (SITE_CACHE[id] && SITE_CACHE[id].option_specs) || [];
  specs.forEach(function(sp){
    var el = $(p+'opt_'+sp.key);
    if(!el) return;
    optPatch = optPatch || {};
    optPatch[sp.key] = el.value;
  });

  var patch = {
    id: id,
    name: $(p+'name').value.trim(),
    homepage: $(p+'home').value.trim(),
    enabled: $(p+'enabled').checked,
    need_browser: $(p+'browser').value === '1',
    verify_ssl: $(p+'ssl').value === '1',
    mode: isDaily ? 'daily' : 'success_based',
    daily_hour: parseInt($(p+'hour').value||'0',10),
    daily_minute: parseInt($(p+'min').value||'0',10),
    jitter_enabled: $(p+'jitterOn').checked,
    jitter_seconds: Math.max(0, parseInt($(p+'jitter').value||'0',10)) * 60,
    retry_enabled: $(p+'retryOn').checked,
    retry_count: parseInt($(p+'rc').value||'0',10),
    retry_interval_minutes: parseInt($(p+'ri').value||'30',10),
    /* 单次执行超时。0 = 不限；输入框被清空时按默认 300 秒走，
       不要变成 0 —— 那等于"永远不超时"，正好是用户想避免的情况。 */
    timeout_seconds: (function(){
      var el = $(p+'tmo');
      if(!el) return 300;
      var v = parseInt(el.value, 10);
      if(isNaN(v) || v < 0) return 300;
      return Math.min(3600, v);
    })(),
    success_interval_minutes: Math.max(60, parseInt($(p+'ival').value||'1440',10)),
    /* 初始基准时刻：-1 = 还没锁定。
       ⚠️ 必须看 data-locked 而不是 hidden 的值：未锁定时 hidden 里放的是
       "预览值"（当前 daily 时刻），直接当锚点提交会把基准悄悄锁成 03:00
       —— 用户什么都没改却被锁了（实测发现的缺陷）。 */
    success_anchor_minutes: (function(){
      var hid = $(p+'anchor');
      if(!hid) return -1;
      var isLocked = hid.getAttribute
        && hid.getAttribute('data-locked') === '1';
      if(!isLocked) return -1;
      var v = parseInt(hid.value, 10);
      if(isNaN(v) || v < 0) return -1;
      return v % 1440;
    })(),
    ai_enabled: $(p+'aiOn').checked,
    notify: $(p+'notify').value,
    proxy_mode: radioValue(p+'pmode') || 'inherit',
    proxy_url: ($(p+'proxy') ? $(p+'proxy').value.trim() : ''),
    headers: collectHeaders(id)
  };
  if(optPatch) patch.options = optPatch;
  api('POST','/api/site',patch).then(function(){
    OPEN_SITE = '';
    loadSites();
  }).catch(function(e){ alert('保存失败：'+e.message); });
}

/* ---- Cookie 操作 ---- */
function saveCookie(id){
  var box = $('f_'+id+'_cookie');
  var val = box? box.value : '';
  if(!val.trim()){ alert('请先粘贴 Cookie 内容'); return; }
  api('POST','/api/site/cookie',{id:id, cookie:val}).then(function(d){
    box.value = '';
    show('f_'+id+'_ckmsg',
         '已保存 '+d.count+' 项（识别为：'+d.format+'）', 'ok');
    loadSites();
  }).catch(function(e){
    show('f_'+id+'_ckmsg', '保存失败：'+e.message, 'err');
  });
}

function checkCookie(id){
  showCookieMsg(id, '检查中…（最多 25 秒）', '');
  api('POST','/api/site/cookie/check',{id:id}).then(function(d){
    var cls = d.status===1? 'ok' : (d.status===2? 'err' : '');
    /* 先存到"待显示"，再刷新：刷新可能重建整行（连败数/模板状态变化时），
       直接写在旧 DOM 上的字会被冲掉，用户就以为按钮没反应。 */
    CK_PENDING_MSG[id] = {text: d.message, kind: cls};
    showCookieMsg(id, d.message, cls);
    loadSites();
  }).catch(function(e){
    showCookieMsg(id, '检查失败：'+e.message, 'err');
  });
}

function clearCookie(id){
  if(!confirm('确定清除该站点的登录 Cookie？清除后签到会因未登录而失败。')) return;
  api('POST','/api/site/cookie/clear',{id:id}).then(function(){
    loadSites();
  }).catch(function(e){ alert('清除失败：'+e.message); });
}

/* ============ 自动导入：承接 QD 的 get-cookies 扩展 ============
 *
 * 用的是现成扩展（开源、348 star）：
 *     https://github.com/qd-today/get-cookies
 * 它由 QD 官方维护，用浏览器特权 API chrome.cookies 读 Cookie
 * ——这是唯一能拿到 HttpOnly 登录字段的途径（网页脚本读不到）。
 *
 * 通信方式（已实测确认）：
 *   它在容器页面里监听 click，命中 [data-toggle="get-cookie"] 就去读 Cookie，
 *   然后 window.postMessage({info:'cookieRaw', data:{名字:值,...}}, '*')
 * 所以只要两件事：
 *   ① 页面上放一个带那些 data-* 属性的按钮
 *   ② 监听 message，收到 cookieRaw 就转成 Cookie 串调我们自己的接口存起来
 *
 * 扩展是在**我们自己的页面**上跑的，浏览器会自动带上本容器的登录会话 Cookie，
 * 直接复用现有认证即可，不需要额外的令牌。
 */

var COOKIE_IMPORT_PENDING = '';
var QD_LISTENER_BOUND = false;
/* 待显示给用户的提示。表单每次渲染都会被重建，所以提示要先存在这里，
   等新表单渲染完再贴上去，否则"已保存"会被刷新动作冲掉。 */
var CK_PENDING_MSG = {};

/* 从 URL 取主机名；失败返回空串（宁可不填也不填错，
   因为 data-site/data-domain 填错会让扩展查不到 Cookie） */
function hostOf(url){
  try{ return new URL(url).hostname || ''; }
  catch(e){ return ''; }
}

function listenQdExtension(){
  if(QD_LISTENER_BOUND) return;      /* 避免重复挂载导致一次点击存两遍 */
  QD_LISTENER_BOUND = true;
  window.addEventListener('message', function(ev){
    var d = ev.data;
    if(!d || d.info !== 'cookieRaw') return;      /* 只认扩展的这一种消息 */
    var sid = COOKIE_IMPORT_PENDING;
    if(!sid) return;                               /* 不是我们发起的，忽略 */
    var raw = d.data;
    if(!raw || raw.error){
      showCookieMsg(sid, '扩展没能读到 Cookie：'
        + ((raw && raw.error) || '返回为空')
        + '　（通常是目标网站那边还没登录）', 'err');
      return;
    }
    var pairs = [];
    for(var k in raw){
      if(Object.prototype.hasOwnProperty.call(raw, k)) pairs.push(k + '=' + raw[k]);
    }
    if(!pairs.length){
      showCookieMsg(sid, '扩展返回的 Cookie 是空的，请确认目标网站已登录', 'err');
      return;
    }
    /* 只填进输入框，不自动保存 —— 用户要能先看一眼再决定。
       这样它和"手动粘贴"走的是同一条路径（保存 → 检查），行为一致。 */
    var text = pairs.join('; ');
    var ta = $('f_' + sid + '_cookie');
    if(ta){
      ta.value = text;
      try{ ta.dispatchEvent(new Event('input', {bubbles:true})); }catch(e){}
    }
    showCookieMsg(sid, '已填入下面的输入框（' + pairs.length
      + ' 项）。确认无误后点「保存 Cookie」，再点「检查登录状态」即可。', 'ok');
    COOKIE_IMPORT_PENDING = '';      /* 用完清掉，避免下次误关联到别的站点 */
  }, false);
}

/* 供扩展识别的按钮。data-* 的名字与含义按扩展源码：
     data-toggle="get-cookie"  扩展靠它识别
     data-site   目标网址，扩展用它做 cookies.getAll({url})
     data-domain 目标域名，扩展再做一次 getAll({domain}) 兜底
     data-name   仅用于扩展弹出的确认框里显示 */
function qdButtonHtml(s){
  var url = s.homepage || '';
  var host = hostOf(url);
  var root = rootDomain(host);
  return '<button type="button" class="b pri"'
    + ' data-toggle="get-cookie"'
    + ' data-site="' + esc(url) + '"'
    + ' data-domain="' + esc(root) + '"'
    + ' data-name="' + esc(s.name) + '"'
    + ' data-fact="markCookieImport" data-sid="' + esc(s.id) + '">'
    + '⚡ 从浏览器读取 Cookie</button>';
}

/* 把 www.a.com -> .a.com：扩展会查 domain=.a.com，能同时覆盖子域 */
function rootDomain(host){
  if(!host) return '';
  var parts = host.split('.');
  if(parts.length <= 2) return host;
  var last2 = parts.slice(-2).join('.');
  /* 处理 co.uk / com.cn 这类两段式后缀 */
  var twoPartSuffix = ['co.uk','com.cn','com.hk','com.tw','co.jp','org.cn','net.cn'];
  if(twoPartSuffix.indexOf(last2) >= 0 && parts.length >= 3){
    return parts.slice(-3).join('.');
  }
  return last2;
}

function markCookieImportStart(id){ COOKIE_IMPORT_PENDING = id; }

function showCookieMsg(id, text, kind){
  var el = $('f_'+id+'_ckmsg');
  if(el) show('f_'+id+'_ckmsg', text, kind);
}

/* ============ 自动导入：承接 QD 的 get-cookies 扩展 ============ */

function originOf(){
  /* 容器对外的地址就用当前页面访问的地址，最准（含端口） */
  return location.origin || (location.protocol+'//'+location.host);
}

function openCookieHelper(id){
  var old = $('acHelp');
  if(old) old.parentNode.removeChild(old);

  var s = SITE_CACHE[id] || {};
  var origin = originOf();

  var box = document.createElement('div');
  box.id = 'acHelp';
  box.setAttribute('style',
    'position:fixed;inset:0;background:rgba(20,26,36,.55);z-index:9999;'
    + 'display:flex;align-items:center;justify-content:center;padding:20px');
  box.innerHTML =
    '<div style="background:#fff;border-radius:10px;max-width:640px;width:100%;'
    + 'max-height:88vh;overflow:auto;padding:20px 22px;'
    + 'box-shadow:0 14px 44px rgba(0,0,0,.3)">'
    + '<div style="display:flex;justify-content:space-between;align-items:center">'
    +   '<h3 style="margin:0;font-size:15px">怎么用「从浏览器读取 Cookie」</h3>'
    +   '<span style="cursor:pointer;color:#8b95a3;font-size:22px;line-height:1" '
    +     'onclick="closeCookieHelper()">&times;</span>'
    + '</div>'

    + '<div class="tip" style="margin-top:10px;font-size:12.5px;line-height:1.8">'
    +   '这个按钮靠一个现成的开源扩展干活：<b>Cookies获取助手[QD]</b>。'
    +   '它用浏览器特权接口读 Cookie，<b>能读到 HttpOnly 的登录字段</b> —— '
    +   '这是唯一能拿到完整登录态的办法（网页脚本和控制台都读不到）。'
    +   '<br>扩展由 QD 项目官方维护：'
    +   '<a href="https://github.com/qd-today/get-cookies" target="_blank" '
    +   'rel="noopener">github.com/qd-today/get-cookies</a>'
    + '</div>'

    + '<div class="fsec" style="margin-top:14px">'
    +   '<h4>第 1 步 · 装扩展（只需一次）</h4>'
    +   '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
    +     '<a class="b" style="text-decoration:none;display:inline-block;'
    +       'padding:5px 12px;border-radius:5px;background:#2d6adc;color:#fff;'
    +       'font-size:12px" target="_blank" rel="noopener" '
    +       'href="https://chromewebstore.google.com/detail/'
    +       'cookies%E8%8E%B7%E5%8F%96%E5%8A%A9%E6%89%8B/'
    +       'mmcdaoockinhaeiljdmjmnjfndpfpklo">Chrome / Edge 商店</a>'
    +     '<a class="b" style="text-decoration:none;display:inline-block;'
    +       'padding:5px 12px;border-radius:5px;border:1px solid #dde3ec;'
    +       'background:#fff;color:#242a33;font-size:12px" target="_blank" '
    +       'rel="noopener" href="https://addons.mozilla.org/zh-CN/firefox/addon/'
    +       'cookies%E8%8E%B7%E5%8F%96%E5%8A%A9%E6%89%8B-qd/">Firefox 商店</a>'
    +   '</div>'
    +   '<div class="tip" style="font-size:11.5px;color:#8b95a3;margin-top:8px;'
    +     'line-height:1.7">装不上商店版也可以从 '
    +     '<a href="https://github.com/qd-today/get-cookies/releases/latest" '
    +     'target="_blank" rel="noopener">Releases</a> 下载，'
    +     'Chrome 用「加载已解压的扩展程序」手动载入。</div>'
    + '</div>'

    + '<div class="fsec">'
    +   '<h4>第 2 步 · 让扩展在本容器页面也生效（关键，最容易漏）</h4>'
    +   '<div class="tip" style="font-size:12.5px;line-height:1.9;color:#3a424d">'
    +     '扩展默认只认 QD 地址。要让它在本容器页面生效，'
    +     '需要打开扩展的<b>选项页</b>，把下面这个地址<b>填进去</b>'
    +     '（按空格/换行分隔，可并列填多个）：'
    +   '</div>'
    +   '<div style="display:flex;gap:8px;align-items:center;margin-top:8px;'
    +     'flex-wrap:wrap">'
    +     '<input id="acExtAddr" readonly value="'+esc(origin)+'" '
    +       'style="flex:1;min-width:220px;font-family:ui-monospace,Consolas,'
    +       'monospace;font-size:12.5px;padding:6px 9px;border:1px solid #dde3ec;'
    +       'border-radius:5px;background:#f7f9fc">'
    +     '<button type="button" class="b" onclick="copyExtAddr()">复制</button>'
    +   '</div>'
    +   '<div class="tip" style="font-size:11.5px;color:#8b95a3;margin-top:8px;'
    +     'line-height:1.7">'
    +     '打开方式：浏览器右上角扩展图标 → 找到「Cookies获取助手」→ '
    +     '右键选<b>选项</b>；或在「扩展程序」页点它的「详情」→「扩展程序选项」。'
    +     '<br>填 <span class="mono">'+esc(origin)+'</span> 即可。'
    +   '</div>'
    + '</div>'

    + '<div class="fsec">'
    +   '<h4>第 3 步 · 回来点按钮</h4>'
    +   '<ol style="margin:0 0 0 18px;padding:0;font-size:12.5px;line-height:1.9;'
    +     'color:#3a424d">'
    +     '<li>确认你的浏览器里已经<b>登录了 '+esc(s.name || id)+'</b>'
    +       '（没登录的话扩展只能读到空）</li>'
    +     '<li>回到本页面，点「<b>⚡ 从浏览器读取 Cookie</b>」</li>'
    +     '<li>扩展会弹一个确认框，点<b>确定</b></li>'
    +     '<li>Cookie 会自动送进来并保存，下面会显示「已保存 N 项」</li>'
    +     '<li>再点「<b>检查登录状态</b>」确认</li>'
    +   '</ol>'
    +   '<div class="tip" style="font-size:11.5px;color:#8b95a3;margin-top:8px;'
    +     'line-height:1.7">'
    +     '<b>点了没反应？</b>按顺序排查：'
    +     '① 第 2 步的地址还没填进扩展选项页；'
    +     '② 本页面是改配置前打开的，<b>刷新一下</b>；'
    +     '③ 扩展没启用，或当前窗口是隐私模式 —— '
    +     '隐私模式需要在扩展详情里单独打开「在无痕模式下启用」，'
    +     '并且要在隐私窗口里也登录目标网站。'
    +   '</div>'
    + '</div>'

    + '<div class="fsec">'
    +   '<h4>不想装扩展？手动粘贴也可以</h4>'
    +   '<div class="tip" style="font-size:12.5px;line-height:1.9;color:#3a424d">'
    +     '在你已登录的浏览器里按 F12 → Network → F5 → 点第一条请求 → '
    +     '右键 Copy as cURL → 粘到上面的输入框。'
    +     '一次复制就含 HttpOnly 字段，不用装任何东西。'
    +   '</div>'
    + '</div>'

    + '<div class="formfoot">'
    +   '<button type="button" class="b" onclick="closeCookieHelper()">关闭</button>'
    + '</div>'
    + '</div>';
  document.body.appendChild(box);
}

function copyExtAddr(){
  var el = $('acExtAddr');
  if(!el) return;
  var val = el.value;
  el.select();
  var ok = false;
  try{ ok = document.execCommand('copy'); }catch(e){ ok = false; }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(val).then(function(){}, function(){});
    ok = true;
  }
  if(!ok && !navigator.clipboard){ alert('请手动复制：' + val); }
}

function closeCookieHelper(){
  var b = $('acHelp');
  if(b) b.parentNode.removeChild(b);
}

/* ---------------- 刷新与"正在编辑"保护 ---------------- */

/* 表单是否被改动过。
   为什么需要：后台每 30 秒自动刷新一次，而刷新会重渲染站点卡片
   —— 正在编辑的表单会被整块替换掉，用户填到一半的内容就没了
   （实测：粘贴 Cookie 到一半被冲掉）。所以改动过就暂停自动刷新。 */
var _formDirty = false;
var _pendingSitesRefresh = false;

/* 标记原始值。用不可见字符做属性前缀，避免与站点自定义的字段名冲突。 */
var _MARK = '\uE000acOrig\uE000';

function snapshotForm(box){
  if(!box) return;
  box.querySelectorAll('input, textarea, select').forEach(function(el){
    if(el.type === 'checkbox' || el.type === 'radio'){
      el.setAttribute(_MARK, el.checked ? '1' : '0');
    }else{
      el.setAttribute(_MARK, el.value);
    }
  });
}

/* 事件委托挂在 document 上：表单是动态重建的，逐个绑定会漏。
   只监听输入类事件，不用 setInterval 轮询比对。 */
document.addEventListener('input', function(e){
  var el = e.target;
  if(!el || !el.closest) return;
  if(el.closest('#tab-sites')) _formDirty = true;
}, true);
document.addEventListener('change', function(e){
  var el = e.target;
  if(!el || !el.closest) return;
  if(el.closest('#tab-sites')) _formDirty = true;
}, true);

/* 用户在表单里改过东西吗？（对比当前值与快照） */
function formHasEdits(){
  var box = document.querySelector('#siteRows form');
  if(!box) return false;
  var els = box.querySelectorAll('input, textarea, select');
  for(var i = 0; i < els.length; i++){
    var el = els[i];
    var orig = el.getAttribute(_MARK);
    if(orig === null) return true;          /* 新出现的字段，视为已改动 */
    if(el.type === 'checkbox' || el.type === 'radio'){
      if((el.checked ? '1' : '0') !== orig) return true;
    }else if(el.value !== orig){
      return true;
    }
  }
  return false;
}

function formIsOpen(){
  var box = document.querySelector('#siteRows form');
  return box ? box.closest('.site') : null;
}

function currentEditId(){
  var s = formIsOpen();
  return s ? s.id.replace('site-', '') : '';
}

/* 定时/手动刷新。
   保护策略分两层：
     1. 数据没变 -> loadSites 直接跳过 DOM 重建（主要防线，见 loadSites）
     2. 数据变了但用户正在编辑 -> 记下待办，先不重建，
        避免把填到一半的内容冲掉；编辑结束（保存/取消）后再补刷新
   这样既不会"动不动就刷新"，也不会因为怕刷新而丢掉数据变化。 */
function refreshAll(){
  loadOverview();
  if(formIsOpen() && (_formDirty || formHasEdits())){
    _pendingSitesRefresh = true;
    var id = currentEditId();
    var el = $('f_'+id+'_ckmsg');
    if(el && !el.textContent){
      show('f_'+id+'_ckmsg',
           '检测到你正在编辑，已暂停刷新列表，内容不会被冲掉。'
           + '保存或取消后会自动刷新。', '');
    }
    return;
  }
  loadSites();
}

/* 保存/取消后：如果之前因为编辑而暂停过，补一次刷新 */
function resumeAutoRefresh(){
  _formDirty = false;
  if(_pendingSitesRefresh){
    _pendingSitesRefresh = false;
    loadSites();
  }
}

function runNow(){
  show('runMsg','执行中…','');
  api('POST','/api/run',{}).then(function(d){
    show('runMsg', d.summary + '\n' + (d.runs||[]).map(function(r){
      return (r.success?'✔ ':'✘ ') + r.site_name + '：' + r.message
        + (r.ai_note? ('\n   AI：'+r.ai_note):'')
        + (Object.keys(r.notified||{}).length? ('\n   通知：'+JSON.stringify(r.notified)):'');
    }).join('\n'), 'ok');
    refreshAll();
  }).catch(function(e){ show('runMsg', e.message, 'err'); });
}
function runSite(id){
  show('runMsg','执行中…','');
  api('POST','/api/run',{id:id}).then(function(d){
    show('runMsg', d.summary + '\n' + (d.runs||[]).map(function(r){
      return (r.success?'✔ ':'✘ ')+r.site_name+'：'+r.message
        +(r.ai_note? ('\n   AI：'+r.ai_note):'');
    }).join('\n'), 'ok');
    refreshAll();
  }).catch(function(e){ show('runMsg', e.message, 'err'); });
}
function toggleSite(id, on){
  api('POST','/api/site/toggle',{id:id, enabled:!!on}).then(loadSites)
    .catch(function(e){ alert(e.message); });
}
function delSite(id){
  if(!confirm('确定删除站点「'+id+'」？')) return;
  api('POST','/api/site/delete',{id:id}).then(loadSites)
    .catch(function(e){ alert(e.message); });
}
/* 旧的「编辑」是 prompt() 里手写 JSON，已由卡片内联表单取代
   （见 toggleEdit / renderSiteForm / saveSite）。 */

/* ---------------- 新增站点 / 录制 ---------------- */
function loadMeta(){
  api('GET','/api/meta').then(function(d){
    META = d;
    $('newSiteTplList').textContent = (d.templates||[]).map(function(t){
      return t.id + (t.need_browser?'(浏览器)':'');
    }).join('、');
  });
}

function startRec(){
  var id = $('ns_id').value.trim(), name = $('ns_name').value.trim(),
      url = $('ns_url').value.trim();
  if(!id){ show('recMsg','请先填写站点标识（英文）','err'); return; }
  if(!/^[A-Za-z0-9_\-]{1,64}$/.test(id)){
    show('recMsg','站点标识只能用英文字母、数字、下划线、连字符','err'); return; }
  if(!url){ show('recMsg','请先填写主页地址','err'); return; }
  api('POST','/api/record/start',{id:id,name:name||id,start_url:url})
    .then(function(){
      REC.id = id; REC.active = true;
      // 新建站点：先落一份基本配置
      api('POST','/api/site',{id:id,name:name||id,homepage:url,kind:'custom',
                              need_browser:true, enabled:true}).then(function(){
        show('recMsg','录制已开始。请在弹出的窗口里登录并完成一次签到。','ok');
        $('recFrameWrap').className='';
        $('recFrame').src = '/api/record/script.js';
      });
    }).catch(function(e){ show('recMsg', e.message, 'err'); });
}

function refreshRec(){
  if(!REC.id){ show('recMsg','还没有开始录制','err'); return; }
  api('GET','/api/record/status?id='+encodeURIComponent(REC.id)).then(function(d){
    $('recSteps').value = JSON.stringify(d.steps||[], null, 1);
    show('recMsg','已记录 '+d.event_count+' 个事件，整理成 '+(d.steps||[]).length+' 步','ok');
  }).catch(function(e){ show('recMsg', e.message, 'err'); });
}

function stopRec(save){
  if(!REC.id){ show('recMsg','还没有开始录制','err'); return; }
  var steps = null, raw = $('recSteps').value.trim();
  if(raw){
    try { steps = JSON.parse(raw); }
    catch(e){ show('recMsg','步骤 JSON 格式不对：'+e.message,'err'); return; }
  }
  api('POST','/api/record/stop',{id:REC.id, save:!!save, append:false})
    .then(function(d){
      if(save && steps && steps.length){
        // 用界面上编辑过的步骤覆盖
        api('POST','/api/site',{id:REC.id, steps:steps, kind:'custom',
                                need_browser:true}).then(function(){
          show('recMsg','已保存 '+steps.length+' 步。','ok');
          REC.id=''; $('recFrameWrap').className='hide'; refreshAll();
        });
      } else {
        show('recMsg', save? ('已保存 '+d.total_steps+' 步。') : '已放弃录制。', 'ok');
        REC.id=''; $('recFrameWrap').className='hide'; refreshAll();
      }
    }).catch(function(e){ show('recMsg', e.message, 'err'); });
}

/* ---------------- 站点模板：同步 + 可选列表 ---------------- */
function fmtTime(ts){
  if(!ts) return '从未';
  var d = new Date(ts*1000);
  function p(n){ return (n<10?'0':'')+n; }
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '
         +p(d.getHours())+':'+p(d.getMinutes());
}

function loadSyncStatus(){
  api('GET','/api/templates/sync/status').then(function(s){
    var parts = [];
    parts.push(s.enabled ? '自动同步：开' : '自动同步：关');
    parts.push('来源：'+(s.source ? esc(s.source) : '（未填）'));
    parts.push('时刻：'+esc(s.time)+'，每 '+s.interval_days+' 天');
    parts.push('上次同步：'+fmtTime(s.last_at));
    if(s.next_at) parts.push('下次：'+fmtTime(s.next_at));
    var last = s.last || {};
    if(last.at){
      if(last.ok){
        parts.push('上次结果：新增 '+(last.added||0)+'，更新 '+(last.updated||0)
                   +'，移除 '+(last.removed||0));
      } else {
        parts.push('<span style="color:#c33">上次失败：'
                   +esc(last.error||'')+'</span>');
      }
    }
    show('syncMsg', parts.join('　·　'), (last.at && !last.ok) ? 'err' : 'ok');
  }).catch(function(e){ show('syncMsg', e.message, 'err'); });
}

function syncNow(){
  var src = $('set_sync_src').value.trim();
  show('syncMsg','正在同步…','');
  api('POST','/api/templates/sync',{source:src}).then(function(r){
    var msg = '同步完成：新增 '+(r.added||[]).length
            + '，更新 '+(r.updated||[]).length
            + '，移除 '+(r.removed||[]).length;
    if((r.errors||[]).length){
      msg += '；有 '+r.errors.length+' 个被拒绝（见下表）';
    }
    show('syncMsg', msg, 'ok');
    loadTemplates();
  }).catch(function(e){ show('syncMsg', e.message, 'err'); });
}

function loadTemplates(){
  api('GET','/api/templates').then(function(d){
    var list = d.templates || [];
    $('plugDir').innerHTML = d.dir ? '　存放目录：<code>'+esc(d.dir)+'</code>' : '';
    /* 被拒绝的插件文件要显示原因，否则用户不知道文件为什么没生效 */
    var errs = d.errors || [];
    $('plugErrors').innerHTML = errs.length
      ? '<div class="hint" style="color:#c33">有 '+errs.length
        +' 个文件未生效：<br>'
        + errs.slice(0,8).map(function(e){
            return '· <b>'+esc(e.file)+'</b>：'+esc(e.reason); }).join('<br>')
        + '</div>'
      : '';
    if(!list.length){
      $('tplList').innerHTML = '<div class="hint">还没有可选站点。</div>';
      return;
    }
    var rows = list.map(function(t){
      var tags = [];
      var srcTxt = t.source==='image' ? '镜像自带'
                 : (t.source==='plugin' ? '已同步' : '本地');
      tags.push('<span class="tag">'+esc(srcTxt)+'</span>');
      if(t.need_browser) tags.push('<span class="tag">需要浏览器</span>');
      var btn = t.installed
        ? '<span class="tag">已加入</span>'
        : '<button class="b pri" onclick="installTpl(\''+esc(t.id)+'\')">加入</button>';
      return '<div class="row" style="align-items:center;gap:10px;'
        + 'border-top:1px solid var(--bd);padding-top:8px">'
        + '<div style="flex:1"><b>'+esc(t.name)+'</b> '+tags.join(' ')
        + '<div class="hint" style="margin:0">'+esc(t.id)+' · '
        + esc(t.homepage||'')+' · '+t.step_count+' 步</div>'
        + (t.note ? '<div class="hint" style="margin:0">'+esc(t.note)+'</div>' : '')
        + '</div><div>'+btn+'</div></div>';
    });
    $('tplList').innerHTML = rows.join('');
  }).catch(function(e){
    $('tplList').innerHTML = '<div class="hint" style="color:#c33">'
      + esc(e.message)+'</div>';
  });
}

function installTpl(id){
  api('POST','/api/templates/install',{id:id}).then(function(r){
    loadTemplates();
    refreshAll();
  }).catch(function(e){ show('syncMsg', e.message, 'err'); });
}

/* ---------------- 设置 ---------------- */
function loadSettings(){
  api('GET','/api/settings').then(function(d){
    SETTINGS = d;
    $('set_proxy').value = d.proxy || '';
    /* 代理模式：没填地址时默认显示「直连」 */
    setRadio('set_pmode', d.proxy_mode || (d.proxy ? 'custom' : 'direct'));
    swGlobalProxy();
    $('set_headless').value = d.headless ? '1' : '0';
    $('set_cdp').value = d.remote_cdp_url || '';
    /* 站点模板同步 */
    $('set_sync_on').checked = !!d.plugin_sync_enabled;
    /* 没配过就预填项目地址：初次安装不必去翻文档找来源 */
    $('set_sync_src').value = d.plugin_sync_source
      || (META.default_template_source || '');
    $('set_sync_time').value = d.plugin_sync_time || '03:00';
    $('set_sync_days').value = d.plugin_sync_interval_days || 1;
    loadSyncStatus();
    /* 可选站点列表：设置页打开时也要刷新（同步进来的模板要立刻可见） */
    loadTemplates();
    var n = d.notify || {};
    var mlabels = META.notify_mode_labels || {};
    $('set_nmode').innerHTML = (META.notify_modes||[]).map(function(m){
      var txt = mlabels[m] ? (m + '（' + mlabels[m] + '）') : m;
      return '<option value="'+esc(m)+'"'+(m===n.mode?' selected':'')+'>'+esc(txt)+'</option>';
    }).join('');
    $('set_aikey').value = '';
    /* 模型用下拉框。若已保存的值不在候选里（例如旧配置里的 deepseek-chat），
       临时插一个选项并选中，避免把用户的设置悄悄改掉。 */
    setModelSelect(d.ai && d.ai.model);
    $('set_aifail').value = (d.ai && d.ai.fail_threshold !== undefined
                             ? d.ai.fail_threshold : 2);
    var ai = (d.ai || {});
    /* 每连续失败几次调用一次 AI；0 = 不启用 */
    $('set_aiafter').value = (ai.ai_after_failures === undefined
                              ? 3 : ai.ai_after_failures);
    /* 每天调用次数的硬上限；0 = 不限制 */
    $('set_maxai').value = (ai.max_calls_per_day === undefined
                            ? 20 : ai.max_calls_per_day);
    /* 是否把失败页面片段一起发给 AI（默认关） */
    $('set_aiexcerpt').checked = !!ai.include_page_excerpt;
    calcAiCalls();
    $('set_wdurl').value = (d.webdav && d.webdav.url) || '';
    $('set_wduser').value = (d.webdav && d.webdav.username) || '';
    $('set_wdpw').value = '';
    $('set_wdkey').checked = !!(d.webdav && d.webdav.backup_key);
    loadAuthState();
    loadKeyInfo();
    renderChannels(n, d);
    /* 每日汇总：未设置时给个默认 09:00，后端默认值也是 9 点 */
    if($('set_digest')) $('set_digest').checked = !!n.digest_enabled;
    if($('set_digest_time')){
      $('set_digest_time').value = pad2(n.digest_hour == null ? 9 : n.digest_hour)
        + ':' + pad2(n.digest_minute == null ? 0 : n.digest_minute);
    }
  }).catch(function(e){ show('setMsg', e.message, 'err'); });
}

function loadAuthState(){
  api('GET','/api/auth/state').then(function(d){
    $('authState').textContent = d.required
      ? (d.configured
          ? '当前状态：已开启，且已设置口令'
          : '当前状态：已开启，但尚未设置口令（请到登录页设置）')
      : '当前状态：已关闭（本界面不再需要口令）';
  }).catch(function(e){ $('authState').textContent = '读取失败：'+e.message; });
}

function changePw(){
  var o = $('set_pwold').value, n = $('set_pwnew').value;
  if(!o || !n){ show('authMsg','请填写当前口令与新口令','err'); return; }
  if(n.length < 6){ show('authMsg','新口令至少 6 位','err'); return; }
  api('POST','/api/auth/password',{old:o,new:n}).then(function(d){
    show('authMsg', d.note || '口令已更改', 'ok');
    $('set_pwold').value=''; $('set_pwnew').value='';
  }).catch(function(e){ show('authMsg', e.message, 'err'); });
}

function disableAuth(){
  var p = prompt('关闭访问控制后，本界面不再需要口令。\n'
    + '请输入当前口令以确认：');
  if(p === null) return;
  api('POST','/api/auth/disable',{password:p}).then(function(d){
    show('authMsg', d.warning || '已关闭访问控制', 'ok');
    loadAuthState();
  }).catch(function(e){ show('authMsg', e.message, 'err'); });
}

function enableAuth(){
  api('POST','/api/auth/enable',{}).then(function(){
    show('authMsg','已开启访问控制', 'ok');
    loadAuthState();
  }).catch(function(e){ show('authMsg', e.message, 'err'); });
}

function loadKeyInfo(){
  api('GET','/api/key').then(function(d){
    $('keyInfo').textContent = '密钥来源：' + d.source
      + '\n指纹：' + (d.fingerprint || '(无)')
      + '（' + d.length + ' 字符，可用来核对两台机器是不是同一个密钥）'
      + '\n环境变量名：' + d.env_var;
  }).catch(function(e){ $('keyInfo').textContent = '读取失败：'+e.message; });
}

function backupKey(){
  if(!confirm('把加密密钥备份到 WebDAV？\n\n'
    + '密钥会存到你 WebDAV 的 autocheckin 目录下，'
    + '请确保那个目录只有你自己能访问。\n\n继续？')) return;
  show('keyMsg','备份中…','');
  api('POST','/api/key/backup',{}).then(function(d){
    show('keyMsg','已备份到：' + d.url + '\n' + (d.warning||''), 'ok');
  }).catch(function(e){ show('keyMsg', e.message, 'err'); });
}

function restoreKey(){
  if(!confirm('从 WebDAV 取回密钥？\n\n'
    + '如果本地密钥与云端不同，会用云端的覆盖本地密钥文件。\n'
    + '覆盖后需要重启容器才生效。\n\n继续？')) return;
  show('keyMsg','取回中…','');
  api('POST','/api/key/restore',{}).then(function(d){
    var msg = d.changed
      ? ('已用云端的密钥覆盖本地（指纹 ' + d.fingerprint + '）。\n'
         + '⚠️ 请重启容器后生效。')
      : ('云端密钥与本地一致（指纹 ' + d.fingerprint + '），无需改动。');
    show('keyMsg', msg, 'ok');
    loadKeyInfo();
  }).catch(function(e){ show('keyMsg', e.message, 'err'); });
}

function renderChannels(n, d){
  /* Server 酱已按要求移除，所以这里只有三个渠道 */
  var labels = {pushplus:'pushplus', wecom:'企业微信机器人',
                telegram:'Telegram'};
  var fields = {
    pushplus:  {key:'pushplus_token',  ph:'token', masked:n.pushplus_token_masked},
    wecom:     {key:'wecom_webhook',   ph:'https://qyapi.weixin.qq.com/…'},
    telegram:  {key:'tg_bot_token',    ph:'bot token，另填 chat id',
                masked:n.tg_bot_token_masked}
  };
  var has = n.has_credentials || {};
  var html = (META.channels||[]).map(function(ch){
    var f = fields[ch] || {key:ch, ph:''};
    var cp = (n.channel_proxy||{})[ch] || {};
    var on = (n.channels||[]).indexOf(ch) >= 0;
    return '<div class="card" style="margin-top:10px">'
      + '<label><input type="checkbox" data-ch="'+esc(ch)+'" class="chOn" '
      + (on?'checked':'')+'> 启用 '+esc(labels[ch]||ch)+' '
      + (has[ch]?'<span class="tag ok">已配置</span>':'<span class="tag">未配置</span>')
      + '</label>'
      + '<input class="chField" data-ch="'+esc(ch)+'" data-key="'+esc(f.key)+'" '
      + 'placeholder="'+esc(f.ph)+'" value="">'
      + (f.masked? '<div class="hint">已保存：'+esc(f.masked)+'（留空＝不修改）</div>' : '')
      + (ch==='telegram'? ('<input class="chField" data-ch="telegram" data-key="tg_chat_id" '
          + 'placeholder="chat id" value="">') : '')
      + '<label>该渠道走代理</label>'
      + '<select class="chProxy" data-ch="'+esc(ch)+'">'
      /* 默认直连：通知渠道多在公网，跟着签到的主代理反而容易失败 */
      + '<option value="direct"'+(cp.mode!=='custom'&&cp.mode!=='inherit'?' selected':'')+'>直连（默认）</option>'
      + '<option value="inherit"'+(cp.mode==='inherit'?' selected':'')+'>跟随主代理</option>'
      + '<option value="custom"'+(cp.mode==='custom'?' selected':'')+'>自定义</option>'
      + '</select>'
      + '<input class="chProxyUrl" data-ch="'+esc(ch)+'" placeholder="自定义代理地址" '
      + 'value="'+esc(cp.url||'')+'">'
      + '</div>';
  }).join('');
  $('chanBox').innerHTML = html;
}

function collectNotify(){
  var n = {mode: $('set_nmode').value,
           channels: [], channel_proxy: {}};
  document.querySelectorAll('.chOn').forEach(function(cb){
    if(cb.checked) n.channels.push(cb.dataset.ch);
  });
  document.querySelectorAll('.chField').forEach(function(inp){
    if(inp.value) n[inp.dataset.key] = inp.value;
  });
  document.querySelectorAll('.chProxy').forEach(function(sel){
    var ch = sel.dataset.ch;
    var url = '';
    document.querySelectorAll('.chProxyUrl').forEach(function(u){
      if(u.dataset.ch === ch) url = u.value.trim();
    });
    if(sel.value) n.channel_proxy[ch] = {mode: sel.value, url: url};
  });
  /* 每日汇总 */
  n.digest_enabled = !!($('set_digest') && $('set_digest').checked);
  n.digest_time = ($('set_digest_time') ? $('set_digest_time').value.trim() : '');
  return n;
}

function saveSettings(){
  var body = {
    proxy_mode: radioValue('set_pmode') || 'direct',
    proxy: $('set_proxy').value.trim(),
    headless: $('set_headless').value === '1',
    remote_cdp_url: $('set_cdp').value.trim(),
    plugin_sync_enabled: $('set_sync_on').checked,
    plugin_sync_source: $('set_sync_src').value.trim(),
    plugin_sync_time: $('set_sync_time').value.trim(),
    plugin_sync_interval_days: parseInt($('set_sync_days').value||'1', 10),
    notify: collectNotify(),
    webdav: {url: $('set_wdurl').value.trim(), username: $('set_wduser').value.trim(),
             password: $('set_wdpw').value, backup_key: $('set_wdkey').checked},
    ai: {api_key: $('set_aikey').value, model: $('set_model').value.trim(),
         ai_after_failures: parseInt($('set_aiafter').value||'0', 10),
         max_calls_per_day: parseInt($('set_maxai').value||'0', 10),
         fail_threshold: parseInt($('set_aifail').value||'0', 10),
         include_page_excerpt: $('set_aiexcerpt').checked}
  };
  api('POST','/api/settings', body).then(function(){
    show('setMsg','已保存。','ok'); refreshAll(); loadSettings();
  }).catch(function(e){ show('setMsg', e.message, 'err'); });
}

function testNotify(){
  show('notifyMsg','发送中…','');
  api('POST','/api/notify/test',{channel:''}).then(function(d){
    show('notifyMsg', JSON.stringify(d.report, null, 1), 'ok');
  }).catch(function(e){ show('notifyMsg', e.message, 'err'); });
}
function testAlert(){
  show('notifyMsg','发送中…','');
  api('POST','/api/notify/alert-test',{}).then(function(d){
    show('notifyMsg', '告警已发送：\n'+JSON.stringify(d.report, null, 1), 'ok');
  }).catch(function(e){ show('notifyMsg', e.message, 'err'); });
}
function testWd(){
  show('wdMsg','测试中…','');
  api('POST','/api/backup/test',{url:$('set_wdurl').value.trim(),
       username:$('set_wduser').value.trim(), password:$('set_wdpw').value})
    .then(function(d){ show('wdMsg','连接成功，备份目录：'+d.root,'ok'); })
    .catch(function(e){ show('wdMsg', e.message, 'err'); });
}
function backupNow(){
  show('wdMsg','备份中…','');
  api('POST','/api/backup/now',{}).then(function(d){
    show('wdMsg','备份成功：'+d.url+'\n（'+d.bytes+' 字节）','ok'); refreshAll();
  }).catch(function(e){ show('wdMsg', e.message, 'err'); });
}
function restoreNow(){
  if(!confirm('从云端恢复会覆盖当前配置，继续？')) return;
  show('wdMsg','恢复中…','');
  api('POST','/api/backup/restore',{}).then(function(d){
    show('wdMsg','已恢复 '+d.sites+' 个站点。','ok'); refreshAll(); loadSettings();
  }).catch(function(e){ show('wdMsg', e.message, 'err'); });
}

/* ---------------- 启动 ---------------- */
boot();
/* 站点列表的自动刷新：默认【关闭】，避免打断填写。
 *
 * 为什么默认关：站点数据只有两种情况会变
 *   1. 用户自己的操作（保存、启停、立即执行、删除）—— 这些分支本来就会刷新
 *   2. 后台调度器跑完签到 —— 会改"下次运行时间"与连败计数
 * 而调度是按用户设定的时刻跑的（一般凌晨），填表单时基本不会正好在跑。
 * 所以定时刷新收益小、干扰大（表单被重建、焦点丢失、填到一半没了）。
 *
 * 默认用 localStorage 记住，不占用后端配置。想开就在「设置 → 网络」里勾上。
 * 即使开着，编辑中的表单也会被跳过（见 refreshAll），不会冲掉内容。
 */
var AUTO_REFRESH_KEY = 'ac_auto_refresh_ms';
var AUTO_REFRESH_TIMER = null;

function autoRefreshMs(){
  var v = parseInt(localStorage.getItem(AUTO_REFRESH_KEY) || '0', 10);
  return isNaN(v) ? 0 : v;
}

function setAutoRefresh(ms){
  localStorage.setItem(AUTO_REFRESH_KEY, String(ms || 0));
  applyAutoRefresh();
}

function applyAutoRefresh(){
  if(AUTO_REFRESH_TIMER){ clearInterval(AUTO_REFRESH_TIMER); AUTO_REFRESH_TIMER = null; }
  var ms = autoRefreshMs();
  if(ms <= 0) return;                    /* 0 = 关闭 */
  AUTO_REFRESH_TIMER = setInterval(function(){
    if($('mainBody').className === '') refreshAll();
  }, ms);
}
</script>
</body>
</html>
"""
