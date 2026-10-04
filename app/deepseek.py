"""DeepSeek 失败分析：连续失败达到阈值后，请 AI 给出新的签到方案。

用户需求原文：
    「连续失败 N 次后调用 deepseek 的 api 进行分析怎么成功签到，分析签到成功后
      并保存之后就按修改后的签到」

因此本模块的产出是**可保存的配置补丁**（新的选择器/URL/关键词/步骤），
交由上层写回站点配置；是否自动应用由 AiConfig.auto_apply 控制。

设计要点：
  * 严格解析 JSON；模型爱把 JSON 包在 markdown 代码块里，必须容错
  * 补丁只允许改"安全字段"，不能凭空改站点 id / 凭据
  * 有每日调用上限，避免连续失败时无限烧 token
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .httpclient import HttpConfig, HttpError, enc_json, request
from .models import AiConfig, SiteConfig
from .notify import CheckinResult

DEEPSEEK_DEFAULT_BASE = 'https://api.deepseek.com'
DEEPSEEK_DEFAULT_MODEL = 'deepseek-chat'

# 补丁里允许出现的字段（白名单，防止 AI 改坏关键配置）
ALLOWED_PATCH_FIELDS = {
    'homepage', 'need_browser', 'verify_ssl',
    'success_keywords', 'fail_keywords', 'steps',
    'daily_hour', 'daily_minute', 'jitter_enabled', 'jitter_seconds',
    'retry_enabled', 'retry_count', 'retry_interval_minutes',
}

SYSTEM_PROMPT = (
    '你是一个自动化签到脚本的排障专家。用户有一个每日自动签到系统，某个站点连续失败。'
    '请根据提供的失败信息、页面片段和当前配置，分析失败原因，并给出**可执行的配置修改建议**。\n'
    '只返回一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。格式：\n'
    '{\n'
    '  "analysis": "对失败原因的中文分析，简洁",\n'
    '  "confidence": 0-100 的整数，表示你对结论的把握,\n'
    '  "patch": { 只包含需要修改的字段 },\n'
    '  "reason": "为什么这样改"\n'
    '}\n'
    'patch 可用字段：homepage, need_browser, verify_ssl, success_keywords, '
    'fail_keywords, steps, daily_hour, daily_minute, jitter_enabled, jitter_seconds, '
    'retry_enabled, retry_count, retry_interval_minutes。\n'
    'steps 是数组，每步形如 {"action":"goto|click|fill|wait|wait_for|assert_text",'
    '"target":"CSS 选择器或 URL","value":"填写内容","timeout_ms":15000,"optional":false}。\n'
    '如果无法确定原因，patch 返回空对象 {} 并把 confidence 设为较低值。'
)


@dataclass
class AiSuggestion:
    """一次 AI 分析的结果。"""

    ok: bool
    analysis: str = ''
    confidence: int = 0
    patch: Dict[str, Any] = field(default_factory=dict)
    reason: str = ''
    error: str = ''
    # 实际发给模型的提示词长度与耗时，便于排查
    prompt_chars: int = 0
    duration_ms: int = 0
    raw: str = ''


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里稳健地抠出 JSON 对象。

    模型常见的几种不听话形式：
        1) 纯 JSON
        2) ```json ... ``` 代码块
        3) JSON 前后带解释文字
    """
    if not text:
        return None
    s = text.strip()

    # 1) 去掉 markdown 代码块围栏
    fence = re.search(r'```(?:json)?\s*([\s\S]*?)```', s, re.IGNORECASE)
    if fence:
        s = fence.group(1).strip()

    # 2) 直接解析
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass

    # 3) 截取第一个 { 到最后一个 } 再试
    start = s.find('{')
    end = s.rfind('}')
    if start >= 0 and end > start:
        candidate = s[start:end + 1]
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            pass

        # 4) 轻微修复：去掉尾随逗号
        fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
        try:
            obj = json.loads(fixed)
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            pass
    return None


def sanitize_patch(patch: Any) -> Dict[str, Any]:
    """只保留白名单字段，并做基本类型校验。"""
    if not isinstance(patch, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in patch.items():
        if k not in ALLOWED_PATCH_FIELDS:
            continue
        if k in ('success_keywords', 'fail_keywords'):
            if isinstance(v, list):
                out[k] = [str(x) for x in v if str(x).strip()][:30]
        elif k == 'steps':
            if isinstance(v, list):
                steps = []
                for s in v[:40]:
                    if not isinstance(s, dict):
                        continue
                    action = str(s.get('action', '')).strip()
                    if not action:
                        continue
                    steps.append({
                        'action': action,
                        'target': str(s.get('target', '')),
                        'value': str(s.get('value', '')),
                        'timeout_ms': int(s.get('timeout_ms') or 15000),
                        'optional': bool(s.get('optional', False)),
                        'note': str(s.get('note', '')),
                    })
                out[k] = steps
        elif k in ('need_browser', 'verify_ssl', 'jitter_enabled', 'retry_enabled'):
            out[k] = bool(v)
        elif k == 'homepage':
            out[k] = str(v)
        elif k in ('daily_hour', 'daily_minute', 'jitter_seconds',
                   'retry_count', 'retry_interval_minutes'):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
    # 数值范围收敛，避免 AI 给出离谱值
    if 'daily_hour' in out:
        out['daily_hour'] = max(0, min(23, out['daily_hour']))
    if 'daily_minute' in out:
        out['daily_minute'] = max(0, min(59, out['daily_minute']))
    if 'jitter_seconds' in out:
        out['jitter_seconds'] = max(0, min(3600, out['jitter_seconds']))
    if 'retry_count' in out:
        out['retry_count'] = max(0, min(20, out['retry_count']))
    if 'retry_interval_minutes' in out:
        out['retry_interval_minutes'] = max(1, min(1440, out['retry_interval_minutes']))
    return out


def build_prompt(site: SiteConfig, result: CheckinResult,
                 page_excerpt: str = '', trace: Optional[List[str]] = None) -> str:
    """构造给模型的用户提示。"""
    lines = [
        '## 站点',
        '名称：%s' % site.name,
        '主页：%s' % (site.homepage or '(空)'),
        '模板：%s' % (site.template or '(自定义)'),
        '需要浏览器：%s' % ('是' if site.need_browser else '否'),
        '放宽证书校验：%s' % ('是' if not site.verify_ssl else '否'),
        '',
        '## 当前配置',
        '成功关键词：%s' % (site.success_keywords or []),
        '失败关键词：%s' % (site.fail_keywords or []),
        '步骤：%s' % json.dumps([s.to_dict() for s in site.steps], ensure_ascii=False)
        if site.steps else '步骤：(无，使用内置模板)',
        '',
        '## 失败情况',
        '连续失败次数：%s' % ((site.state or {}).get('consecutive_failures', '?')),
        '错误信息：%s' % (result.message or '(无)'),
        '错误类型：%s' % (result.error_kind or '(无)'),
        '尝试次数：%s' % result.attempt,
    ]
    if trace:
        lines += ['', '## 执行轨迹', '\n'.join(trace[:30])]
    if page_excerpt:
        # 截断，避免提示词过长
        lines += ['', '## 页面内容片段（可能含线索）', page_excerpt[:4000]]
    lines += ['', '请分析失败原因并给出配置修改建议，只返回 JSON。']
    return '\n'.join(lines)


def _extract_content(resp_json: Dict[str, Any]) -> str:
    """兼容 OpenAI 风格的返回结构。"""
    choices = resp_json.get('choices') or []
    if choices:
        msg = choices[0].get('message') or {}
        content = msg.get('content')
        if isinstance(content, str):
            return content
    # 少数情况直接给 content
    if isinstance(resp_json.get('content'), str):
        return resp_json['content']
    return ''


def analyze(site: SiteConfig, result: CheckinResult, ai: AiConfig,
            api_key: str, proxy: str = '', page_excerpt: str = '',
            trace: Optional[List[str]] = None,
            transport=None) -> AiSuggestion:
    """调用 DeepSeek 分析失败原因。

    transport: 可注入的发送函数 (url, body_bytes, headers, timeout, proxy) -> HttpResponse
               默认走真实网络；单测注入假的即可。
    """
    started = time.time()
    if not ai.enabled:
        return AiSuggestion(False, error='AI 分析已关闭')
    if not api_key:
        return AiSuggestion(False, error='未配置 DeepSeek API Key')

    prompt = build_prompt(site, result, page_excerpt, trace)
    base = (ai.base_url or DEEPSEEK_DEFAULT_BASE).rstrip('/')
    url = base + '/chat/completions'
    body = enc_json({
        'model': ai.model or DEEPSEEK_DEFAULT_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.2,
        'stream': False,
    })
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer %s' % api_key,
    }

    try:
        if transport is not None:
            resp = transport(url, body, headers, ai.timeout, proxy)
        else:
            resp = request('POST', url, global_proxy=proxy, data=body,
                           cfg=HttpConfig(timeout=ai.timeout, headers=headers))
        if not resp.ok:
            return AiSuggestion(False,
                                error='DeepSeek 返回 HTTP %d：%s' % (resp.status,
                                                                   (resp.text or '')[:200]),
                                prompt_chars=len(prompt),
                                duration_ms=int((time.time() - started) * 1000))
        data = resp.json()
    except HttpError as e:
        return AiSuggestion(False, error=str(e), prompt_chars=len(prompt),
                            duration_ms=int((time.time() - started) * 1000))
    except Exception as e:  # noqa: BLE001
        return AiSuggestion(False, error='调用 DeepSeek 失败：%s' % e,
                            prompt_chars=len(prompt),
                            duration_ms=int((time.time() - started) * 1000))

    content = _extract_content(data)
    if not content:
        return AiSuggestion(False, error='DeepSeek 返回内容为空',
                            prompt_chars=len(prompt),
                            duration_ms=int((time.time() - started) * 1000))

    obj = extract_json(content)
    if obj is None:
        return AiSuggestion(False, error='AI 返回的不是合法 JSON',
                            raw=content[:500], prompt_chars=len(prompt),
                            duration_ms=int((time.time() - started) * 1000))

    try:
        confidence = int(obj.get('confidence') or 0)
    except (TypeError, ValueError):
        confidence = 0

    return AiSuggestion(
        ok=True,
        analysis=str(obj.get('analysis') or ''),
        confidence=max(0, min(100, confidence)),
        patch=sanitize_patch(obj.get('patch')),
        reason=str(obj.get('reason') or ''),
        prompt_chars=len(prompt),
        duration_ms=int((time.time() - started) * 1000),
        raw=content[:2000],
    )


def apply_patch(site: SiteConfig, patch: Dict[str, Any]) -> List[str]:
    """把补丁写回站点配置，返回实际改动的字段名列表。

    只处理白名单字段，并且 steps 会被转换成 Step 对象。
    """
    from .models import Step

    changed: List[str] = []
    clean = sanitize_patch(patch)
    for k, v in clean.items():
        if k == 'steps':
            steps = [Step.from_dict(x) for x in v]
            if steps != site.steps:
                site.steps = steps
                changed.append(k)
            continue
        if getattr(site, k, None) != v:
            setattr(site, k, v)
            changed.append(k)
    return changed


def should_analyze(site: SiteConfig, ai: AiConfig, calls_today: int = 0) -> bool:
    """是否应该对这次失败调用 AI。"""
    if not ai.enabled:
        return False
    if not getattr(site, 'ai_enabled', True):
        return False
    if calls_today >= max(1, int(ai.max_calls_per_day or 20)):
        return False
    from .schedule import should_ask_ai
    return should_ask_ai(site.to_schedule())
