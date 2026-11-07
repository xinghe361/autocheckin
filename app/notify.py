"""通知：多渠道推送 + 按站点策略决定是否发送。

渠道（实测：直连 / 走代理）：
    pushplus   200  ✅ 可用
    Server酱   200  ✅ 可用
    企业微信   403  ← 需要把出口 IP 加进白名单
    Telegram   200  ✅ 可用（必须走代理）

策略：全部通知 / 仅失败 / 仅成功 / 自定义站点 / 不通知
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .httpclient import HttpConfig, HttpError, enc_json, normalize_proxy, request
from .models import (NOTIFY_ALL, NOTIFY_CUSTOM, NOTIFY_FAIL_ONLY, NOTIFY_NONE,
                     NOTIFY_SUCCESS_ONLY, PROXY_CUSTOM, PROXY_INHERIT,
                     NotifyConfig)

CHANNEL_PUSHPLUS = 'pushplus'
CHANNEL_WECOM = 'wecom'
CHANNEL_TELEGRAM = 'telegram'

ALL_CHANNELS = (CHANNEL_PUSHPLUS, CHANNEL_WECOM, CHANNEL_TELEGRAM)


def channel_proxy(cfg: NotifyConfig, channel: str, global_proxy: str = '') -> str:
    """决定某个通知渠道该走的代理地址（空字符串 = 直连）。

    每个渠道可以单独选择走不走代理。**默认直连** —— 通知渠道（pushplus /
    企业微信 / Telegram）大多在公网，跟着签到用的主代理反而容易失败
    （用户反馈过）。想跟随主代理就显式选「跟随主代理」。

      1) direct / 未设置 → 直连
      2) inherit → 跟随主代理
      3) custom → 用该渠道单独填的地址
    """
    override = (cfg.channel_proxy or {}).get(channel) or {}
    mode = (override.get('mode') or '').strip().lower()
    url = (override.get('url') or '').strip()

    if mode == PROXY_CUSTOM:
        return normalize_proxy(url)
    if mode == PROXY_INHERIT:
        return normalize_proxy(global_proxy)
    # 默认（含显式 direct）→ 直连
    return ''


@dataclass
class CheckinResult:
    """一次签到的结果，通知内容据此生成。"""

    site_id: str
    site_name: str
    success: bool
    message: str = ''
    # 网络错误的分类（timeout / tls_error / ...），成功时为空
    error_kind: str = ''
    # 第几次尝试（1 = 首次）
    attempt: int = 1
    duration_ms: int = 0
    at: int = field(default_factory=lambda: int(time.time()))
    # AI 分析给出的结论（若有）
    ai_note: str = ''
    # 这次签到走过的每一步（GET 某地址 -> 200 / 提取 xx / 命中关键词…）。
    # 引擎一直在记，但以前出了函数就丢了 —— 失败时只能看到一句结论，
    # 看不出是第几步、哪个地址出的问题。现在往上传，由 history 落盘。
    trace: List[str] = field(default_factory=list)
    # 失败页面的正文片段（已压缩 + 截断）。
    #
    # ⚠️ 刻意**不落历史、不进通知、不进任何接口**：它可能含用户名、未读消息、
    #    页面里的令牌等。唯一的去处是「AI 分析时附带页面片段」这个开关打开后
    #    的提示词（那边还会再脱敏一次）。
    page_excerpt: str = ''


def should_notify(result: CheckinResult, cfg: NotifyConfig) -> bool:
    """按全局策略 + 站点覆盖，决定这条结果要不要推送。"""
    mode = (cfg.mode or NOTIFY_ALL).lower()
    if mode == NOTIFY_NONE:
        return False
    if mode == NOTIFY_ALL:
        return True
    if mode == NOTIFY_FAIL_ONLY:
        return not result.success
    if mode == NOTIFY_SUCCESS_ONLY:
        return result.success
    if mode == NOTIFY_CUSTOM:
        return result.site_id in (cfg.custom_sites or [])
    # 未知策略按"全部"处理，避免静默丢通知
    return True


def format_message(result: CheckinResult, title_prefix: str = '自动签到') -> str:
    """生成人类可读的通知正文（纯文本，各渠道通用）。"""
    head = '✅ 成功' if result.success else '❌ 失败'
    lines = [
        '%s：%s' % (head, result.site_name),
    ]
    if result.message:
        lines.append(result.message)
    if not result.success and result.error_kind:
        from .netutil import describe_network_error
        lines.append('原因：%s' % describe_network_error(result.error_kind))
    if result.attempt > 1:
        lines.append('第 %d 次尝试' % result.attempt)
    if result.duration_ms:
        lines.append('耗时 %.1fs' % (result.duration_ms / 1000.0))
    if result.ai_note:
        lines.append('AI 分析：%s' % result.ai_note)
    lines.append('时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(result.at)))
    return '\n'.join(lines)


def title_of(result: CheckinResult, prefix: str = '自动签到') -> str:
    return '%s · %s · %s' % (prefix, result.site_name, '成功' if result.success else '失败')


# ---------------------------------------------------------------- 各渠道


def send_pushplus(token: str, title: str, content: str, proxy: str = '',
                  timeout: int = 20) -> str:
    body = enc_json({
        'token': token,
        'title': title,
        'content': content,
        'template': 'txt',
    })
    resp = request('POST', 'https://www.pushplus.plus/send', global_proxy=proxy,
                   data=body,
                   cfg=HttpConfig(timeout=timeout,
                                  headers={'Content-Type': 'application/json'}))
    if not resp.ok:
        raise HttpError('http_error', 'pushplus 推送失败（HTTP %d）' % resp.status,
                        resp.text[:200])
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise HttpError('bad_response', 'pushplus 返回不是合法 JSON', str(e)) from e
    if data.get('code') not in (200, '200'):
        raise HttpError('pushplus_error',
                        'pushplus 返回错误：%s' % data.get('msg') or data.get('code'))
    return 'pushplus 已推送'


def send_wecom(webhook: str, title: str, content: str, proxy: str = '',
               timeout: int = 20) -> str:
    body = enc_json({
        'msgtype': 'markdown',
        'markdown': {'content': '**%s**\n%s' % (title, content)},
    })
    resp = request('POST', webhook, global_proxy=proxy, data=body,
                   cfg=HttpConfig(timeout=timeout,
                                  headers={'Content-Type': 'application/json'}))
    if not resp.ok:
        # 403 通常是出口 IP 不在白名单
        hint = '（企业微信要求把出口 IP 加入白名单）' if resp.status == 403 else ''
        raise HttpError('http_error',
                        '企业微信 推送失败（HTTP %d）%s' % (resp.status, hint),
                        resp.text[:200])
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return '企业微信 已推送'
    if data.get('errcode') not in (0, '0'):
        raise HttpError('wecom_error', '企业微信 返回错误：%s' % data.get('errmsg'))
    return '企业微信 已推送'


def send_telegram(bot_token: str, chat_id: str, title: str, content: str,
                  proxy: str = '', timeout: int = 20) -> str:
    if not proxy:
        raise HttpError('no_proxy',
                        'Telegram 必须走代理：请在设置里填写代理地址')
    url = 'https://api.telegram.org/bot%s/sendMessage' % bot_token
    body = enc_json({
        'chat_id': chat_id,
        'text': '%s\n\n%s' % (title, content),
        'disable_web_page_preview': True,
    })
    resp = request('POST', url, global_proxy=proxy, data=body,
                   cfg=HttpConfig(timeout=timeout,
                                  headers={'Content-Type': 'application/json'}))
    if not resp.ok:
        raise HttpError('http_error', 'Telegram 推送失败（HTTP %d）' % resp.status,
                        resp.text[:200])
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return 'Telegram 已推送'
    if not data.get('ok'):
        raise HttpError('telegram_error',
                        'Telegram 返回错误：%s' % data.get('description'))
    return 'Telegram 已推送'


def configured_channels(cfg: NotifyConfig) -> List[str]:
    """根据已填写的凭据，推断哪些渠道可用。

    只看凭据是否齐全 —— 有三个渠道就是三个，不多不少。
    """
    out = []
    if cfg.pushplus_token:
        out.append(CHANNEL_PUSHPLUS)
    if cfg.wecom_webhook:
        out.append(CHANNEL_WECOM)
    if cfg.tg_bot_token and cfg.tg_chat_id:
        out.append(CHANNEL_TELEGRAM)
    return out


def send_all(cfg: NotifyConfig, result: CheckinResult,
             proxy: str = '') -> Dict[str, str]:
    """把结果推送到所有已配置的渠道。

    返回 {渠道: 结果描述}；单个渠道失败不影响其它渠道。
    每个渠道使用自己的代理策略（channel_proxy 决定）。
    """
    title = title_of(result)
    content = format_message(result)
    report: Dict[str, str] = {}

    want = set(cfg.channels or []) or set(configured_channels(cfg))
    for ch in ALL_CHANNELS:
        if ch not in want:
            continue
        if ch not in configured_channels(cfg):
            report[ch] = '跳过：未配置'
            continue
        ch_proxy = channel_proxy(cfg, ch, proxy)
        try:
            if ch == CHANNEL_PUSHPLUS:
                report[ch] = send_pushplus(cfg.pushplus_token, title, content, ch_proxy)
            elif ch == CHANNEL_WECOM:
                report[ch] = send_wecom(cfg.wecom_webhook, title, content, ch_proxy)
            elif ch == CHANNEL_TELEGRAM:
                report[ch] = send_telegram(cfg.tg_bot_token, cfg.tg_chat_id,
                                           title, content, ch_proxy)
        except HttpError as e:
            report[ch] = '失败：%s' % e
        except Exception as e:  # noqa: BLE001
            report[ch] = '失败：%s' % e
    return report


def notify(result: CheckinResult, cfg: NotifyConfig, proxy: str = '') -> Dict[str, str]:
    """策略 + 发送的完整入口。"""
    if not should_notify(result, cfg):
        return {}
    return send_all(cfg, result, proxy)


def has_any_channel(cfg: NotifyConfig) -> bool:
    return bool(configured_channels(cfg))


def send_alert(cfg: NotifyConfig, title: str, content: str,
               proxy: str = '') -> Dict[str, str]:
    """发送"系统级告警"，**不受按站点通知策略限制**。

    用于这类必须让用户知道的事件：
      * AI 分析失败次数达到上限，已自动停止该站点签到
      * 站点被自动停用

    只要配置了任一通知渠道就会推送（用户要求："如果有通知渠道的话"）。
    每个渠道仍按自己的代理设置走。
    """
    report: Dict[str, str] = {}
    channels = configured_channels(cfg)
    if not channels:
        return report

    for ch in channels:
        ch_proxy = channel_proxy(cfg, ch, proxy)
        try:
            if ch == CHANNEL_PUSHPLUS:
                report[ch] = send_pushplus(cfg.pushplus_token, title, content, ch_proxy)
            elif ch == CHANNEL_WECOM:
                report[ch] = send_wecom(cfg.wecom_webhook, title, content, ch_proxy)
            elif ch == CHANNEL_TELEGRAM:
                report[ch] = send_telegram(cfg.tg_bot_token, cfg.tg_chat_id,
                                           title, content, ch_proxy)
        except HttpError as e:
            report[ch] = '失败：%s' % e
        except Exception as e:  # noqa: BLE001
            report[ch] = '失败：%s' % e
    return report


def digest_message(summary: Dict[str, Any]) -> tuple:
    """每日汇总的正文本（history.day_summary 的结果）。

    站点多了以后逐条推送会刷屏，用户索性就把通知关了 —— 一天一条
    "成功几个、失败的是谁、为什么"反而更有用。
    """
    from .reasons import classify

    date = str(summary.get('date') or '')
    title = '%s 签到日报' % (date or '今天')
    sites = summary.get('sites') or []
    if not sites:
        return title, '这一天没有任何签到记录（可能是没启用任何站点）。'

    ok = int(summary.get('ok') or 0)
    fail = int(summary.get('fail') or 0)
    lines = ['成功 %d 次 · 失败 %d 次' % (ok, fail)]

    bad = [s for s in sites if s.get('fail')]
    if bad:
        lines.append('')
        lines.append('有问题的站点：')
        for s in bad[:10]:
            label = ''
            if s.get('reason'):
                label = classify(str(s.get('reason')), '').get('label') or ''
            msg = str(s.get('last_msg') or '').strip()
            detail = label or msg or '失败'
            # 有归类时把原始消息附在后面，没归类就只显示原始消息
            if label and msg and msg != label:
                detail = '%s（%s）' % (label, msg[:40])
            lines.append('· %s：%s' % (s.get('site_name') or s.get('site_id'),
                                      detail))
        if len(bad) > 10:
            lines.append('· …还有 %d 个' % (len(bad) - 10))

    good = [s for s in sites if s.get('ok') and not s.get('fail')]
    if good:
        lines.append('')
        names = [str(s.get('site_name') or s.get('site_id')) for s in good[:20]]
        tail = '' if len(good) <= 20 else ' 等 %d 个' % len(good)
        lines.append('正常：' + '、'.join(names) + tail)

    return title, '\n'.join(lines)


def format_ai_failure_alert(site_name: str, ai_failures: int,
                            last_error: str, disabled: bool) -> str:
    """AI 分析失败告警正文。"""
    lines = [
        '站点「%s」的签到连续失败，且自动分析也连续失败 %d 次。' % (site_name, ai_failures),
    ]
    if last_error:
        lines.append('最近一次分析失败原因：%s' % last_error)
    if disabled:
        lines.append('已自动停止该站点的签到，避免继续无意义地重试。')
        lines.append('请到「站点」页检查配置或手动改回启用状态。')
    else:
        lines.append('（已按设置保留该站点，未自动停用）')
    lines.append('时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    return '\n'.join(lines)
