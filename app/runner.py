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


def _looks_like_cookie_header(raw: str) -> bool:
    """raw 本身就是合法的 `a=1; b=2` 形状吗？（用于解析失败时的兜底判断）

    只有在"确实是 Cookie 头"时才允许原样使用。别的一律不放过，
    否则会把 Host=… 之类的请求头当 Cookie 发出去。
    """
    import re as _re
    t = (raw or '').strip()
    if not t or '=' not in t:
        return False
    if any(c in t for c in ('\r', '\n', '\x00')):
        return False
    # 每个分号段都必须是 name=value，且名字是合法 token
    for part in t.split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            return False
        name = part.split('=', 1)[0].strip()
        if not _re.match(r'^[A-Za-z0-9!#$%&\'*+\-.^_`|~]+$', name):
            return False
    return True


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
        from .cookies import CookieParseError, parse_cookie_input, \
            to_cookie_header
        try:
            parsed, _fmt = parse_cookie_input(raw)
        except CookieParseError as e:
            # 解析不出来：如果它本身就是合法的 Cookie 头形状，就按原样用；
            # 否则**记下原因**、不要硬发出去。
            #
            # 以前这里是 `except Exception: return raw` —— 等于把
            # "Cookie:  session=abc" 这类畸形串原样发给站点，站点回
            # "未登录"，而上层完全不知道真正原因是本地存储坏了。
            # 静默兜底比报错危险得多（这条踩过）。
            if not _looks_like_cookie_header(raw):
                self.last_error = ('站点「%s」的 Cookie 无法解析：%s'
                                   % (site.name, e))
                return ''
            return raw
        if not parsed:
            self.last_error = '站点「%s」的 Cookie 解析结果为空' % site.name
            return ''
        return to_cookie_header(parsed)

    def _api_key(self, cfg: AppConfig) -> str:
        if not cfg.ai.api_key_enc or not self.box:
            return ''
        return self.box.try_decrypt(cfg.ai.api_key_enc, '')

    def _today(self, now: int) -> str:
        return time.strftime('%Y-%m-%d', time.localtime(now))

    # ------------------------------------------------------------------
    def _ai_after_failures(self, cfg: AppConfig) -> int:
        """每连续失败几次调用一次 AI 分析（全局）。0 = 不启用。"""
        try:
            return max(0, int(getattr(cfg.ai, 'ai_after_failures', 3)))
        except (TypeError, ValueError):
            return 3

    def due_sites(self, cfg: AppConfig, state: RuntimeState,
                  now: int) -> List[SiteConfig]:
        """到点且启用的站点。

        注意：这里**不再**有"每站点每天最多尝试几次"的过滤。
        按用户要求统一逻辑：签到尝试次数完全由站点自己的
        retry_enabled / retry_count / retry_interval_minutes 决定。
        """
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

        result = self.checkin_fn(site, self._proxy_for(site), password,
                                 self.browser, cookie)

        # 结果回写到调度状态（成功清连败，失败累加）
        next_at = plan_next(sched, now, result.success, self.rng)
        site.apply_schedule_state(sched)
        state.set_next_run_at(site.id, next_at)

        run = SiteRun(site_id=site.id, site_name=site.name, result=result,
                      next_run_at=next_at)

        # 签到成功 -> 清掉"当天的问题"与"已暂停"标记。
        # 这是逻辑闭环：站点恢复后不该继续背着之前的失败标记，
        # 否则用户会看到"今天已停止重试"这类过期提示，也不知道它已经好了。
        if result.success:
            self._clear_problem_flags(site, cfg, state, now, run)

        # 连续失败达到倍数阈值 → 调用 AI 分析（阈值是全局设置）。
        # 每天次数的硬上限在 _maybe_analyze 里把关。
        if not result.success and should_ask_ai(sched, self._ai_after_failures(cfg)):
            run.ai_used = True
            run.ai_note = self._maybe_analyze(site, cfg, state, result, now)

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

        # 1) AI 连败计数
        site.state = dict(site.state or {})
        if int(site.state.get('ai_consecutive_failures') or 0) > 0:
            cleared.append('AI 失败计数')
        site.state['ai_consecutive_failures'] = 0
        site.state['last_ai_error'] = ''
        site.state.pop('ai_failed_at', None)

        # 2) 暂停原因（站点仍是启用状态时才会残留，比如用户已手动启用过）
        if site.state.get('disabled_reason'):
            cleared.append('暂停标记')
            site.state.pop('disabled_reason', None)
            site.state.pop('disabled_at', None)

        if cleared and run is not None:
            note = '已清除：%s' % '、'.join(cleared)
            run.ai_note = (run.ai_note + '｜' if run.ai_note else '') + note

    def _maybe_analyze(self, site: SiteConfig, cfg: AppConfig, state: RuntimeState,
                       result: CheckinResult, now: int) -> str:
        """调用 AI 分析；成功则把补丁写回站点配置。

        什么时候调用，以及调用几次（统一逻辑）：
          * 调用时机：由全局 ai_after_failures 的整数倍决定
            （调用方已经判断过；0 表示不启用 AI，走不到这里）
          * 每天最多调用几次：全局 max_calls_per_day 是**硬上限**，
            到了这个次数后，即使倍数规则还要求调用也不再调用。
            0 = 不限制（完全由倍数规则决定）。
            计数是**按站点**的（每个站点各自有上限）。

        另外连续 AI 失败达到 fail_threshold → 暂停该站点并推送告警
        （0 = 不限制，永不停用）。
        """
        today = self._today(now)
        if not cfg.ai.enabled:
            return ''

        # 停用的站点不消耗 AI。
        #
        # 为什么需要：「立即执行」可以强制跑某个站点，即使它已被停用
        # （用户就是靠这个手动重试的）。但停用本身就是"别再烧资源"的意思，
        # 而且界面与 README 都写着"暂停后不会再消耗 AI 额度"。
        # 实测过：停用站点连败数正好是 ai_after_failures 整数倍时点「立即」，
        # 会照样调用 AI —— 与文档矛盾，也白花钱。
        if not getattr(site, 'enabled', True):
            return '站点已停用，不调用 AI 分析'

        # 是否启用 AI：由全局 ai_after_failures 决定（0 = 不启用）
        if self._ai_after_failures(cfg) <= 0:
            return 'AI 分析已关闭（"每几次失败调用一次"设为 0）'

        # 每天调用次数的硬上限
        try:
            cap = int(getattr(cfg.ai, 'max_calls_per_day', 20))
        except (TypeError, ValueError):
            cap = 20
        if cap > 0:
            calls_today = state.ai_calls_today(site.id, today)
            if calls_today >= cap:
                return ('AI 分析已达每天上限（%d 次），不再调用；明天恢复'
                        % cap)

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

        # 计一次今天的调用（bump_ai_calls 内部按日期分桶，
        # 跨天自动归零，所以"每天上限"是天然按天重置的）
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
        # 附上"这次真正是哪个模型回答的"。
        # 服务端返回的 served 可能和请求的 requested 不同（旧名字会被静默映射），
        # 不写出来用户就无法确认自己付的是哪个模型的钱（用户明确问过这点）。
        served = str(getattr(suggestion, 'served_model', '') or '')
        asked = str(getattr(suggestion, 'requested_model', '') or '')
        if served:
            if asked and served.lower() != asked.lower():
                note += '｜模型：请求 %s，实际由 %s 回答（已被服务端映射）' % (
                    asked, served)
            else:
                note += '｜模型：%s' % served
        return note

    def _record_ai_failure(self, site: SiteConfig, cfg: AppConfig,
                           state: RuntimeState, error: str, now: int) -> None:
        """记一次 AI 失败；达到阈值就停用站点并推送告警。

        fail_threshold 同时表达"要不要停用"和"几次后停用"：
            0  = 不限制，站点永不因 AI 失败被停用（每天都失败也继续问）
            1+ = 连续失败这么多次后暂停该站点
        """
        site.state = dict(site.state or {})
        fails = int(site.state.get('ai_consecutive_failures') or 0) + 1
        site.state['ai_consecutive_failures'] = fails
        site.state['last_ai_error'] = error
        site.state['ai_failed_at'] = int(now)

        try:
            threshold = int(getattr(cfg.ai, 'fail_threshold', 2))
        except (TypeError, ValueError):
            threshold = 2
        if threshold <= 0:
            # 0 = 不限制连续失败次数：只记录，不停用
            return
        if fails < threshold:
            return

        disabled = False
        if site.enabled:
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
        # ⚠️ 绝不整包写回 cfg / state。
        #
        # 这两个对象是**几分钟前**读的快照（浏览器签到很慢）。整包写回会把
        # 期间用户做的一切覆盖掉，实测过的后果：
        #   * 刚保存的新 Cookie 消失（剩旧的）
        #   * 刚改的口令被回滚 → 旧口令重新可用
        #   * 刚吊销的令牌哈希被回滚 → 令牌重新有效（安全回归）
        #   * next_run_at 被回滚 → 同一天签到两次
        #   * AI 每日计数被回滚 → 上限失效，多烧 token
        # 所以只增量回写"运行期字段"：站点状态 + 本次跑过的调度信息。
        self.store.merge_site_runtime(targets)
        self._persist_state_delta(state, targets, now)
        # AI 补丁改了站点配置（如 need_browser / success_keywords），
        # 必须落盘，否则重启后又按旧配置签到。这里只回写**补丁允许的字段**，
        # 不动用户改过的其它设置（整包写回会把用户改动一起覆盖掉）。
        self.store.merge_site_patch(targets)
        return report

    def _persist_state_delta(self, state: RuntimeState, targets, now: int
                             ) -> None:
        """只增量写回 state 里"本轮真的动过"的部分。

        同样在锁内重读盘，避免覆盖别的执行体（调度线程/另一次「立即执行」）
        刚写进去的 next_run_at 与 ai_calls —— 那正是重复签到与
        AI 上限失效的来源。
        """
        def _mut(st: RuntimeState) -> bool:
            changed = False
            # 只更新本轮处理的站点（其余站点的排程可能是别人刚写的）
            for site in targets:
                sid = str(getattr(site, 'id', ''))
                if not sid:
                    continue
                if sid not in st.sites:
                    st.sites[sid] = {}
                src = (state.sites or {}).get(sid) or {}
                for k, v in src.items():
                    if st.sites[sid].get(k) != v:
                        st.sites[sid][k] = v
                        changed = True
            # AI 每日计数：按站点+日期合并，取**较大值**。
            # 取最大值而不是覆盖：别的执行体可能已经加过次数，
            # 覆盖会让"今天已用次数"倒退，上限就形同虚设。
            for sid, bucket in (state.ai_calls or {}).items():
                cur = st.ai_calls.get(sid) or {}
                if (bucket or {}).get('date') == cur.get('date'):
                    if int((bucket or {}).get('count') or 0) > int(
                            cur.get('count') or 0):
                        st.ai_calls[sid] = dict(bucket)
                        changed = True
                else:
                    st.ai_calls[sid] = dict(bucket or {})
                    changed = True
            if int(state.last_run_at or 0) > int(st.last_run_at or 0):
                st.last_run_at = int(state.last_run_at)
                changed = True
            if int(getattr(state, 'last_backup_at', 0) or 0) > int(
                    st.last_backup_at or 0):
                st.last_backup_at = int(state.last_backup_at)
                changed = True
            return changed

        self.store.update_state(_mut)
