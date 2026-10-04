"""签到调度引擎（纯逻辑，无外部依赖，便于单测）。

需求对应：
  * 每个站点可选「每天固定时刻 + ±1 小时内随机偏移」
  * 可选「以上次签到成功时刻为基准」：今天 03:21 成功 → 明天 03:21+随机
  * 签到失败可配置重试次数与重试间隔
  * 连续失败达到阈值 → 触发 DeepSeek 分析

时间统一用「本地时区的时间戳（秒）」表示，避免 naive/aware 混用出错。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# 随机偏移上限：需求写的是「±1 小时之内」
JITTER_MAX_SECONDS = 3600

# 调度模式
MODE_DAILY = 'daily'          # 每天固定时刻
MODE_SUCCESS_BASED = 'success_based'  # 以上次成功时刻为基准

# 一次签到尝试的结果
RESULT_SUCCESS = 'success'
RESULT_FAILED = 'failed'


@dataclass
class SiteSchedule:
    """单个站点的调度配置与运行状态。"""

    site_id: str

    # --- 配置 ---
    mode: str = MODE_DAILY
    # 每天固定时刻（本地时间），mode=MODE_DAILY 时使用
    daily_hour: int = 3
    daily_minute: int = 0

    # 是否启用随机偏移
    jitter_enabled: bool = False
    # 偏移范围（秒），默认 ±1 小时；可在 [-JITTER_MAX_SECONDS, JITTER_MAX_SECONDS] 内配置
    jitter_seconds: int = JITTER_MAX_SECONDS

    # 失败重试
    retry_enabled: bool = True
    retry_count: int = 3            # 最多重试几次
    retry_interval_minutes: int = 30  # 每次重试间隔

    # 连续失败多少次后交给 DeepSeek 分析
    ai_after_failures: int = 3
    ai_enabled: bool = True

    # --- 状态（持久化） ---
    last_success_at: Optional[int] = None     # 上次签到成功的时间戳
    last_attempt_at: Optional[int] = None     # 上次尝试的时间戳
    consecutive_failures: int = 0             # 连续失败次数（成功后清零）
    retries_done: int = 0                     # 本轮已重试次数
    ai_analyzed_for_streak: int = 0           # 已经为哪一档连败触发过 AI，避免重复触发


def clamp_jitter(seconds: int) -> int:
    """把偏移量限制在 1 小时以内，并保证非负。"""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        s = JITTER_MAX_SECONDS
    if s < 0:
        s = 0
    return min(s, JITTER_MAX_SECONDS)


def apply_jitter(base_ts: int, site: SiteSchedule, rng: random.Random) -> int:
    """在基准时间上叠加随机偏移（未启用则原样返回）。

    偏移取 [-jitter, +jitter] 内的均匀整数秒。
    """
    if not site.jitter_enabled:
        return int(base_ts)
    j = clamp_jitter(site.jitter_seconds)
    if j == 0:
        return int(base_ts)
    delta = rng.randint(-j, j)
    return int(base_ts) + delta


def next_daily_time(now: int, site: SiteSchedule, rng: random.Random) -> int:
    """模式一：每天固定时刻（+可选随机偏移）。

    规则：
      * 基准是「今天/明天的 daily_hour:daily_minute」
      * 叠加随机偏移后，如果结果已经过去，就顺延到明天同一基准再叠加偏移
      * 结果一定 > now
    """
    base_today = _at_local_time(now, site.daily_hour, site.daily_minute)
    candidate = apply_jitter(base_today, site, rng)
    if candidate > now:
        return candidate
    # 今天这个点已经过了（或加偏移后落在过去），顺延一天
    base_tomorrow = base_today + 86400
    return apply_jitter(base_tomorrow, site, rng)


def next_success_based_time(now: int, site: SiteSchedule, rng: random.Random) -> int:
    """模式二：以上次签到成功时刻为基准，每 24 小时一次（+可选随机偏移）。

    例：今天 03:21 成功 → 下次 明天 03:21 + 随机偏移。
    若从未成功过，则退化为「从现在起 24 小时 + 偏移」。
    """
    if site.last_success_at:
        base = int(site.last_success_at) + 86400
    else:
        base = int(now) + 86400

    candidate = apply_jitter(base, site, rng)
    # 偏移可能把它拉到过去（例如上次成功在 23 小时前、偏移 -1h）；
    # 这种情况下按 24 小时步进继续顺延，直到落到未来，保证不会立即重复触发。
    guard = 0
    while candidate <= now and guard < 400:
        base += 86400
        candidate = apply_jitter(base, site, rng)
        guard += 1
    return candidate


def next_run_at(now: int, site: SiteSchedule, rng: Optional[random.Random] = None) -> int:
    """计算下一次该签到的时间戳。"""
    rng = rng or random.Random()
    if site.mode == MODE_SUCCESS_BASED:
        return next_success_based_time(now, site, rng)
    return next_daily_time(now, site, rng)


def next_retry_at(now: int, site: SiteSchedule) -> Optional[int]:
    """签到失败后，下一次重试的时间；不能再重试则返回 None。

    重试计数语义：on_failure 会先把本次失败计入 retries_done，所以这里用
    `retries_done <= retry_count` 判断。这样可以保证 retry_count=N 时
    恰好重试 N 次（1 次正常尝试 + N 次重试）。
    """
    if not site.retry_enabled:
        return None
    if site.retries_done > max(0, int(site.retry_count)):
        return None
    interval = max(1, int(site.retry_interval_minutes)) * 60
    return int(now) + interval


def should_ask_ai(site: SiteSchedule) -> bool:
    """连续失败是否达到了需要 AI 分析的程度。

    语义：在 ai_after_failures 的**整数倍**处各分析一次。
    例如阈值 3 → 在连败 3、6、9… 次时分析。

    为什么不"每多失败一次就分析"：
    站点若持续坏掉（连败 4、5、6…），逐次分析会天天烧 token 却给不出新结论。
    按倍数走既能有限次尝试，又能随连败加重再要一次新建议。
    """
    if not site.ai_enabled:
        return False
    threshold = max(1, int(site.ai_after_failures))
    if site.consecutive_failures < threshold:
        return False
    if site.consecutive_failures % threshold != 0:
        return False
    # 同一档位只分析一次
    return site.ai_analyzed_for_streak != site.consecutive_failures


def on_attempt(site: SiteSchedule, now: int) -> None:
    """记录一次「尝试」发生。"""
    site.last_attempt_at = int(now)


def on_success(site: SiteSchedule, now: int) -> None:
    """签到成功：清零连败与重试计数，记录成功时刻。"""
    site.last_success_at = int(now)
    site.consecutive_failures = 0
    site.retries_done = 0
    site.ai_analyzed_for_streak = 0


def on_failure(site: SiteSchedule) -> None:
    """签到失败：连败 +1，重试计数 +1。"""
    site.consecutive_failures += 1
    site.retries_done += 1


def mark_ai_analyzed(site: SiteSchedule) -> None:
    """标记这一档连败已经做过 AI 分析。"""
    site.ai_analyzed_for_streak = site.consecutive_failures


def plan_next(site: SiteSchedule, now: int, succeeded: bool,
              rng: Optional[random.Random] = None) -> int:
    """一次签到结束后，决定下一次什么时候跑。

    成功 → 按调度模式排下一次正常签到（并清空重试）
    失败 → 优先按重试策略排重试；重试用尽则回到正常调度
    """
    on_attempt(site, now)
    if succeeded:
        on_success(site, now)
        return next_run_at(now, site, rng)

    on_failure(site)
    retry_at = next_retry_at(now, site)
    if retry_at is not None:
        return retry_at
    # 重试用尽：回到正常节奏，同时把重试计数清零，给下一轮留出额度
    site.retries_done = 0
    return next_run_at(now, site, rng)


def _at_local_time(ts: int, hour: int, minute: int) -> int:
    """把时间戳所在「本地日期」的 hour:minute 换回时间戳。"""
    d = datetime.fromtimestamp(ts)
    target = d.replace(hour=int(hour) % 24, minute=int(minute) % 60,
                       second=0, microsecond=0)
    return int(target.timestamp())
