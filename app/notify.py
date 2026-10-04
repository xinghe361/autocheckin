"""通知：多渠道推送 + 按站点策略决定是否发送。

渠道（实测：直连 / 走代理）：
    pushplus   200  ✅ 可用
    Server酱   200  ✅ 可用
    企业微信   403  ← 需要把出口 IP 加进白名单
    Telegram   200  ✅ 可用（必须走代理）

策略：全部通知 / 仅失败 / 仅成功 / 自定义站点 / 不通知
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .httpclient import HttpConfig, HttpError, enc_json, normalize_proxy, request
from .models import (NOTIFY_ALL, NOTIFY_CUSTOM, NOTIFY_FAIL_ONLY, NOTIFY_NONE,
                     NOTIFY_SUCCESS_ONLY, PROXY_CUSTOM, PROXY_DIRECT,
                     PROXY_INHERIT, NotifyConfig)

CHANNEL_PUSHPLUS = 'pushplus'
# Server 酱已按要求从界面移除。这里保留常量，只为兼容旧配置里的
# serverchan_key 字段（不删数据，但不再作为可选渠道发送）。
CHANNEL_SERVERCHAN = 'serverchan'
CHANNEL_WECOM = 'wecom'
CHANNEL_TELEGRAM = 'telegram'

ALL_CHANNELS = (CHANNEL_PUSHPLUS, CHANNEL_WECOM, CHANNEL_TELEGRAM)


def channel_proxy(cfg: NotifyConfig, channel: str, global_proxy: str = '') -> str:
    """决定某个通知渠道该走的代理地址（空字符串 = 直连）。

    按用户要求：每个渠道可以单独选择走不走代理，没单独设置就跟随主代理。
    （原来中间还有一层"通知级代理"，用户指出那是多余的：
      已经有主代理，每个渠道又能独立设置，中间那层没有存在意义，已移除。）

      1) 该渠道有单独设置 → 用它（direct 则强制直连）
      2) 否则跟随主代理
    """
    override = (cfg.channel_proxy or {}).get(channel) or {}
    mode = (override.get('mode') or '').strip().lower()
    url = (override.get('url') or '').strip()

    if mode == PROXY_DIRECT:
        return ''
    if mode == PROXY_CUSTOM:
        return normalize_proxy(url)

    # 渠道未单独设置 → 跟随主代理
    return normalize_proxy(global_proxy)


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


def send_serverchan(key: str, title: str, content: str, proxy: str = '',
                    timeout: int = 20) -> str:
    # 兼容 sctapi.ftqq.com（实测可达）
    url = 'https://sctapi.ftqq.com/%s.send' % key
    body = enc_json({'title': title, 'desp': content})
    resp = request('POST', url, global_proxy=proxy, data=body,
                   cfg=HttpConfig(timeout=timeout,
                                  headers={'Content-Type': 'application/json'}))
    if not resp.ok:
        raise HttpError('http_error', 'Server酱 推送失败（HTTP %d）' % resp.status,
                        resp.text[:200])
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return 'Server酱 已推送'
    if data.get('code') not in (0, '0'):
        raise HttpError('serverchan_error',
                        'Server酱 返回错误：%s' % (data.get('message') or data.get('code')))
    return 'Server酱 已推送'


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

    注意：Server 酱已从界面移除，所以这里不再把它算作可用渠道
    （旧配置里若还留着 serverchan_key，也不会被发送）。
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
