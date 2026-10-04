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
                 version: str = '', global_proxy_mode: str = ''):
        self.store = store
        self.box = box                  # SecretBox，用于解出站点密码/API Key
        self.browser = browser
        self.proxy = proxy
        # 全局代理模式（direct/custom）：决定 inherit 的站点最终走不走代理
        self.global_proxy_mode = global_proxy_mode or ''
        self.now_fn = now_fn or (lambda: int(time.time()))
        self.rng = rng or random.Random()
        self.checkin_fn = checkin_fn or _default_checkin
        self.analyze_fn = analyze_fn or _default_analyze
        self.notify_fn = notify_fn or notify_mod.notify
        # 系统级告警（AI 也失败、站点被自动停用）走这里，不受站点通知策略限制
        self.alert_fn = alert_fn or notify_mod.send_alert
        self.version = version

    # ------------------------------------------------------------------
    def _proxy_for(self, site: SiteConfig) -> str:
        """这个站点最终该走哪个代理（空串=直连）。

        站点可以单独设置：跟随全局 / 强制直连 / 用独立代理。
        单独抽出来是因为它决定"这个站点走不走代理"，
        排查"为什么这个站点连不上"时第一个要看的就是它。
        """
        from .models import resolve_site_proxy
        return resolve_site_proxy(site, self.proxy, self.global_proxy_mode)

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
    def _daily_limit(self, cfg: AppConfig) -> int:
        """每个站点每天的尝试次数上限（0 = 不限制）。"""
        ai = cfg.ai
        if not getattr(ai, 'daily_limit_enabled', True):
            return 0
        try:
            return max(0, int(getattr(ai, 'max_attempts_per_day', 3) or 3))
        except (TypeError, ValueError):
            return 3

    def due_sites(self, cfg: AppConfig, state: RuntimeState,
                  now: int) -> List[SiteConfig]:
        """到点且启用的站点。

        额外过滤"今天已用完尝试次数"的站点 —— 用户要求：
        每天每个站点最多试 N 次，用完就等第二天，
        避免一直重试、一直问 AI 把额度烧光。
        """
        today = self._today(now)
        limit = self._daily_limit(cfg)
        out = []
        for site in cfg.sites:
            if not site.enabled:
                continue
            nxt = state.next_run_at(site.id)
            if nxt and nxt > now:
                continue
            if state.day_exhausted(site.id, today, limit):
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

        result = self.checkin_fn(site, self._proxy_for(site), password,
                                 self.browser, cookie)

        # 记一次"今天的尝试"。用完当天额度后就不再排下一次尝试
        # （due_sites 会过滤掉），避免一直重试。
        today = self._today(now)
        attempts = state.bump_day_attempts(site.id, today)

        # 结果回写到调度状态（成功清连败，失败累加）
        next_at = plan_next(sched, now, result.success, self.rng)
        site.apply_schedule_state(sched)
        state.set_next_run_at(site.id, next_at)

        run = SiteRun(site_id=site.id, site_name=site.name, result=result,
                      next_run_at=next_at)

        # 签到成功 -> 清掉"今天的问题站点"与"已暂停"标记。
        # 这是逻辑闭环：站点恢复后不该继续背着之前的失败标记，
        # 否则用户会看到"今天已停止重试"这类过期提示，也不知道它已经好了。
        if result.success:
            self._clear_problem_flags(site, cfg, state, now, run)

        # 连续失败达到阈值 → 请 AI 分析（每天只问一次，见 _maybe_analyze）
        if not result.success and should_ask_ai(sched):
            run.ai_used = True
            run.ai_note = self._maybe_analyze(site, cfg, state, result, now)

        # 当天额度用完：标记为"今天的问题站点"，通知一次，然后今天不再试。
        # 第二天 due_sites 会重新放行（day 记录按日期重置）。
        #
        # 注意判断条件用的是【日期】而不是 day_problem 这个布尔标记：
        # 布尔标记第二天仍然为 True，会让"第二天还失败就暂停"的逻辑永远走不到
        # （这里踩过一次）。
        limit = self._daily_limit(cfg)
        today_str = self._today(now)
        already_marked_today = (
            state.get_site_flag(site.id, 'day_problem_date') == today_str)
        if (not result.success and limit and attempts >= limit
                and not already_marked_today):
            self._mark_day_problem(site, cfg, state, result, now, attempts, limit)
            run.ai_note = (run.ai_note + '｜' if run.ai_note else '') + \
                '今天已尝试 %d 次，不再重试；明天会重新开始' % attempts

        # 通知（按策略）
        notify_cfg = cfg.notify
        run.notified = self.notify_fn(result, notify_cfg, self.proxy)

        return run

    def _clear_problem_flags(self, site: SiteConfig, cfg: AppConfig,
                             state: RuntimeState, now: int, run) -> None:
        """签到成功后清掉问题标记与 AI 失败计数。

        用户要求：签到成功之后就清除"问题站点"和"暂停"标记。

        说明一个边界：被自动**暂停**（site.enabled=False）的站点不会走到这里，
        因为 due_sites 会过滤掉停用的站点 —— 它没有"靠成功自愈"的机会。
        这是刻意的：暂停的目的就是停止消耗，包括不再试、不再问 AI。
        用户修好后到「站点」页手动「启用」即可。
        这里清掉的是"当天的问题"标记和 AI 失败计数，
        让恢复后的站点不再背着过期状态（卡片上不会残留"今天已停止重试"）。
        """
        cleared = []

        # 1) 当天的问题标记
        if state.get_site_flag(site.id, 'day_problem'):
            cleared.append('今天的问题标记')
        state.clear_day_problem(site.id)

        # 2) AI 连败计数
        site.state = dict(site.state or {})
        if int(site.state.get('ai_consecutive_failures') or 0) > 0:
            cleared.append('AI 失败计数')
        site.state['ai_consecutive_failures'] = 0
        site.state['last_ai_error'] = ''
        site.state.pop('ai_failed_at', None)

        # 3) 暂停原因（站点仍是启用状态时才会残留，比如用户已手动启用过）
        if site.state.get('disabled_reason'):
            cleared.append('暂停标记')
            site.state.pop('disabled_reason', None)
            site.state.pop('disabled_at', None)

        if cleared and run is not None:
            note = '已清除：%s' % '、'.join(cleared)
            run.ai_note = (run.ai_note + '｜' if run.ai_note else '') + note

    def _mark_day_problem(self, site: SiteConfig, cfg: AppConfig,
                          state: RuntimeState, result: CheckinResult,
                          now: int, attempts: int, limit: int) -> None:
        """标记"今天不再尝试"并推送通知。第二天自动解除。

        如果昨天也发生过同样的事（说明连续两个自然日都修不好），
        就直接暂停该站点 —— 这是用户要求的"第二天还不行就停掉并通知"。
        """
        prev_date = state.get_site_flag(site.id, 'day_problem_date')
        today = self._today(now)
        repeated = bool(prev_date) and prev_date != today

        state.set_site_flag(site.id, 'day_problem', True)
        state.set_site_flag(site.id, 'day_problem_at', int(now))
        state.set_site_flag(site.id, 'day_problem_date', today)
        reason = ('今天已尝试 %d 次仍未成功（上限 %d 次），'
                  '今天不再重试，明天自动重新开始' % (attempts, limit))
        state.set_site_flag(site.id, 'day_problem_reason', reason)

        # 连续第二天修不好 -> 暂停该站点
        if repeated and getattr(cfg.ai, 'auto_disable', True) and site.enabled:
            site.enabled = False
            site.state['disabled_reason'] = (
                '连续两天（%s 起）自动修复均失败，已于 %s 暂停；'
                '请检查站点配置或登录 Cookie 后再手动启用'
                % (prev_date, time.strftime('%Y-%m-%d %H:%M',
                                            time.localtime(now))))
            site.state['disabled_at'] = int(now)
            if notify_mod.has_any_channel(cfg.notify):
                body = '\n'.join([
                    '站点：%s（%s）' % (site.name, site.id),
                    '失败原因：%s' % (result.message or '未知'),
                    '',
                    '连续两天每天尝试 %d 次、并请 AI 分析一次，均未解决，'
                    '已自动暂停该站点。' % limit,
                    '不会再消耗 AI 额度。',
                    '',
                    '处理建议：检查登录 Cookie 是否失效、或站点是否改版；',
                    '修好后到「站点」页手动「启用」即可。',
                ])
                try:
                    self.alert_fn(cfg.notify, '自动签到 · 站点已暂停', body,
                                  self.proxy)
                except Exception:                               # noqa: BLE001
                    pass
            return

        if notify_mod.has_any_channel(cfg.notify):
            body = '\n'.join([
                '站点：%s（%s）' % (site.name, site.id),
                '失败原因：%s' % (result.message or '未知'),
                '',
                reason + '。',
                '如果这个站点已失效，可以到「站点」页停用它；',
                '若明天仍失败，会自动暂停它并再通知你。',
            ])
            try:
                self.alert_fn(cfg.notify, '自动签到 · 今日已停止重试', body,
                              self.proxy)
            except Exception:                                   # noqa: BLE001
                pass

    def _maybe_analyze(self, site: SiteConfig, cfg: AppConfig, state: RuntimeState,
                       result: CheckinResult, now: int) -> str:
        """调用 AI 分析；成功则把补丁写回站点配置。

        token 防护（三层，按用户要求）：
          1. 每天每站点最多问 AI `ai_calls_per_day` 次（默认 1 次）
          2. 每天每站点尝试次数上限由 due_sites 把关（默认 3 次）
          3. 全局每日调用总量上限（默认 20 次），防止多站点同时坏掉烧光额度
        连续 AI 失败达到阈值 → 自动停止该站点签到并推送告警（用户要求）。

        另外实现用户要求的"第二天还是不行就暂停"：
        如果昨天也被标记为"当天的问题站点"，而今天用完额度、AI 同样没能解决，
        说明这个站点已经持续两个自然日无法自动修复 -> 暂停它并通知。
        """
        today = self._today(now)
        if not cfg.ai.enabled:
            return ''

        # ① 每天每站点只问一次（默认）：问过就不再问，等第二天
        per_day = max(1, int(getattr(cfg.ai, 'ai_calls_per_day', 1) or 1))
        calls_today = state.ai_calls_today(site.id, today)
        if calls_today >= per_day:
            return 'AI 分析今天已问过（每天 %d 次），不再重复；明天再试' % per_day
        # 保留原来的绝对上限作为第二重保护
        hard = max(1, int(cfg.ai.max_calls_per_day or 20))
        if calls_today >= hard:
            return 'AI 分析已达今日上限，跳过'

        # ③ 全局每日总量上限
        gcap = int(getattr(cfg.ai, 'global_max_calls_per_day', 20) or 0)
        if gcap > 0 and state.ai_calls_total_today(today) >= gcap:
            return ('今天全部站点的 AI 分析已达总量上限（%d 次），'
                    '不再调用；明天恢复' % gcap)

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
        state.mark_day_ai_done(site.id, today)
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
