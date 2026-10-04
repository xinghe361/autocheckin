"""服务层：界面/接口背后的业务逻辑。

单独成层的目的：所有操作都可以直接单测，不需要起 HTTP 服务。
web.py 里的路由只是薄薄一层转发。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import notify as notify_mod
from . import secrets as S
from . import webdav as W
from .models import (AppConfig, NotifyConfig, SiteConfig, Step, WebdavConfig,
                     AiConfig, KIND_CUSTOM, default_config)
from .recorder import RawEvent, StepRecorder, merge_steps
from .runner import Runner
from .store import RuntimeState, Store
from .templates import builtin_sites, template_by_id


@dataclass
class RecordSession:
    """一次"新增站点 → 记录我的签到操作"会话。"""

    site_id: str
    start_url: str
    name: str = ''
    started_at: float = field(default_factory=time.time)
    recorder: StepRecorder = field(default_factory=StepRecorder)
    running: bool = True

    def add_events(self, events: List[Dict[str, Any]]) -> int:
        n = 0
        for e in events or []:
            try:
                self.recorder.add_dict(e)
                n += 1
            except Exception:  # noqa: BLE001
                continue
        return n

    def steps(self) -> List[Step]:
        return self.recorder.to_steps(self.start_url)

    def event_count(self) -> int:
        return len(self.recorder.events)


class Service:
    """把 store / secrets / runner / webdav 组装成界面可用的操作。"""

    def __init__(self, data_dir: str, version: str = '',
                 env_key: Optional[str] = None, proxy: str = '',
                 browser=None, runner_factory=None, log_fn=None):
        self.data_dir = data_dir
        self.version = version
        self.store = Store(data_dir)
        key = S.load_or_create_key(data_dir, env_key=env_key)
        self.box = S.SecretBox(key)
        self.proxy = proxy
        self.browser = browser
        self._runner_factory = runner_factory
        self._record_lock = threading.Lock()
        self._recordings: Dict[str, RecordSession] = {}
        # 可注入的日志函数：后台巡检等场景需要留痕，但单测里不该刷屏
        self.log = log_fn or (lambda *a: None)

    # ------------------------------------------------------------ 基础
    def load_config(self) -> AppConfig:
        cfg = self.store.load_config()
        if not cfg.sites:
            cfg.sites = builtin_sites()
        # 环境变量里的代理优先于配置文件（方便 compose 统一控制）。
        # 注意：只有配置里还没设代理时才顶上去，否则会覆盖掉用户在
        # 网页「设置 → 网络」里的选择（网页改完却"不生效"就是这么来的）。
        from .models import PROXY_CUSTOM, PROXY_INHERIT
        if self.proxy and not cfg.proxy:
            cfg.proxy = self.proxy
            if (cfg.proxy_mode or '').lower() in ('', PROXY_INHERIT,
                                                 'direct', 'none'):
                cfg.proxy_mode = PROXY_CUSTOM
        return cfg

    def save_config(self, cfg: AppConfig) -> None:
        self.store.save_config(cfg)

    def make_runner(self, cfg: AppConfig) -> Runner:
        if self._runner_factory:
            return self._runner_factory(self)
        # 全局代理：模式为"直连"时一律传空串。
        # 这样"选了直连但地址栏还留着旧地址"也不会意外走代理。
        from .models import effective_global_proxy
        return Runner(self.store, box=self.box, browser=self.browser,
                      proxy=effective_global_proxy(cfg.proxy, cfg.proxy_mode),
                      version=self.version,
                      global_proxy_mode=cfg.proxy_mode)

    def load_state(self) -> RuntimeState:
        return self.store.load_state()

    # ------------------------------------------------------------ 站点
    def list_sites(self) -> List[Dict[str, Any]]:
        """站点列表（含下一次运行时间与连败状态，供界面展示）。"""
        cfg = self.load_config()
        state = self.load_state()
        out = []
        for s in cfg.sites:
            st = s.state or {}
            out.append({
                'id': s.id,
                'name': s.name,
                'enabled': s.enabled,
                'kind': s.kind,
                'template': s.template,
                'homepage': s.homepage,
                'need_browser': s.need_browser,
                'verify_ssl': s.verify_ssl,
                'mode': s.mode,
                'daily_hour': s.daily_hour,
                'daily_minute': s.daily_minute,
                'jitter_enabled': s.jitter_enabled,
                'jitter_seconds': s.jitter_seconds,
                'retry_enabled': s.retry_enabled,
                'retry_count': s.retry_count,
                'retry_interval_minutes': s.retry_interval_minutes,
                'ai_enabled': s.ai_enabled,
                'ai_after_failures': s.ai_after_failures,
                'notify': s.notify or '',
                'notify_override': s.notify_override,
                'has_credentials': bool(s.username or s.password_enc),
                'step_count': len(s.steps or []),
                'consecutive_failures': int(st.get('consecutive_failures') or 0),
                'last_success_at': st.get('last_success_at'),
                'next_run_at': state.next_run_at(s.id),
                # 登录态摘要：界面要显示"未配置 / 已配置 / 已失效"
                # 注意这里【不含任何 Cookie 内容】，只有状态与数量
                'has_cookie': bool(s.cookie_enc),
                'cookie_status': int(s.cookie_status or 0),
                'cookie_updated_at': int(s.cookie_updated_at or 0),
                'headers': dict(s.headers or {}),
                # 站点级代理策略：界面要显示"直连 / 独立代理"标签
                'proxy_mode': s.proxy_mode or 'inherit',
                'proxy_url': s.proxy_url or '',
                # AI 相关状态：便于界面解释"为什么这个站点不跑了"
                'ai_consecutive_failures': int(st.get('ai_consecutive_failures') or 0),
                'last_ai_error': st.get('last_ai_error') or '',
                'disabled_reason': st.get('disabled_reason') or '',
            })
        return out

    def get_site(self, site_id: str) -> Optional[SiteConfig]:
        return self.load_config().site(site_id)

    def upsert_site(self, data: Dict[str, Any]) -> SiteConfig:
        """新增或修改站点。密码为空时保留原密码（避免界面上留空把密码清掉）。"""
        cfg = self.load_config()
        site_id = str(data.get('id') or '').strip()
        if not site_id:
            raise ValueError('站点 id 不能为空')

        existing = cfg.site(site_id)
        site = existing or SiteConfig(id=site_id, name=site_id)

        if 'name' in data and data['name']:
            site.name = str(data['name'])
        for k in ('homepage', 'template', 'kind', 'mode', 'notify',
                  'notify_override', 'proxy_mode'):
            if k in data and data[k] is not None:
                setattr(site, k, str(data[k]))
        # 站点级独立代理地址（只在 proxy_mode='custom' 时生效）
        if 'proxy_url' in data and data['proxy_url'] is not None:
            site.proxy_url = str(data['proxy_url'])
        for k in ('enabled', 'need_browser', 'verify_ssl', 'jitter_enabled',
                  'retry_enabled', 'ai_enabled'):
            if k in data and data[k] is not None:
                setattr(site, k, bool(data[k]))
        for k in ('daily_hour', 'daily_minute', 'jitter_seconds', 'retry_count',
                  'retry_interval_minutes', 'ai_after_failures'):
            if k in data and data[k] is not None and str(data[k]) != '':
                setattr(site, k, int(data[k]))
        for k in ('success_keywords', 'fail_keywords'):
            if k in data and isinstance(data[k], list):
                setattr(site, k, [str(x) for x in data[k] if str(x).strip()])
        # 自定义请求头：{"Origin": "...", "Referer": "..."}
        # 允许通过传空字典来清空；值统一转成字符串，去掉空键
        if 'headers' in data and isinstance(data['headers'], dict):
            site.headers = {str(k): str(v) for k, v in data['headers'].items()
                            if str(k).strip() and v is not None}
        if 'steps' in data and isinstance(data['steps'], list):
            site.steps = [Step.from_dict(x) if isinstance(x, dict) else x
                          for x in data['steps']]

        # 凭据：只写不回显
        if 'username' in data and data['username'] is not None:
            site.username = str(data['username'])
        if data.get('password'):
            site.password_enc = self.box.encrypt(str(data['password']))

        if existing is None:
            cfg.sites.append(site)
        self.save_config(cfg)
        return site

    # ------------------------------------------------------------------
    # 登录 Cookie 管理
    # ------------------------------------------------------------------
    def set_site_cookie(self, site_id: str, raw: str) -> Dict[str, Any]:
        """保存站点的登录 Cookie。

        用户粘贴什么格式都行（cURL / Cookie 串 / 键值列表 / cookies.txt），
        这里负责识别并归一化成 "a=1; b=2" 再加密存起来。
        解析失败必须明确报错 —— 静默存下一堆垃圾只会让签到默默失败。
        """
        from .cookies import CookieParseError, parse_cookie_input, to_cookie_header

        cfg = self.load_config()
        site = cfg.site(str(site_id or '').strip())
        if not site:
            raise ValueError('站点不存在：%s' % site_id)

        try:
            parsed, fmt = parse_cookie_input(raw or '')
        except CookieParseError as e:
            raise ValueError(str(e)) from e

        cookie_header = to_cookie_header(parsed)
        site.cookie_enc = self.box.encrypt(cookie_header)
        site.cookie_updated_at = int(time.time())
        site.cookie_source = fmt
        # 刚换了 Cookie，之前的检查结论作废
        site.cookie_status = 0
        site.cookie_checked_at = 0
        site.cookie_error = ''
        self.save_config(cfg)
        return {
            'ok': True,
            'count': len(parsed),
            'format': fmt,
            'names': list(parsed.keys()),
            'updated_at': site.cookie_updated_at,
        }

    def clear_site_cookie(self, site_id: str) -> bool:
        cfg = self.load_config()
        site = cfg.site(str(site_id or '').strip())
        if not site:
            return False
        site.cookie_enc = ''
        site.cookie_updated_at = 0
        site.cookie_source = ''
        site.cookie_status = 0
        site.cookie_checked_at = 0
        site.cookie_error = ''
        self.save_config(cfg)
        return True

    def cookie_info(self, site_id: str) -> Dict[str, Any]:
        """给界面看的 Cookie 状态（绝不含 Cookie 内容）。"""
        site = self.get_site(str(site_id or '').strip())
        if not site:
            raise ValueError('站点不存在：%s' % site_id)
        names: List[str] = []
        header = ''
        if site.cookie_enc:
            header = self.box.try_decrypt(site.cookie_enc, '')
            if header:
                try:
                    from .cookies import parse_cookie_input
                    names = list(parse_cookie_input(header)[0].keys())
                except Exception:                               # noqa: BLE001
                    names = []
        return {
            'has_cookie': bool(site.cookie_enc),
            'count': len(names),
            'names': names,
            'updated_at': site.cookie_updated_at,
            'source': site.cookie_source,
            'status': site.cookie_status,       # 0未检查 1有效 2失效
            'checked_at': site.cookie_checked_at,
            'error': site.cookie_error,
        }

    def check_site_cookie(self, site_id: str, proxy: str = '') -> Dict[str, Any]:
        """检查站点 Cookie 是否有效，并把结论记到站点上。"""
        from .engine import check_cookie_valid

        cfg = self.load_config()
        site = cfg.site(str(site_id or '').strip())
        if not site:
            raise ValueError('站点不存在：%s' % site_id)
        if not site.cookie_enc:
            raise ValueError('该站点还没有配置 Cookie，请先粘贴一个')

        raw = self.box.try_decrypt(site.cookie_enc, '')
        if not raw:
            raise ValueError('已保存的 Cookie 无法解密，请重新粘贴')

        code, msg = check_cookie_valid(site, raw, proxy)
        site.cookie_status = int(code)
        site.cookie_checked_at = int(time.time())
        site.cookie_error = '' if code == 1 else msg
        self.save_config(cfg)
        return {'status': int(code), 'message': msg,
                'checked_at': site.cookie_checked_at}

    def delete_site(self, site_id: str) -> bool:
        cfg = self.load_config()
        before = len(cfg.sites)
        cfg.sites = [s for s in cfg.sites if s.id != site_id]
        if len(cfg.sites) == before:
            return False
        self.save_config(cfg)
        state = self.load_state()
        state.sites.pop(site_id, None)
        state.ai_calls.pop(site_id, None)
        self.store.save_state(state)
        return True

    def set_site_enabled(self, site_id: str, enabled: bool) -> bool:
        cfg = self.load_config()
        site = cfg.site(site_id)
        if not site:
            return False
        site.enabled = bool(enabled)
        self.save_config(cfg)
        return True

    # ------------------------------------------------------------ 运行
    def run_now(self, site_id: Optional[str] = None) -> Dict[str, Any]:
        """立即执行（单个站点或全部到期站点）。"""
        cfg = self.load_config()
        runner = self.make_runner(cfg)
        report = runner.run_once(cfg, only_site=site_id)
        return {
            'summary': report.summary(),
            'runs': [{
                'site_id': r.site_id,
                'site_name': r.site_name,
                'success': r.result.success,
                'message': r.result.message,
                'error_kind': r.result.error_kind,
                'duration_ms': r.result.duration_ms,
                'next_run_at': r.next_run_at,
                'ai_used': r.ai_used,
                'ai_note': r.ai_note,
                'notified': r.notified,
            } for r in report.runs],
        }

    # ------------------------------------------------------------ 录制
    def start_recording(self, site_id: str, name: str, start_url: str) -> Dict[str, Any]:
        """开启一次录制会话（不会自动打开浏览器，由调用方决定）。"""
        if not start_url:
            raise ValueError('请先填写站点主页地址，录制需要从它开始')
        with self._record_lock:
            session = RecordSession(site_id=site_id, start_url=start_url, name=name)
            self._recordings[site_id] = session
        return {'site_id': site_id, 'start_url': start_url,
                'event_count': 0, 'running': True}

    def recording_status(self, site_id: str) -> Dict[str, Any]:
        session = self._recordings.get(site_id)
        if not session:
            return {'running': False, 'event_count': 0, 'steps': []}
        return {'running': session.running,
                'event_count': session.event_count(),
                'steps': [s.to_dict() for s in session.steps()]}

    def push_events(self, site_id: str, events: List[Dict[str, Any]]) -> int:
        """录制脚本上报事件（页面里 POST 过来）。"""
        session = self._recordings.get(site_id)
        if not session or not session.running:
            return 0
        with self._record_lock:
            return session.add_events(events)

    def stop_recording(self, site_id: str, save: bool = True,
                       append: bool = True) -> Dict[str, Any]:
        """结束录制并（可选）把步骤保存进站点配置。"""
        session = self._recordings.get(site_id)
        if not session:
            raise ValueError('没有正在进行的录制会话')
        session.running = False
        new_steps = session.steps()

        saved = False
        total = len(new_steps)
        if save and new_steps:
            cfg = self.load_config()
            site = cfg.site(site_id)
            if not site:
                site = SiteConfig(id=site_id, name=session.name or site_id,
                                  kind=KIND_CUSTOM, homepage=session.start_url,
                                  need_browser=True)
                cfg.sites.append(site)
            if not site.homepage:
                site.homepage = session.start_url
            site.kind = KIND_CUSTOM
            site.need_browser = True
            site.steps = merge_steps(site.steps or [], new_steps) if append else list(new_steps)
            total = len(site.steps)
            self.save_config(cfg)
            saved = True

        self._recordings.pop(site_id, None)
        return {'saved': saved, 'event_count': session.event_count(),
                'new_steps': len(new_steps), 'total_steps': total,
                'steps': [s.to_dict() for s in new_steps]}

    def cancel_recording(self, site_id: str) -> bool:
        return self._recordings.pop(site_id, None) is not None

    def active_recordings(self) -> List[str]:
        return [k for k, v in self._recordings.items() if v.running]

    # ------------------------------------------------------------ 备份
    def webdav_client(self, cfg: AppConfig) -> W.WebdavClient:
        wd = cfg.webdav
        password = self.box.try_decrypt(wd.password_enc, '') if wd.password_enc else ''
        return W.WebdavClient(wd.url, wd.username, password,
                              proxy=cfg.proxy or self.proxy)

    def backup_now(self) -> Dict[str, Any]:
        cfg = self.load_config()
        if not cfg.webdav.url:
            raise ValueError('还没有配置 WebDAV 地址')
        client = self.webdav_client(cfg)
        r = W.do_backup(client, self.store.export_config(cfg),
                        self.load_state().to_dict(), self.version)
        # 用户主动开启了"一并备份密钥"时才备份它
        if cfg.webdav.backup_key:
            try:
                r['key'] = W.save_key(client, self.box.key or '')
            except Exception as e:                          # noqa: BLE001
                r['key'] = {'ok': False, 'error': str(e)}
        state = self.load_state()
        state.last_backup_at = int(time.time())
        self.store.save_state(state)
        return r

    # ------------------------------------------------------------ 密钥
    def key_info(self) -> Dict[str, Any]:
        """密钥来源与指纹（不回显密钥本身）。"""
        from .secrets import KEY_ENV
        env_key = os.environ.get(KEY_ENV, '').strip()
        key = self.box.key or ''
        return {
            'source': '环境变量' if env_key else '数据目录中的密钥文件',
            'has_key': bool(key),
            # 只给指纹，便于你核对"两边是不是同一个密钥"，又不怕泄露
            'fingerprint': (key[:6] + '…' + key[-4:]) if key else '',
            'env_var': KEY_ENV,
            'length': len(key),
        }

    def backup_key_now(self) -> Dict[str, Any]:
        """单独把密钥备份到 WebDAV。"""
        cfg = self.load_config()
        if not cfg.webdav.url:
            raise ValueError('还没有配置 WebDAV 地址')
        client = self.webdav_client(cfg)
        return W.save_key(client, self.box.key or '')

    def restore_key_from_cloud(self) -> Dict[str, Any]:
        """从 WebDAV 读回密钥，写入本地密钥文件。

        注意：只有当本地密钥与云端不一致时才有意义；写入后需要重启容器生效。
        """
        from .secrets import KEY_FILENAME
        cfg = self.load_config()
        if not cfg.webdav.url:
            raise ValueError('还没有配置 WebDAV 地址')
        client = self.webdav_client(cfg)
        key = W.load_key(client)
        if not key:
            raise ValueError('云端没有找到密钥备份（%s）' % W.KEY_FILE)

        local_exists = os.path.exists(os.path.join(self.data_dir, KEY_FILENAME))
        same = (self.box.key or '') == key
        if not same:
            S.SecretBox(key)                    # 先校验格式，坏密钥不要写进去
            path = os.path.join(self.data_dir, KEY_FILENAME)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, key.encode('ascii'))
            finally:
                os.close(fd)
        return {
            'ok': True,
            'changed': not same,
            'local_existed': local_exists,
            'fingerprint': key[:6] + '…' + key[-4:],
            'need_restart': not same,
        }

    def restore_from_cloud(self) -> Dict[str, Any]:
        cfg = self.load_config()
        if not cfg.webdav.url:
            raise ValueError('还没有配置 WebDAV 地址')
        client = self.webdav_client(cfg)
        data = W.do_restore(client)
        restored = AppConfig.from_dict(data['config'])
        # 保留当前的 WebDAV 凭据（恢复的内容里可能是别处的设置）
        restored.webdav.url = cfg.webdav.url
        restored.webdav.username = cfg.webdav.username
        restored.webdav.password_enc = cfg.webdav.password_enc
        self.save_config(restored)
        st = RuntimeState.from_dict(data['state'])
        self.store.save_state(st)
        return {'saved_at': data['saved_at'], 'sites': len(restored.sites)}

    def test_webdav(self, url: str = '', username: str = '',
                    password: str = '', use_saved: bool = True) -> Dict[str, Any]:
        cfg = self.load_config()
        if not url:
            url = cfg.webdav.url
        if not url:
            raise ValueError('请先填写 WebDAV 地址')
        if not password and use_saved and cfg.webdav.password_enc:
            password = self.box.try_decrypt(cfg.webdav.password_enc, '')
        if not username:
            username = cfg.webdav.username
        client = W.WebdavClient(url, username, password,
                                proxy=cfg.proxy or self.proxy)
        return W.test_connection(client)

    # ------------------------------------------------------------ 通知
    def test_notify(self, channel: str = '') -> Dict[str, Any]:
        """发一条测试通知，验证渠道配置是否可用。"""
        cfg = self.load_config()
        result = notify_mod.CheckinResult(
            site_id='__test__', site_name='测试通知', success=True,
            message='这是一条测试通知。收到即表示该渠道配置正确。')
        if channel:
            ncfg = NotifyConfig.from_dict(cfg.notify.to_dict())
            ncfg.mode = notify_mod.NOTIFY_ALL
            ncfg.channels = [channel]
        else:
            ncfg = cfg.notify
        report = notify_mod.send_all(ncfg, result, cfg.proxy or self.proxy)
        return {'report': report}

    def test_alert(self) -> Dict[str, Any]:
        """测试"系统级告警"通道（AI 分析失败、站点被自动停用走这条）。"""
        cfg = self.load_config()
        if not notify_mod.has_any_channel(cfg.notify):
            raise ValueError('还没有配置任何通知渠道')
        body = notify_mod.format_ai_failure_alert(
            '示例站点', int(getattr(cfg.ai, 'fail_threshold', 2) or 2),
            '这是测试：AI 返回 401（示例原因）', True)
        report = notify_mod.send_alert(cfg.notify, '自动签到 · 分析失败告警（测试）',
                                       body, cfg.proxy or self.proxy)
        return {'report': report}

    # ---------------------------------------------------- Cookie 效期检查
    # 间隔：每天最多查一次。Cookie 的有效期通常以天/周计，
    # 查太勤对站点不礼貌，也浪费请求。
    COOKIE_CHECK_INTERVAL = 24 * 3600

    def maybe_check_cookies(self, force: bool = False) -> Dict[str, Any]:
        """定期检查各站点的登录 Cookie，失效时推送通知。

        关键设计：**只在"有效 → 失效"的那一刻推一次**。
        否则每天都会重复推同一条"登录已失效"，变成骚扰，
        用户很快就会把通知关掉，反而失去意义。

        返回一份概要，便于调度日志与界面展示。
        """
        cfg = self.load_config()
        state = self.load_state()
        now = int(time.time())

        last = int(state.get('_cookie_check_at') or 0)
        if not force and last and (now - last) < self.COOKIE_CHECK_INTERVAL:
            return {'skipped': True, 'last_check_at': last,
                    'next_check_in': self.COOKIE_CHECK_INTERVAL - (now - last)}

        if not notify_mod.has_any_channel(cfg.notify):
            # 没有通知渠道也要更新检查时间，避免每分钟都重跑一遍
            state.set('_cookie_check_at', now)
            self.store.save_state(state)
            return {'skipped': True, 'reason': '未配置通知渠道'}

        checked = failing = newly_failed = 0
        details: List[Dict[str, Any]] = []

        from .engine import check_cookie_valid

        for site in cfg.sites:
            if not site.enabled or not site.cookie_enc:
                continue
            raw = self.box.try_decrypt(site.cookie_enc, '')
            if not raw:
                # 解不开本身就是个要报的问题
                code, msg = 2, '已保存的 Cookie 无法解密（密钥可能变了），请重新粘贴'
            else:
                try:
                    code, msg = check_cookie_valid(site, raw, cfg.proxy or '')
                except Exception as e:                          # noqa: BLE001
                    code, msg = 0, '检查时出错：%s' % e

            checked += 1
            prev = int(site.cookie_status or 0)
            site.cookie_status = int(code)
            site.cookie_checked_at = now
            site.cookie_error = '' if code == 1 else msg

            if code == 2:
                failing += 1
                # 只在状态"从非失效变成失效"时推；已经是失效状态就不重复推
                if prev != 2:
                    newly_failed += 1
                    details.append({'id': site.id, 'name': site.name,
                                    'message': msg})

        # 通知：把本轮新失效的站点汇总成一条，避免多条推送刷屏
        notified = False
        if details and notify_mod.has_any_channel(cfg.notify):
            title = '自动签到 · 登录状态失效告警'
            lines = ['以下站点的登录状态已失效，自动签到会失败，请重新配置 Cookie：', '']
            for d in details:
                lines.append('· %s（%s）' % (d['name'], d['id']))
                if d.get('message'):
                    lines.append('   %s' % d['message'])
            lines += ['', '到「站点」页对应站点下点「编辑」，粘贴新的 Cookie 即可。']
            try:
                notify_mod.send_alert(cfg.notify, title, '\n'.join(lines),
                                      cfg.proxy or self.proxy)
                notified = True
            except Exception as e:                              # noqa: BLE001
                self.log('发送 Cookie 失效告警失败：%s' % e)

        state.set('_cookie_check_at', now)
        self.store.save_state(state)
        self.save_config(cfg)

        return {'checked': checked, 'failing': failing,
                'newly_failed': newly_failed, 'notified': notified,
                'last_check_at': now, 'details': details}

    # ------------------------------------------------------------ 概览
    def overview(self) -> Dict[str, Any]:
        cfg = self.load_config()
        state = self.load_state()
        from .models import effective_global_proxy
        eff = effective_global_proxy(cfg.proxy, cfg.proxy_mode)
        return {
            'version': self.version,
            # 是否"真的"在走代理：模式为直连时，哪怕地址栏还留着旧地址也算未启用
            'proxy_configured': bool(eff),
            'proxy_mode': cfg.proxy_mode,
            'site_count': len(cfg.sites),
            'enabled_count': sum(1 for s in cfg.sites if s.enabled),
            'notify_channels': notify_mod.configured_channels(cfg.notify),
            'notify_mode': cfg.notify.mode,
            'webdav_configured': bool(cfg.webdav.url),
            'ai_configured': bool(cfg.ai.api_key_enc),
            'last_run_at': state.last_run_at,
            'last_backup_at': state.last_backup_at,
            'active_recordings': self.active_recordings(),
        }
