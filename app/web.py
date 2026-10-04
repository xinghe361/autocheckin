"""HTTP 层与网页界面（只用标准库，不额外引入框架）。

路由全部是薄转发，真正的逻辑在 service.py，所以业务规则可以脱离 HTTP 单测。
"""

from __future__ import annotations

import json
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import auth as AUTH
from . import recorder as RC

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
        # 每次请求前由 HTTP 层设置，供认证相关端点读取
        self._current_token = ''
        self._current_source = ''
        # 配套浏览器脚本带来的令牌（请求头 X-AC-Token）
        self._script_token = ''
        # 本次请求是否用脚本令牌通过认证
        self._via_script = False
        # 本次请求要下发的会话令牌（登录成功时设置，由 HTTP 层写成 Cookie）
        self._new_token = ''
        # 本次请求是否要清除会话 Cookie（登出）
        self._do_logout = False
        self._register()

    def _public_origin(self, q: Optional[dict] = None) -> str:
        """推断容器对外的地址，用于注入到油猴脚本里。

        优先用请求里带的 host（用户在浏览器里访问的那个地址最可靠）；
        没带就退回监听地址与端口。
        """
        q = q or {}
        host = ''
        for key in ('host', 'origin'):
            v = q.get(key)
            if isinstance(v, list):
                v = v[0] if v else ''
            if v:
                host = str(v)
                break
        if host:
            if host.startswith('http://') or host.startswith('https://'):
                return host.rstrip('/')
            return 'http://' + host.strip('/')
        import os
        port = os.environ.get('PORT', '28999')
        return 'http://127.0.0.1:%s' % port

    def _require_auth(self, method: str, path: str) -> None:
        """未登录时拒绝访问受保护端点。

        两条通过路径：
          1. 网页会话令牌（你自己登录）
          2. 配套浏览器脚本的令牌（X-AC-Token）—— 但它**只能访问极少数端点**，
             见 SCRIPT_ALLOWED_PATHS。这样即使脚本令牌泄露，最坏情况也只是
             "有人能覆盖站点 Cookie"，而不是容器被整个接管。
        """
        if not self.auth.required():
            return
        if path in AUTH.PUBLIC_PATHS:
            return
        if self.auth.needs_setup():
            # 还没设口令：只允许去设置，其它一律拒绝
            raise ApiError('尚未设置访问口令，请先完成初始化', 403)
        if self.auth.is_valid(self._current_token):
            return
        # 脚本令牌
        if self._script_token and self.auth.api_token_valid(self._script_token):
            if (method.upper(), path) in AUTH.SCRIPT_ALLOWED_PATHS:
                self._via_script = True
                return
            raise ApiError('该令牌仅允许写入站点 Cookie，不能访问此接口', 403)
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
            # 只在"要求认证但还没设口令"时允许设置，避免被人抢先设一个
            if not self.auth.needs_setup():
                raise ApiError('口令已设置，不能重复初始化', 403)
            pw = str(body.get('password') or '')
            self.auth.set_password(pw)
            token = self.auth.login(pw, source='setup')
            if not token:
                raise ApiError('口令设置后登录失败，请重试', 500)
            self._new_token = token
            return {'ok': True}

        @route('POST', '/api/auth/login')
        def _auth_login(q, body):
            source = (self._current_source or 'unknown')
            try:
                token = self.auth.login(str(body.get('password') or ''),
                                        source=source)
            except PermissionError as e:
                raise ApiError(str(e), 429) from e
            if not token:
                raise ApiError('口令不正确', 401)
            self._new_token = token
            return {'ok': True}

        @route('POST', '/api/auth/logout')
        def _auth_logout(q, body):
            self.auth.logout(self._current_token or '')
            self._do_logout = True
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
            sid = q.get('id', [''])[0]
            site = self.service.get_site(sid)
            if not site:
                raise ApiError('站点不存在：%s' % sid, 404)
            # 不返回任何凭据明文
            return {'site': site.to_dict(include_secrets=False)}

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

        # --- 配套浏览器脚本：配对与令牌 ---
        @route('POST', '/api/pair/new')
        def _pair_new(q, body):
            """生成一次性配对码（需要已登录，所以不会成为免认证后门）。"""
            code = self.auth.issue_pair_code()
            return {
                'code': code,
                'ttl_seconds': AUTH.PAIR_TTL_SECONDS,
                'pending': self.auth.pending_pair_codes(),
            }

        # 公开：脚本拿配对码来换令牌。安全性由"短码本身"保证：
        # 一次性、10 分钟过期、只存哈希、必须由已登录用户先生成。
        @route('POST', '/api/pair/redeem')
        def _pair_redeem(q, body):
            code = str(body.get('code') or '')
            token = self.auth.redeem_pair_code(code)
            if not token:
                raise ApiError('配对码无效或已过期，请在网页上重新生成', 401)
            return {'token': token}

        @route('GET', '/api/script/status')
        def _script_status(q, body):
            return {
                'authorized': self.auth.api_token_count(),
                'cookie_api': '/api/site/cookie',
            }

        @route('POST', '/api/script/revoke')
        def _script_revoke(q, body):
            """吊销全部脚本令牌（不影响你自己的登录）。"""
            n = self.auth.revoke_api_tokens()
            return {'ok': True, 'revoked': n}

        # 配套油猴脚本本体：由容器直接提供，省得用户去找/复制。
        # 用文本接口返回，浏览器访问时会触发 Tampermonkey 的安装询问。
        @route('GET', '/autocheckin-cookie.user.js')
        def _userscript(q, body):
            # 用请求里带的 host 作为容器地址写进脚本（浏览器访问时的地址最准）
            origin = self._public_origin(q)
            return Raw(build_userscript(origin),
                       'text/javascript; charset=utf-8')

        # --- 立即执行 ---
        @route('POST', '/api/run')
        def _run(q, body):
            return self.service.run_now(body.get('id') or None)

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
        @route('POST', '/api/notify/test')
        def _notify_test(q, body):
            return self.service.test_notify(str(body.get('channel') or ''))

        @route('POST', '/api/notify/alert-test')
        def _alert_test(q, body):
            return self.service.test_alert()

        # --- 站点模板与元数据（供界面下拉）---
        @route('GET', '/api/meta')
        def _meta(q, body):
            from .models import (NOTIFY_MODES, PROXY_INHERIT, PROXY_DIRECT,
                                 PROXY_CUSTOM)
            from .notify import ALL_CHANNELS
            from .templates import BUILTIN
            return {
                'version': self.version,
                'templates': [{'id': t['id'], 'name': t['name'],
                               'need_browser': t['need_browser'],
                               'verify_ssl': t.get('verify_ssl', True),
                               'note': t.get('note', '')} for t in BUILTIN],
                'notify_modes': list(NOTIFY_MODES),
                'channels': list(ALL_CHANNELS),
                'proxy_modes': [PROXY_INHERIT, PROXY_DIRECT, PROXY_CUSTOM],
                'actions': ['goto', 'click', 'fill', 'wait', 'wait_for',
                            'assert_text', 'extract'],
            }

    # ------------------------------------------------------------------
    def _apply_settings(self, cfg, body: Dict[str, Any]) -> None:
        """把界面提交的设置写进配置；空字符串表示"不修改"凭据。"""
        if 'proxy' in body:
            cfg.proxy = str(body.get('proxy') or '')
        if 'headless' in body:
            cfg.headless = bool(body['headless'])
        if 'remote_cdp_url' in body:
            cfg.remote_cdp_url = str(body.get('remote_cdp_url') or '')
        if 'browser_path' in body:
            cfg.browser_path = str(body.get('browser_path') or '')

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
            if 'max_calls_per_day' in a and str(a['max_calls_per_day']) != '':
                ac.max_calls_per_day = int(a['max_calls_per_day'])
            if 'auto_apply' in a:
                ac.auto_apply = bool(a['auto_apply'])
            if 'fail_threshold' in a and str(a['fail_threshold']) != '':
                ac.fail_threshold = max(1, int(a['fail_threshold']))
            if 'auto_disable' in a:
                ac.auto_disable = bool(a['auto_disable'])
            if a.get('api_key'):
                ac.api_key_enc = self.service.box.encrypt(str(a['api_key']))

    # ------------------------------------------------------------------
    def dispatch(self, method: str, path: str,
                 query: Dict[str, list], body: Optional[dict] = None,
                 token: str = '', source: str = '', script_token: str = ''):
        """路由分发。返回 (状态码, 内容类型, 字节) 或 (状态码, dict)。

        token/source 由 HTTP 层从 Cookie 与请求头解析后传入。
        script_token 是配套浏览器脚本的令牌（请求头 X-AC-Token）。
        """
        self._current_token = token or ''
        self._current_source = source or ''
        self._script_token = script_token or ''
        self._via_script = False
        self._new_token = ''
        self._do_logout = False

        fn = self.routes.get((method.upper(), path))
        if fn is None:
            alt = path.rstrip('/') or '/'
            fn = self.routes.get((method.upper(), alt))
            if fn is not None:
                path = alt
        if fn is None:
            raise ApiError('未知接口：%s %s' % (method, path), 404)

        # 访问控制在这里统一做，避免每个端点各自实现时漏掉
        self._require_auth(method.upper(), path)

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

        if isinstance(out, Raw):
            return 200, out.content_type, out.data

        return 200, 'application/json; charset=utf-8', out


class Raw:
    """已是最终字节的响应（用于返回注入脚本等）。"""

    def __init__(self, text: str, content_type: str = 'text/plain; charset=utf-8'):
        self.data = text.encode('utf-8')
        self.content_type = content_type


# ---------------------------------------------------------------- 配套脚本

# 支持的站点：站点标识 -> [域名匹配]
SCRIPT_SITE_MATCHES = {
    'v2ex': ['www.v2ex.com', 'v2ex.com'],
    'nodeseek': ['www.nodeseek.com', 'nodeseek.com'],
    'chiphell': ['www.chiphell.com', 'chiphell.com'],
}


def _userscript_template(origin: str) -> str:
    matches = []
    for domains in SCRIPT_SITE_MATCHES.values():
        for d in domains:
            matches.append('// @match        https://%s/*' % d)
    match_block = '\n'.join(sorted(set(matches)))
    connects = '\n'.join(
        '// @connect      %s' % d
        for d in sorted({d for ds in SCRIPT_SITE_MATCHES.values() for d in ds}))

    return r'''// ==UserScript==
// @name         自动签到 · Cookie 助手
// @namespace    autocheckin
// @version      1.0.0
// @description  把当前网站的登录 Cookie 一键送到你的自动签到容器，省去来回切换页面
// @author       autocheckin
__MATCHES__
// @connect      __HOST__
__CONNECTS__
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_deleteValue
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

/* eslint-disable */
(function () {
  'use strict';

  // 容器地址由容器在提供本脚本时注入，不用你手填。
  var CONTAINER = '__ORIGIN__';
  var SITES = __SITES__;

  var TOKEN_KEY = 'ac_token';

  // ------------------------------------------------------------ 工具
  function siteIdFor(hostname) {
    var h = (hostname || '').toLowerCase();
    for (var i = 0; i < SITES.length; i++) {
      var s = SITES[i];
      for (var j = 0; j < s.domains.length; j++) {
        var d = s.domains[j];
        if (h === d || h.slice(-(d.length + 1)) === '.' + d) return s.id;
      }
    }
    return '';
  }

  function el(tag, style, text) {
    var e = document.createElement(tag);
    if (style) e.setAttribute('style', style);
    if (text != null) e.textContent = text;
    return e;
  }

  function setStatus(text, kind) {
    var box = document.getElementById('ac-ck-status');
    if (!box) return;
    var colors = { ok: '#1e6b3a', err: '#a03a30', info: '#5a636e' };
    box.style.color = colors[kind] || colors.info;
    box.textContent = text || '';
  }

  function api(path, payload, cb) {
    var token = GM_getValue(TOKEN_KEY, '');
    if (!token) {
      cb({ error: '还没有配对。请先在容器网页上点「自动导入」生成配对码，再填到上面的框里。' });
      return;
    }
    GM_xmlhttpRequest({
      method: 'POST',
      url: CONTAINER + path,
      headers: { 'Content-Type': 'application/json', 'X-AC-Token': token },
      data: JSON.stringify(payload || {}),
      onload: function (r) {
        var data = {};
        try { data = JSON.parse(r.responseText || '{}'); } catch (e) { data = {}; }
        if (r.status >= 200 && r.status < 300) { cb(data); return; }
        if (r.status === 401 || r.status === 403) {
          cb({ error: data.error || '配对已失效，请重新配对' });
          return;
        }
        cb({ error: data.error || ('请求失败（HTTP ' + r.status + '）') });
      },
      onerror: function () {
        cb({ error: '连不上容器：' + CONTAINER + '（确认地址、端口和网络可达）' });
      },
      ontimeout: function () { cb({ error: '请求超时' }); }
    });
  }

  // ------------------------------------------------------------ 界面
  function buildPanel() {
    var wrap = el('div',
      'position:fixed;right:16px;bottom:16px;z-index:2147483647;' +
      'font:13px/1.6 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;');

    var btn = el('button', null, '🍪 Cookie 助手');
    btn.id = 'ac-ck-open';
    btn.setAttribute('style',
      'padding:8px 14px;border:none;border-radius:20px;cursor:pointer;' +
      'background:#2d6adc;color:#fff;box-shadow:0 3px 12px rgba(0,0,0,.28);' +
      'font-size:13px;');
    btn.onclick = function () { panel.classList.toggle('ac-hide'); };
    wrap.appendChild(btn);

    var panel = el('div');
    panel.id = 'ac-ck-panel';
    panel.className = 'ac-hide';
    panel.setAttribute('style',
      'display:none;width:430px;max-width:92vw;background:#fff;color:#242a33;' +
      'border:1px solid #dde3ec;border-radius:10px;padding:14px 16px;' +
      'box-shadow:0 10px 34px rgba(0,0,0,.22);margin-bottom:10px;');

    panel.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:center;' +
      'margin-bottom:8px">' +
        '<b style="font-size:14px">Cookie 助手</b>' +
        '<span id="ac-ck-x" style="cursor:pointer;color:#8b95a3;font-size:18px;' +
        'line-height:1">&times;</span>' +
      '</div>' +
      '<div id="ac-ck-site" style="color:#5a636e;margin-bottom:10px"></div>' +
      '<div id="ac-ck-body"></div>' +
      '<div id="ac-ck-status" style="margin-top:8px;min-height:18px"></div>' +
      '<div style="margin-top:10px;border-top:1px solid #eef1f6;padding-top:8px;' +
      'font-size:11.5px;color:#8b95a3">容器：' + CONTAINER + '</div>';

    wrap.appendChild(panel);
    document.body.appendChild(wrap);

    panel.querySelector('#ac-ck-x').onclick = function () {
      panel.style.display = 'none';
    };
    return panel;
  }

  function renderReady(panel, siteId, names) {
    var body = panel.querySelector('#ac-ck-body');
    body.innerHTML =
      '<div style="background:#f7f9fc;border:1px solid #e4e9f2;border-radius:6px;' +
      'padding:10px;margin-bottom:10px">' +
        '<div style="margin-bottom:6px">页面可见的 Cookie：<b>' + names.length +
        '</b> 个</div>' +
        (names.length
          ? '<div style="font-size:11.5px;color:#5a636e;word-break:break-all">' +
            names.join('、') + '</div>'
          : '<div style="font-size:11.5px;color:#a03a30">' +
            '一个都读不到，说明关键登录字段是 HttpOnly（脚本读不到值），' +
            '请按下面的办法复制。</div>') +
      '</div>' +
      '<div style="margin-bottom:6px;font-weight:600">拿到完整 Cookie 的办法</div>' +
      '<ol style="margin:0 0 8px 18px;padding:0;font-size:12.5px;color:#3a424d">' +
        '<li>按 <b>F12</b> 打开开发者工具，切到 <b>Network</b></li>' +
        '<li>按 <b>F5</b> 刷新页面</li>' +
        '<li>点列表里<b>第一条请求</b></li>' +
        '<li>右键 → <b>Copy</b> → <b>Copy as cURL</b></li>' +
        '<li>粘到下面框里 → 点「发送到容器」</li>' +
      '</ol>' +
      '<div style="font-size:11.5px;color:#8b95a3;margin-bottom:10px">' +
        '为什么不用控制台：<code>document.cookie</code> 读不到 HttpOnly 的' +
        '登录字段，用 DevTools 复制才完整。</div>' +
      '<textarea id="ac-ck-input" spellcheck="false" ' +
      'style="width:100%;min-height:88px;box-sizing:border-box;padding:8px;' +
      'border:1px solid #dde3ec;border-radius:6px;font:12px/1.5 Consolas,monospace" ' +
      'placeholder="把 Copy as cURL 的整段，或 a=1; b=2 这样的 Cookie 串粘到这里"></textarea>' +
      '<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">' +
        '<button id="ac-ck-send" style="padding:6px 14px;border:none;' +
        'border-radius:5px;background:#2d6adc;color:#fff;cursor:pointer">' +
        '发送到容器</button>' +
        '<button id="ac-ck-clear" style="padding:6px 12px;border:1px solid #dde3ec;' +
        'border-radius:5px;background:#fff;cursor:pointer">清除本脚本配对</button>' +
      '</div>';

    body.querySelector('#ac-ck-send').onclick = function () {
      var v = body.querySelector('#ac-ck-input').value;
      if (!v || !v.trim()) { setStatus('请先粘贴 Cookie 内容', 'err'); return; }
      setStatus('正在发送…', 'info');
      api('/api/site/cookie', { id: siteId, cookie: v }, function (d) {
        if (d.error) { setStatus('失败：' + d.error, 'err'); return; }
        setStatus('已保存 ' + d.count + ' 项（识别为：' + d.format + '）。' +
                  '可以回容器页面点「检查是否有效」。', 'ok');
        body.querySelector('#ac-ck-input').value = '';
      });
    };

    body.querySelector('#ac-ck-clear').onclick = function () {
      if (!confirm('清除本脚本保存的配对令牌？（容器里的站点 Cookie 不会被删）')) return;
      GM_deleteValue(TOKEN_KEY);
      renderPairing(panel, siteId);
    };
  }

  function renderPairing(panel, siteId) {
    var body = panel.querySelector('#ac-ck-body');
    body.innerHTML =
      '<div style="margin-bottom:8px">本脚本还没有和容器配对。' +
      '请在容器的「站点」页点该站点的「自动导入」，把生成的配对码填到这里：</div>' +
      '<input id="ac-ck-code" placeholder="配对码，例如 K7M2-9QXF" ' +
      'style="width:100%;box-sizing:border-box;padding:8px;border:1px solid #dde3ec;' +
      'border-radius:6px;font:13px Consolas,monospace;text-transform:uppercase">' +
      '<div style="display:flex;gap:8px;margin-top:10px">' +
        '<button id="ac-ck-pair" style="padding:6px 14px;border:none;' +
        'border-radius:5px;background:#2d6adc;color:#fff;cursor:pointer">配对</button>' +
      '</div>';
    body.querySelector('#ac-ck-pair').onclick = function () {
      var code = (body.querySelector('#ac-ck-code').value || '').trim();
      if (!code) { setStatus('请填配对码', 'err'); return; }
      setStatus('正在配对…', 'info');
      GM_xmlhttpRequest({
        method: 'POST',
        url: CONTAINER + '/api/pair/redeem',
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify({ code: code }),
        onload: function (r) {
          var d = {};
          try { d = JSON.parse(r.responseText || '{}'); } catch (e) { d = {}; }
          if (r.status === 200 && d.token) {
            GM_setValue(TOKEN_KEY, d.token);
            setStatus('配对成功。', 'ok');
            renderReady(panel, siteId, readCookieNames());
          } else {
            setStatus('配对失败：' + (d.error || ('HTTP ' + r.status)), 'err');
          }
        },
        onerror: function () { setStatus('连不上容器：' + CONTAINER, 'err'); }
      });
    };
    body.querySelector('#ac-ck-code').focus();
  }

  function readCookieNames() {
    var out = [];
    var raw = document.cookie || '';
    raw.split(';').forEach(function (p) {
      p = p.trim();
      if (!p || p.indexOf('=') < 0) return;
      var k = p.split('=')[0].trim();
      if (k) out.push(k);
    });
    return out;
  }

  // ------------------------------------------------------------ 入口
  function main() {
    var siteId = siteIdFor(location.hostname);
    if (!siteId) return;
    if (document.getElementById('ac-ck-open')) return;

    var panel = buildPanel();
    var info = panel.querySelector('#ac-ck-site');
    var found = null;
    for (var i = 0; i < SITES.length; i++) {
      if (SITES[i].id === siteId) found = SITES[i];
    }
    info.textContent = '当前站点：' + (found ? found.name : siteId) +
                       '（' + location.hostname + '）';

    if (GM_getValue(TOKEN_KEY, '')) {
      renderReady(panel, siteId, readCookieNames());
    } else {
      renderPairing(panel, siteId);
    }

    GM_registerMenuCommand('打开 Cookie 助手', function () {
      panel.style.display = 'block';
    });
    GM_registerMenuCommand('清除配对令牌', function () {
      GM_deleteValue(TOKEN_KEY);
      location.reload();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', main);
  } else {
    main();
  }
})();
'''.replace('__ORIGIN__', origin) \
   .replace('__MATCHES__', match_block) \
   .replace('__CONNECTS__', connects) \
   .replace('__HOST__', _host_of(origin)) \
   .replace('__SITES__', json.dumps(
       [{'id': k, 'name': _site_display_name(k), 'domains': v}
        for k, v in SCRIPT_SITE_MATCHES.items()], ensure_ascii=False))


def _host_of(origin: str) -> str:
    from urllib.parse import urlsplit
    try:
        return urlsplit(origin or '').hostname or 'localhost'
    except Exception:                                           # noqa: BLE001
        return 'localhost'


def _site_display_name(site_id: str) -> str:
    from .templates import template_by_id
    tpl = template_by_id(site_id)
    return tpl.get('name') or site_id


def build_userscript(origin: str) -> str:
    """生成配套油猴脚本（容器地址已注入）。"""
    return _userscript_template(origin or 'http://127.0.0.1:28999')


# ------------------------------------------------------------------ HTTP 服务器

def make_handler(webapp: WebApp, page_html: str):
    """构造请求处理器。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AutoCheckin'
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):      # 静音：避免刷屏
            pass

        # -- 工具 --
        def _send(self, status: int, content_type: str, payload: bytes,
                  extra_headers=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            # 基础安全响应头
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            for k, v in (extra_headers or {}).items():
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

        def _script_token(self) -> str:
            """配套浏览器脚本带来的令牌（跨站请求，带不上会话 Cookie）。"""
            return (self.headers.get('X-AC-Token') or '').strip()

        def _send_json(self, status: int, obj: Any, extra_headers=None):
            payload = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self._send(status, 'application/json; charset=utf-8', payload,
                       extra_headers)

        def _read_body(self) -> dict:
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise ApiError('请求体过大', 413)
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode('utf-8'))
            except ValueError as e:
                raise ApiError('请求体不是合法 JSON：%s' % e, 400) from e
            return data if isinstance(data, dict) else {'value': data}

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
                self._send(200, 'text/html; charset=utf-8',
                           page_html.encode('utf-8'))
                return

            token = self._session_token()
            extra = None
            try:
                body = self._read_body() if method in ('POST', 'PUT') else {}
                status, ctype, out = webapp.dispatch(method, path, query, body,
                                                    token=token,
                                                    source=self._client_source(),
                                                    script_token=self._script_token())
                # 登录成功 → 下发会话 Cookie；登出 → 清除 Cookie
                if webapp._new_token:
                    extra = {'Set-Cookie': AUTH.make_cookie(webapp._new_token)}
                elif webapp._do_logout:
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
.kvrow{display:flex;gap:6px;margin-bottom:6px}
.kvrow input:first-child{flex:0 0 34%}
.kvrow input{flex:1}
.paths{font-size:12px;color:var(--mut);margin-top:4px}
</style>
</head>
<body>
<header>
  <h1>自动签到</h1>
  <span class="mut" id="ver"></span>
  <nav id="mainNav">
    <button data-tab="sites" class="on">站点</button>
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
    <div class="hint">也可以直接从内置模板复制一份再改。内置模板：
      <span id="tplList" class="mono"></span></div>
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
  <div class="card">
    <h2>网络</h2>
    <label>全局代理（所有站点与浏览器统一走它）</label>
    <input id="set_proxy" placeholder="http://10.0.0.1:7890（留空＝直连）">
    <div class="hint">代理已做分流时，填代理地址即可；不填则直连。</div>
    <div class="row">
      <div><label>浏览器无头模式</label>
        <select id="set_headless"><option value="1">开启（推荐）</option>
        <option value="0">关闭（可看画面）</option></select></div>
      <div><label>连接已有 Chrome（CDP，可选）</label>
        <input id="set_cdp" placeholder="http://browser:9222"></div>
    </div>
  </div>

  <div class="card">
    <h2>通知</h2>
    <div class="row">
      <div><label>通知策略</label><select id="set_nmode"></select></div>
      <div><label>通知走代理</label><select id="set_nproxy"></select></div>
    </div>
    <label>通知代理地址（"自定义"时使用）</label>
    <input id="set_nproxy_url" placeholder="http://10.0.0.1:7890">
    <div class="hint">每个渠道还能单独覆盖走不走代理（见下）。</div>
    <div id="chanBox"></div>
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
      <div><label>模型</label><input id="set_model" placeholder="deepseek-chat"></div>
      <div><label>每天最多分析次数</label><input id="set_maxai" type="number" value="20"></div>
    </div>
    <div class="row">
      <div><label>分析连续失败几次后停止该站点签到</label>
        <input id="set_aifail" type="number" value="2"></div>
      <div><label>分析也失败时自动停用该站点</label>
        <select id="set_aiauto">
          <option value="1">开启（推荐）</option>
          <option value="0">关闭</option>
        </select></div>
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
  </div>
  <div id="setMsg" class="msg"></div>
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
  ['sites','add','settings'].forEach(function(t){
    $('tab-'+t).className = (t === name) ? '' : 'hide';
  });
  if(name === 'settings') loadSettings();
}
document.querySelectorAll('#mainNav button[data-tab]').forEach(function(b){
  b.onclick = function(){ switchTab(b.dataset.tab); };
});

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

function cookieTag(s){
  if(!s.has_cookie) return '<span class="tag err">未配置登录</span>';
  if(s.cookie_status===2) return '<span class="tag err">登录已失效</span>';
  if(s.cookie_status===1) return '<span class="tag ok">登录有效</span>';
  return '<span class="tag ok">已配置登录</span>';
}

function loadSites(){
  api('GET','/api/sites').then(function(d){
    var cards = (d.sites||[]).map(function(s){
      var open = (OPEN_SITE === s.id);
      var state = s.enabled? '<span class="tag ok">启用</span>'
                           : '<span class="tag">停用</span>';
      var sch = s.mode==='success_based'
        ? ('上次成功后 ' + s.retry_interval_minutes + ' 分钟')
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

      var fail = '';
      if(s.consecutive_failures)
        fail += ' <span class="tag err">连败 '+s.consecutive_failures+'</span>';
      if(s.disabled_reason)
        fail += '<div class="hint" style="color:#c0392b">'+esc(s.disabled_reason)+'</div>';
      if(s.ai_consecutive_failures)
        fail += '<div class="hint">AI 分析失败 '+s.ai_consecutive_failures+' 次'
              + (s.last_ai_error? ('：'+esc(s.last_ai_error)) : '') + '</div>';

      var html = '<div class="site'+(open?' open':'')+'" id="site-'+esc(s.id)+'">'
        + '<div class="hd"><div>'
        +   '<div class="nm">'+esc(s.name)+' '+cookieTag(s)+'</div>'
        +   '<div class="meta">'+esc(s.id)+badges+'</div>'
        +   '<div class="meta">'+state+' · '+esc(sch)
        +     ' · 下次 '+esc(fmtTime(s.next_run_at))+fail+'</div>'
        + '</div><div class="acts">'
        +   '<button class="b" onclick="runSite(\''+esc(s.id)+'\')">立即</button>'
        +   '<button class="b" onclick="toggleSite(\''+esc(s.id)+'\','+(s.enabled?0:1)+')">'
        +     (s.enabled?'停用':'启用')+'</button>'
        +   '<button class="b'+(open?' pri':'')+'" onclick="toggleEdit(\''+esc(s.id)+'\')">'
        +     (open?'收起':'编辑')+'</button>'
        +   '<button class="b danger" onclick="delSite(\''+esc(s.id)+'\')">删除</button>'
        + '</div></div>'
        + '<div id="form-'+esc(s.id)+'">'+(open? '<div class="hint">加载表单…</div>':'')
        + '</div></div>';
      return html;
    }).join('');
    $('siteRows').innerHTML = cards || '<div class="mut">还没有站点</div>';
    if(OPEN_SITE) renderSiteForm(OPEN_SITE);
  }).catch(function(e){
    $('siteRows').innerHTML = '<div class="err">'+esc(e.message)+'</div>';
  });
}

/* 展开 / 收起某个站点的编辑表单（其他站点自动下推） */
function toggleEdit(id){
  OPEN_SITE = (OPEN_SITE === id) ? '' : id;
  loadSites();
}

function _sw(id, label, on, onchange){
  return '<label class="sw'+(on?'':' off')+'" for="'+id+'">'
    + '<input type="checkbox" id="'+id+'"'+(on?' checked':'')
    + ' onchange="'+onchange+'">'
    + '<span class="track"></span><span class="lb">'+esc(label)+'</span></label>';
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
      flushCookieMsg(id);
    }).catch(function(){
      box.innerHTML = siteFormHtml(s, {has_cookie:s.has_cookie,names:[]});
      flushCookieMsg(id);
    });
  }).catch(function(e){
    box.innerHTML = '<div class="err">读取站点失败：'+esc(e.message)+'</div>';
  });
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

  /* ---- 登录状态区 ---- */
  var ckStatus;
  if(!c.has_cookie){
    ckStatus = '<span class="tag err">未配置</span>'
      + '<span class="hint" style="margin:0">该站点签到需要登录，请按下面的说明粘贴 Cookie</span>';
  }else{
    var when = c.updated_at? fmtTime(c.updated_at) : '未知时间';
    var st = c.status===1? '<span class="tag ok">有效</span>'
           : c.status===2? '<span class="tag err">已失效</span>'
           : '<span class="tag">未检查</span>';
    ckStatus = st + '<span class="tag ok">已配置 '+c.count+' 项</span>'
      + '<span class="hint" style="margin:0">更新于 '+esc(when)
      + '（来源：'+esc(c.source||'未知')+'）</span>';
    if(c.error) ckStatus += '<div class="hint" style="color:#c0392b">'+esc(c.error)+'</div>';
  }

  var cookieNames = (c.names&&c.names.length)
    ? '<div class="hint">已保存字段：'+esc(c.names.join('、'))+'</div>' : '';

  /* ---- 表单 ---- */
  return '<form onsubmit="return false">'

  + '<div class="fsec"><h4>基本</h4>'
  +   '<div class="fgrid">'
  +     '<div><label>站点名称</label><input id="'+p+'name" value="'+esc(s.name)+'"></div>'
  +     '<div><label>主页地址</label><input id="'+p+'home" value="'+esc(s.homepage)+'"></div>'
  +   '</div>'
  +   _sw(p+'enabled','启用签到', s.enabled, "swSync('"+id+"')")
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

  + '<div class="fsec"><h4>签到时间</h4>'
  +   '<div class="radios">'
  +     '<label><input type="radio" name="'+p+'mode" value="daily"'
  +       (isDaily?' checked':'')+' onchange="swSync(\''+id+'\')">每天固定时刻</label>'
  +     '<label><input type="radio" name="'+p+'mode" value="success_based"'
  +       (isDaily?'':' checked')+' onchange="swSync(\''+id+'\')">上次成功后间隔</label>'
  +   '</div>'
  +   '<div class="fgrid" style="margin-top:6px">'
  +     '<div id="'+p+'dailyBox"><label>时刻（24 小时制）</label>'
  +       '<span class="inline"><input type="number" id="'+p+'hour" min="0" max="23" value="'
  +         s.daily_hour+'"> : <input type="number" id="'+p+'min" min="0" max="59" value="'
  +         String(s.daily_minute).padStart(2,'0')+'"></span></div>'
  +     '<div><label>随机延迟上限（分钟）</label>'
  +       '<span class="inline"><input type="number" id="'+p+'jitter" min="0" max="720" value="'
  +         Math.round((s.jitter_seconds||0)/60)+'"></span>'
  +       '<div class="hint">在设定时刻前后随机浮动，避免每天同一秒请求</div></div>'
  +   '</div>'
  +   _sw(p+'jitterOn','启用随机延迟', s.jitter_enabled, "swSync('"+id+"')")
  + '</div>'

  + '<div class="fsec"><h4>失败重试</h4>'
  +   _sw(p+'retryOn','失败后重试', s.retry_enabled, "swSync('"+id+"')")
  +   '<div class="fgrid" style="margin-top:6px">'
  +     '<div><label>重试次数</label><input type="number" id="'+p+'rc" min="0" max="10" value="'
  +       s.retry_count+'"></div>'
  +     '<div><label>重试间隔（分钟）</label><input type="number" id="'+p+'ri" min="1" max="1440" value="'
  +       s.retry_interval_minutes+'"></div>'
  +   '</div>'
  + '</div>'

  + '<div class="fsec"><h4>AI 分析</h4>'
  +   _sw(p+'aiOn','连续失败后让 AI 分析原因', s.ai_enabled, "swSync('"+id+"')")
  +   '<div class="fgrid" style="margin-top:6px">'
  +     '<div><label>连续失败几次后分析</label>'
  +       '<input type="number" id="'+p+'aiN" min="1" max="20" value="'
  +       s.ai_after_failures+'"></div>'
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
  +       '<button type="button" class="b" onclick="openCookieHelper(\''+id+'\')">'
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
  +       '<b>手动粘贴怎么拿：</b>用你自己的浏览器打开 '
  +       '<a href="'+esc(s.homepage||'#')+'" target="_blank" rel="noopener">'
  +       esc(s.homepage||'该网站')+'</a> 并登录 → 按 <b>F12</b> → '
  +       '<b>Network</b> 标签 → 按 <b>F5</b> 刷新 → 点列表里第一条请求 → '
  +       '<b>右键 → Copy → Copy as cURL</b> → 把整段粘到上面的框里。<br>'
  +       '不用自己整理格式，粘进来会自动提取。'
  +       '注意 <b>document.cookie 拿不到 HttpOnly 的登录字段</b>，所以别用控制台。'
  +     '</div>'
  +     '<div class="formfoot">'
  +       '<button type="button" class="b pri" onclick="saveCookie(\''+id+'\')">'
  +         '保存 Cookie</button>'
  +       '<button type="button" class="b" onclick="checkCookie(\''+id+'\')">'
  +         '检查是否有效</button>'
  +       '<button type="button" class="b danger" onclick="clearCookie(\''+id+'\')">'
  +         '清除</button>'
  +       '<span class="grow"></span>'
  +     '</div>'
  +     '<div id="'+p+'ckmsg" class="msg"></div>'
  +   '</div>'
  + '</div>'

  + '<div class="fsec"><h4>自定义请求头</h4>'
  +   '<div class="hint" style="margin:0 0 6px">有些站点的接口会校验来源，'
  +     '缺了会返回 403。内置站点已预置好，一般不用改。</div>'
  +   '<div id="'+p+'hdr">'+headerRows(s.headers)+'</div>'
  +   '<button type="button" class="b" onclick="addHeaderRow(\''+id+'\')">+ 增加一行</button>'
  + '</div>'

  + '<div class="formfoot">'
  +   '<button type="button" class="b pri" onclick="saveSite(\''+id+'\')">保存</button>'
  +   '<button type="button" class="b" onclick="toggleEdit(\''+id+'\')">取消</button>'
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
  var aOn = $(p+'aiOn'); set($(p+'aiN'), !!(aOn&&aOn.checked)); lb(aOn, !!(aOn&&aOn.checked));
  var daily = document.querySelector('input[name="'+p+'mode"][value="daily"]');
  var isDaily = !!(daily && daily.checked);
  set($(p+'hour'), isDaily); set($(p+'min'), isDaily);
  var box = $(p+'dailyBox'); if(box) box.style.opacity = isDaily? '1':'0.45';
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
    ai_enabled: $(p+'aiOn').checked,
    ai_after_failures: parseInt($(p+'aiN').value||'3',10),
    notify: $(p+'notify').value,
    headers: collectHeaders(id)
  };
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
  show('f_'+id+'_ckmsg','检查中…','');
  api('POST','/api/site/cookie/check',{id:id}).then(function(d){
    var cls = d.status===1? 'ok' : (d.status===2? 'err' : '');
    show('f_'+id+'_ckmsg', d.message, cls);
    loadSites();
  }).catch(function(e){
    show('f_'+id+'_ckmsg','检查失败：'+e.message,'err');
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
 * 所以我们只要两件事：
 *   ① 页面上放一个带那些 data-* 属性的按钮
 *   ② 监听 message，收到 cookieRaw 就转成 Cookie 串调我们自己的接口存起来
 *
 * 这里不需要配对码：脚本是在**我们自己的页面**上跑的，
 * 浏览器会自动带上本容器的登录会话 Cookie，直接复用现有认证即可。
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
    showCookieMsg(sid, '从浏览器读取到 ' + pairs.length + ' 项，正在保存…', '');
    saveCookieText(sid, pairs.join('; '));
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
    + ' onclick="markCookieImportStart(\'' + esc(s.id) + '\')">'
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

/* 与手动粘贴走同一条保存路径：后端会自己识别格式 */
function saveCookieText(id, text){
  api('POST','/api/site/cookie',{id:id, cookie:text}).then(function(d){
    /* 先把提示写下来再刷新列表 —— 刷新会重渲染整个表单，
       直接写在旧 DOM 上会被冲掉，用户就看不到"已保存"了。 */
    var msg = '已保存 ' + d.count + ' 项（识别为：' + d.format + '）。'
            + '可以点「检查是否有效」。';
    CK_PENDING_MSG[id] = { text: msg, kind: 'ok' };
    loadSites();
  }).catch(function(e){
    showCookieMsg(id, '保存失败：' + e.message, 'err');
  });
}

/* ============ 自动导入：配套浏览器助手（油猴脚本，备用方案） ============ */

function originOf(){
  /* 容器对外的地址就用当前页面访问的地址，最准（含端口） */
  return location.origin || (location.protocol+'//'+location.host);
}

function userscriptUrl(){
  return originOf() + '/autocheckin-cookie.user.js?host='
    + encodeURIComponent(location.host);
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
    +     '<li>再点「<b>检查是否有效</b>」确认</li>'
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
    +   '<h4>不想装扩展？两条备选</h4>'
    +   '<div class="tip" style="font-size:12.5px;line-height:1.9;color:#3a424d">'
    +     '<b>① 手动粘贴（最省事）：</b>'
    +     '在你已登录的浏览器里按 F12 → Network → F5 → 点第一条请求 → '
    +     '右键 Copy as cURL → 粘到上面的输入框。'
    +     '一次复制就含 HttpOnly 字段，不用装任何东西。<br>'
    +     '<b>② 用本容器自带的油猴脚本：</b>'
    +     '<a href="'+esc(userscriptUrl())+'">安装/更新油猴脚本</a>'
    +     '（需要已装 Tampermonkey）。它在目标网站上开一个面板引导你粘贴，'
    +     '但受浏览器限制同样读不到 HttpOnly 的值。'
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

/* 说明：原先这里有一组「生成配对码 / 吊销脚本令牌」的界面函数。
   现在主路径改成了承接 QD 的 get-cookies 扩展 —— 脚本就在本容器页面上跑，
   浏览器会自动带上本容器的登录会话，所以不再需要配对码。
   相关后端接口保留（配套油猴脚本仍用它换令牌），但页面不再展示那套流程。 */

function refreshAll(){ loadOverview(); loadSites(); }

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
    $('tplList').textContent = (d.templates||[]).map(function(t){
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

/* ---------------- 设置 ---------------- */
function loadSettings(){
  api('GET','/api/settings').then(function(d){
    SETTINGS = d;
    $('set_proxy').value = d.proxy || '';
    $('set_headless').value = d.headless ? '1' : '0';
    $('set_cdp').value = d.remote_cdp_url || '';
    var n = d.notify || {};
    $('set_nmode').innerHTML = (META.notify_modes||[]).map(function(m){
      return '<option value="'+esc(m)+'"'+(m===n.mode?' selected':'')+'>'+esc(m)+'</option>';
    }).join('');
    $('set_nproxy').innerHTML = (META.proxy_modes||[]).map(function(m){
      return '<option value="'+esc(m)+'"'+(m===n.proxy_mode?' selected':'')+'>'
        + esc(m==='inherit'?'跟随主代理':(m==='direct'?'直连':'自定义'))+'</option>';
    }).join('');
    $('set_nproxy_url').value = n.proxy_url || '';
    $('set_aikey').value = '';
    $('set_model').value = (d.ai && d.ai.model) || 'deepseek-chat';
    $('set_maxai').value = (d.ai && d.ai.max_calls_per_day) || 20;
    $('set_aifail').value = (d.ai && d.ai.fail_threshold) || 2;
    $('set_aiauto').value = (d.ai && d.ai.auto_disable === false) ? '0' : '1';
    $('set_wdurl').value = (d.webdav && d.webdav.url) || '';
    $('set_wduser').value = (d.webdav && d.webdav.username) || '';
    $('set_wdpw').value = '';
    $('set_wdkey').checked = !!(d.webdav && d.webdav.backup_key);
    loadAuthState();
    loadKeyInfo();
    renderChannels(n, d);
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
  var labels = {pushplus:'pushplus', serverchan:'Server酱', wecom:'企业微信机器人',
                telegram:'Telegram'};
  var fields = {
    pushplus:  {key:'pushplus_token',  ph:'token', masked:n.pushplus_token_masked},
    serverchan:{key:'serverchan_key',  ph:'SCT…',  masked:n.serverchan_key_masked},
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
      + '<option value=""'+(cp.mode?'':' selected')+'>跟随全局</option>'
      + '<option value="direct"'+(cp.mode==='direct'?' selected':'')+'>直连</option>'
      + '<option value="custom"'+(cp.mode==='custom'?' selected':'')+'>自定义</option>'
      + '</select>'
      + '<input class="chProxyUrl" data-ch="'+esc(ch)+'" placeholder="自定义代理地址" '
      + 'value="'+esc(cp.url||'')+'">'
      + '</div>';
  }).join('');
  $('chanBox').innerHTML = html;
}

function collectNotify(){
  var n = {mode: $('set_nmode').value, proxy_mode: $('set_nproxy').value,
           proxy_url: $('set_nproxy_url').value.trim(),
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
  return n;
}

function saveSettings(){
  var body = {
    proxy: $('set_proxy').value.trim(),
    headless: $('set_headless').value === '1',
    remote_cdp_url: $('set_cdp').value.trim(),
    notify: collectNotify(),
    webdav: {url: $('set_wdurl').value.trim(), username: $('set_wduser').value.trim(),
             password: $('set_wdpw').value, backup_key: $('set_wdkey').checked},
    ai: {api_key: $('set_aikey').value, model: $('set_model').value.trim(),
         max_calls_per_day: parseInt($('set_maxai').value||'20', 10),
         fail_threshold: parseInt($('set_aifail').value||'2', 10),
         auto_disable: $('set_aiauto').value === '1'}
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
setInterval(function(){
  if($('mainBody').className === '') refreshAll();
}, 30000);
</script>
</body>
</html>
"""
