"""运行器：把调度、签到、重试、AI 分析、通知串成一条流水线。

刻意的可测性设计：所有有副作用的东西（取当前时间、签到、调 AI、发通知、存状态）
都可以注入，因此不需要真实网络/浏览器就能验证完整的编排逻辑。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import notify as notify_mod
from .models import AppConfig, NOTIFY_NONE, SiteConfig
from .notify import CheckinResult
from .schedule import plan_next, should_ask_ai
from .store import RuntimeState, Store


@dataclass
class SiteRun:
    """单个站点一轮执行的结果。"""

    site_id: str
    site_name: str
    result: CheckinResult
    next_run_at: int = 0
    ai_used: bool = False
    ai_note: str = ''
    ai_changed: List[str] = field(default_factory=list)
    notified: Dict[str, str] = field(default_factory=dict)
    skipped: str = ''


@dataclass
class RunReport:
    """一轮所有站点的汇总。"""

    started_at: int
    finished_at: int = 0
    runs: List[SiteRun] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.runs if r.result.success)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.runs if not r.result.success)

    def summary(self) -> str:
        if not self.runs:
            return '没有到期的站点'
        parts = []
        if self.success_count:
            parts.append('成功 %d' % self.success_count)
        if self.fail_count:
            parts.append('失败 %d' % self.fail_count)
        skipped = [r for r in self.runs if r.skipped]
        if skipped:
            parts.append('跳过 %d' % len(skipped))
        return '，'.join(parts) if parts else '无结果'


def _default_checkin(site: SiteConfig, proxy: str, password: str, browser,
                     cookie_header: str = '') -> CheckinResult:
    from .engine import checkin_site
    return checkin_site(site, proxy=proxy, password=password, browser=browser,
                        cookie_header=cookie_header)


def _default_analyze(site: SiteConfig, result: CheckinResult, ai_cfg, api_key: str,
                     proxy: str) -> Any:
    from .deepseek import analyze
    return analyze(site, result, ai_cfg, api_key, proxy=proxy)


class Runner:
    """编排器。"""

    def __init__(self, store: Store, box=None, browser=None, proxy: str = '',
                 now_fn: Callable[[], int] = None, rng: random.Random = None,
                 checkin_fn: Callable = None, analyze_fn: Callable = None,
                 notify_fn: Callable = None, alert_fn: Callable = None,
                 version: str = ''):
        self.store = store
        self.box = box                  # SecretBox，用于解出站点密码/API Key
        self.browser = browser
        self.proxy = proxy
        self.now_fn = now_fn or (lambda: int(time.time()))
        self.rng = rng or random.Random()
        self.checkin_fn = checkin_fn or _default_checkin
        self.analyze_fn = analyze_fn or _default_analyze
        self.notify_fn = notify_fn or notify_mod.notify
        # 系统级告警（AI 也失败、站点被自动停用）走这里，不受站点通知策略限制
        self.alert_fn = alert_fn or notify_mod.send_alert
        self.version = version

    # ------------------------------------------------------------------
    def _password_of(self, site: SiteConfig) -> str:
        """取站点密码。

        密钥不匹配时**不能静默返回空串** —— 那样只会表现为"登录失败"，
        完全看不出是密钥问题。这里记下原因，由 checkin 前置检查给出明确提示。
        """
        if not site.password_enc or not self.box:
            return ''
        plain, err = self.box.decrypt_checked(site.password_enc)
        if err:
            self.last_error = '站点「%s」的密码无法解密：%s' % (site.name, err)
            return ''
        return plain

    def _cookie_of(self, site: SiteConfig) -> str:
        """取站点登录 Cookie（解密后）。

        与密码同理：解不开必须留下明确原因，不能静默当空。
        另外，如果配置里存的是完整 Cookie 串或各种粘贴格式，这里统一
        归一化成请求头需要的 "a=1; b=2" 形式。
        """
        if not site.cookie_enc or not self.box:
            return ''
        raw, err = self.box.decrypt_checked(site.cookie_enc)
        if err:
            self.last_error = '站点「%s」的 Cookie 无法解密：%s' % (site.name, err)
            return ''
        if not raw:
            return ''
        # 兼容：历史数据里可能存的是 "name: value" 多行形式
        try:
            from .cookies import parse_cookie_input, to_cookie_header
            parsed, _fmt = parse_cookie_input(raw)
            return to_cookie_header(parsed)
        except Exception:                                       # noqa: BLE001
            # 解析失败就按原样使用（可能本来就是合法 Cookie 头）
            return raw

    def _api_key(self, cfg: AppConfig) -> str:
        if not cfg.ai.api_key_enc or not self.box:
            return ''
        return self.box.try_decrypt(cfg.ai.api_key_enc, '')

    def _today(self, now: int) -> str:
        return time.strftime('%Y-%m-%d', time.localtime(now))

    # ------------------------------------------------------------------
    def due_sites(self, cfg: AppConfig, state: RuntimeState,
                  now: int) -> List[SiteConfig]:
        """到点且启用的站点。"""
        out = []
        for site in cfg.sites:
            if not site.enabled:
                continue
            nxt = state.next_run_at(site.id)
            if nxt and nxt > now:
                continue
            out.append(site)
        return out

    def ensure_scheduled(self, cfg: AppConfig, state: RuntimeState, now: int) -> None:
        """给还没排过期的站点安排首次运行时间（不会立即执行）。"""
        from .schedule import next_run_at
        for site in cfg.sites:
            if not site.enabled:
                continue
            if not state.next_run_at(site.id):
                state.set_next_run_at(site.id, next_run_at(now, site.to_schedule(), self.rng))

    # ------------------------------------------------------------------
    def run_site(self, site: SiteConfig, cfg: AppConfig, state: RuntimeState,
                 now: int) -> SiteRun:
        """执行单个站点：签到 → 重试/调度 → AI → 通知 → 落状态。"""
        sched = site.to_schedule()
        sched.last_attempt_at = now
        site.state = dict(site.state or {})
        site.state['_attempt'] = sched.retries_done + 1

        # 前置检查：密码解不开就没必要真去登录（否则只会得到"登录失败"，看不出原因）
        password = self._password_of(site)
        if site.password_enc and not password and self.box:
            _, err = self.box.decrypt_checked(site.password_enc)
            if err:
                next_at = plan_next(sched, now, False, self.rng)
                site.apply_schedule_state(sched)
                state.set_next_run_at(site.id, next_at)
                result = CheckinResult(
                    site_id=site.id, site_name=site.name, success=False,
                    message='无法解密已保存的密码：%s。请到站点设置里重新填写密码。'
                            % err,
                    error_kind='secret_error')
                run = SiteRun(site_id=site.id, site_name=site.name,
                              result=result, next_run_at=next_at)
                run.notified = self.notify_fn(result, cfg.notify, self.proxy)
                return run

        # Cookie 同理：解不开就明确报错，别让用户对着"未登录"猜
        cookie = self._cookie_of(site)
        if site.cookie_enc and not cookie and self.box:
            _, err = self.box.decrypt_checked(site.cookie_enc)
            if err:
                next_at = plan_next(sched, now, False, self.rng)
                site.apply_schedule_state(sched)
                state.set_next_run_at(site.id, next_at)
                result = CheckinResult(
                    site_id=site.id, site_name=site.name, success=False,
                    message='无法解密已保存的 Cookie：%s。请到站点设置里重新粘贴 Cookie。'
                            % err,
                    error_kind='secret_error')
                run = SiteRun(site_id=site.id, site_name=site.name,
                              result=result, next_run_at=next_at)
                run.notified = self.notify_fn(result, cfg.notify, self.proxy)
                return run

        result = self.checkin_fn(site, self.proxy, password, self.browser,
                                 cookie)

        # 结果回写到调度状态（成功清连败，失败累加）
        next_at = plan_next(sched, now, result.success, self.rng)
        site.apply_schedule_state(sched)
        state.set_next_run_at(site.id, next_at)

        run = SiteRun(site_id=site.id, site_name=site.name, result=result,
                      next_run_at=next_at)

        # 连续失败达到阈值 → 请 AI 分析，成功后按新方案执行
        if not result.success and should_ask_ai(sched):
            run.ai_used = True
            run.ai_note = self._maybe_analyze(site, cfg, state, result, now)

        # 通知（按策略）
        notify_cfg = cfg.notify
        run.notified = self.notify_fn(result, notify_cfg, self.proxy)

        return run

    def _maybe_analyze(self, site: SiteConfig, cfg: AppConfig, state: RuntimeState,
                       result: CheckinResult, now: int) -> str:
        """调用 AI 分析；成功则把补丁写回站点配置。

        连续 AI 失败达到阈值 → 自动停止该站点签到并推送告警（用户要求）。
        """
        today = self._today(now)
        calls_today = state.ai_calls_today(site.id, today)
        if calls_today >= max(1, int(cfg.ai.max_calls_per_day or 20)):
            return 'AI 分析已达今日上限，跳过'
        if not cfg.ai.enabled:
            return ''

        api_key = self._api_key(cfg)
        if not api_key:
            return '未配置 DeepSeek API Key，跳过 AI 分析'

        from .deepseek import apply_patch
        from .schedule import mark_ai_analyzed

        site.state = dict(site.state or {})
        try:
            suggestion = self.analyze_fn(site, result, cfg.ai, api_key, self.proxy)
        except Exception as e:  # noqa: BLE001
            suggestion = None
            self._record_ai_failure(site, cfg, state, 'AI 调用异常：%s' % e, now)

        state.bump_ai_calls(site.id, today)
        # 不论是否成功都标记这一档已分析，避免每次失败都重复请求
        sched = site.to_schedule()
        mark_ai_analyzed(sched)
        site.apply_schedule_state(sched)

        if suggestion is None:
            return str(site.state.get('last_ai_error') or 'AI 分析失败')

        if not suggestion.ok:
            err = getattr(suggestion, 'error', '未知原因')
            self._record_ai_failure(site, cfg, state, err, now)
            return 'AI 分析未成功：%s' % err

        # 分析成功 → 清零 AI 失败计数
        site.state['ai_consecutive_failures'] = 0
        site.state['last_ai_error'] = ''

        note = suggestion.analysis or ''
        if suggestion.patch:
            if getattr(cfg.ai, 'auto_apply', True):
                changed = apply_patch(site, suggestion.patch)
                if changed:
                    note += '（已按建议更新：%s）' % '、'.join(changed)
                else:
                    note += '（建议与现有配置一致，无需改动）'
            else:
                note += '（已生成建议，但未开启自动应用）'
        return note

    def _record_ai_failure(self, site: SiteConfig, cfg: AppConfig,
                           state: RuntimeState, error: str, now: int) -> None:
        """记一次 AI 失败；达到阈值就停用站点并推送告警。"""
        site.state = dict(site.state or {})
        fails = int(site.state.get('ai_consecutive_failures') or 0) + 1
        site.state['ai_consecutive_failures'] = fails
        site.state['last_ai_error'] = error
        site.state['ai_failed_at'] = int(now)

        threshold = max(1, int(getattr(cfg.ai, 'fail_threshold', 2) or 2))
        if fails < threshold:
            return

        disabled = False
        if getattr(cfg.ai, 'auto_disable', True) and site.enabled:
            site.enabled = False
            site.state['disabled_reason'] = (
                '连续 %d 次自动分析失败，已于 %s 自动停止' %
                (fails, time.strftime('%Y-%m-%d %H:%M', time.localtime(now))))
            site.state['disabled_at'] = int(now)
            disabled = True

        # 告警不受按站点通知策略限制：只要配了渠道就推
        alert_cfg = cfg.notify
        if notify_mod.has_any_channel(alert_cfg):
            body = notify_mod.format_ai_failure_alert(site.name, fails, error, disabled)
            try:
                self.alert_fn(alert_cfg, '自动签到 · 分析失败告警', body, self.proxy)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    def run_once(self, cfg: Optional[AppConfig] = None,
                 state: Optional[RuntimeState] = None,
                 only_site: Optional[str] = None) -> RunReport:
        """跑一轮：只处理到期的站点（only_site 可强制只跑某个）。"""
        now = self.now_fn()
        cfg = cfg or self.store.load_config()
        state = state or self.store.load_state()
        self.ensure_scheduled(cfg, state, now)

        if only_site:
            targets = [s for s in cfg.sites if s.id == only_site]
        else:
            targets = self.due_sites(cfg, state, now)

        report = RunReport(started_at=now)
        for site in targets:
            try:
                report.runs.append(self.run_site(site, cfg, state, now))
            except Exception as e:  # noqa: BLE001
                # 单站点异常不能影响其它站点
                report.runs.append(SiteRun(
                    site_id=site.id, site_name=site.name,
                    result=CheckinResult(site_id=site.id, site_name=site.name,
                                         success=False,
                                         message='执行异常：%s' % e,
                                         error_kind='exception'),
                    next_run_at=state.next_run_at(site.id)))

        state.last_run_at = now
        report.finished_at = self.now_fn()
        self.store.save_config(cfg)
        self.store.save_state(state)
        return report
