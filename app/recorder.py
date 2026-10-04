"""步骤录制：把用户在页面上的操作记成可回放的签到流程。

用户需求原文：
    「给新网站也留下新增的按钮，点击新建之后记录我签到的操作并记录保存以后就这样签到」

实现思路（浏览器内注入脚本 + 服务端整理）：
    注入的脚本监听 click / input / change / 导航事件，把每次操作按
    {action, target, value, timeout_ms} 上报；服务端用 StepRecorder 归一化：
      * 为元素生成稳健的 CSS 选择器（优先 id，其次有意义的属性，最后 nth-of-type 路径）
      * 合并同一元素的连续输入，避免每个字符记一步
      * 相邻重复点击去重
      * 丢弃明显无意义的操作（body/html 点击、纯滚动）

这样录制出来的 steps 与 browser.py 的 run_steps 使用同一套动作语义。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .models import (ACTION_CLICK, ACTION_EXTRACT, ACTION_FILL, ACTION_GOTO,
                     ACTION_WAIT, ACTION_WAIT_FOR, Step)

# 录制时可识别的原始事件类型
EV_CLICK = 'click'
EV_INPUT = 'input'
EV_CHANGE = 'change'
EV_NAVIGATE = 'navigate'
EV_SUBMIT = 'submit'

# 元素上没有意义的标签：点了也不算"签到操作"
IGNORED_TAGS = {'html', 'body', 'head', 'script', 'style', 'meta', 'link'}

# 选择器里优先使用的属性（越靠前越稳定）
ATTR_PRIORITY = ('data-testid', 'data-id', 'data-name', 'name', 'aria-label',
                 'placeholder', 'title')


def css_escape(value: str) -> str:
    """CSS 标识符转义（够用即可，避免选择器语法错误）。"""
    if value is None:
        return ''
    s = str(value)
    # 只保留可安全出现在选择器里的字符，其余转成 \XX 形式
    out = []
    for ch in s:
        if re.match(r'[A-Za-z0-9_\-]', ch):
            out.append(ch)
        else:
            out.append('\\' + ch)
    return ''.join(out)


def build_selector(info: Dict[str, Any]) -> str:
    """根据元素的描述信息生成 CSS 选择器。

    info 由页面脚本上报，形如：
        {'tag':'button', 'id':'sign', 'attrs':{'data-id':'x','name':'n'},
         'classes':['btn','primary'], 'text':'签到', 'path':['div','button'],
         'index':2, 'index_total':3}
    """
    if not info or not isinstance(info, dict):
        return ''
    tag = (info.get('tag') or '').lower()
    if not tag:
        return ''

    # 1) id 最稳
    el_id = info.get('id')
    if el_id:
        return '#%s' % css_escape(el_id)

    attrs = info.get('attrs') or {}
    for a in ATTR_PRIORITY:
        v = attrs.get(a)
        if v:
            return '%s[%s="%s"]' % (tag, a, str(v).replace('"', '\\"'))

    # 2) 有意义的 class（排除明显的工具类）
    classes = [c for c in (info.get('classes') or [])
               if c and not re.match(r'^(ng|css|jsx|sc|_|is-|has-)', c)
               and len(c) > 2]
    if classes:
        return '%s.%s' % (tag, css_escape(classes[0]))

    # 3) 退化为带序号的结构路径
    path = info.get('path') or []
    if path:
        base = ' > '.join(path)
        total = int(info.get('index_total') or 0)
        index = int(info.get('index') or 0)
        if total > 1 and index > 0:
            return '%s:nth-of-type(%d)' % (base, index)
        return base

    if tag:
        return tag
    return ''


@dataclass
class RawEvent:
    """录制到的原始事件。"""

    kind: str
    at: float = field(default_factory=time.time)
    url: str = ''
    info: Dict[str, Any] = field(default_factory=dict)
    value: str = ''


class StepRecorder:
    """把原始事件流归一化成可回放的 Step 列表。"""

    def __init__(self, include_wait: bool = True, wait_ms: int = 1500):
        self.include_wait = include_wait
        self.wait_ms = wait_ms
        self.events: List[RawEvent] = []

    # ------------------------------------------------------------------
    def add(self, event: RawEvent) -> None:
        self.events.append(event)

    def add_dict(self, d: Dict[str, Any]) -> None:
        self.events.append(RawEvent(
            kind=(d.get('kind') or d.get('type') or '').lower(),
            at=float(d.get('at') or time.time()),
            url=d.get('url') or '',
            info=d.get('info') or {},
            value=d.get('value') or '',
        ))

    # ------------------------------------------------------------------
    def to_steps(self, start_url: str = '') -> List[Step]:
        """归一化输出。"""
        steps: List[Step] = []
        if start_url:
            steps.append(Step(action=ACTION_GOTO, target=start_url,
                              timeout_ms=45000))

        last_url = start_url
        last_fill_selector = ''
        last_click_selector = ''

        for ev in self.events:
            kind = ev.kind

            if kind == EV_NAVIGATE:
                url = ev.url or ev.value
                if url and url != last_url:
                    steps.append(Step(action=ACTION_GOTO, target=url,
                                      timeout_ms=45000))
                    last_url = url
                    last_fill_selector = ''
                    last_click_selector = ''
                continue

            selector = build_selector(ev.info)
            tag = (ev.info.get('tag') or '').lower()
            if tag in IGNORED_TAGS:
                continue

            if kind in (EV_INPUT, EV_CHANGE):
                if not selector:
                    continue
                if selector == last_fill_selector and steps and \
                        steps[-1].action == ACTION_FILL:
                    # 同一输入框连续输入 → 覆盖上一步，不记成每字符一步
                    steps[-1].value = ev.value
                else:
                    steps.append(Step(action=ACTION_FILL, target=selector,
                                      value=ev.value, timeout_ms=15000))
                    last_fill_selector = selector
                continue

            if kind in (EV_CLICK, EV_SUBMIT):
                if not selector:
                    continue
                # 相邻重复点击去重
                if selector == last_click_selector and steps and \
                        steps[-1].action == ACTION_CLICK:
                    continue
                if self.include_wait:
                    steps.append(Step(action=ACTION_WAIT, timeout_ms=self.wait_ms,
                                      optional=True, note='等待页面响应'))
                steps.append(Step(action=ACTION_CLICK, target=selector,
                                  timeout_ms=20000))
                last_click_selector = selector
                last_fill_selector = ''
                continue

        return steps

    # ------------------------------------------------------------------
    def to_json(self, start_url: str = '') -> str:
        return json.dumps([s.to_dict() for s in self.to_steps(start_url)],
                          ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(text: str, start_url: str = '') -> List[Step]:
        data = json.loads(text or '[]')
        steps = [Step.from_dict(x) for x in data if isinstance(x, dict)]
        if start_url and not any(s.action == ACTION_GOTO for s in steps):
            steps.insert(0, Step(action=ACTION_GOTO, target=start_url))
        return steps


def merge_steps(existing: List[Step], new: List[Step]) -> List[Step]:
    """把新录制的步骤合并进已有流程（用于"再录一次补上漏掉的步骤"）。"""
    if not existing:
        return list(new)
    if not new:
        return list(existing)
    # 去掉新步骤里与已有开头重复的 goto
    if new and new[0].action == ACTION_GOTO and existing and \
            existing[0].action == ACTION_GOTO and new[0].target == existing[0].target:
        new = new[1:]
    return list(existing) + list(new)


# ------------------------------------------------------- 注入到页面的脚本

RECORDER_SCRIPT = r"""
(function () {
  if (window.__autocheckinRecorder) return 'already';
  window.__autocheckinRecorder = true;
  var lastUrl = location.href;

  function selectorInfo(el) {
    if (!el || !el.tagName) return null;
    var info = { tag: el.tagName.toLowerCase(), id: el.id || '',
                 classes: [], attrs: {}, path: [], index: 0, index_total: 0 };
    if (el.className && typeof el.className === 'string') {
      info.classes = el.className.split(/\s+/).filter(Boolean).slice(0, 5);
    }
    ['data-testid','data-id','data-name','name','aria-label','placeholder','title','type']
      .forEach(function (a) {
        var v = el.getAttribute && el.getAttribute(a);
        if (v) info.attrs[a] = v;
      });
    // 结构路径（最多 4 层）
    var parts = [], node = el, depth = 0;
    while (node && node.tagName && depth < 4) {
      var t = node.tagName.toLowerCase();
      if (t === 'html' || t === 'body') break;
      parts.unshift(t);
      node = node.parentElement;
      depth++;
    }
    info.path = parts;
    // 同级同标签中的序号
    if (el.parentElement) {
      var sibs = Array.prototype.filter.call(el.parentElement.children, function (c) {
        return c.tagName === el.tagName;
      });
      info.index_total = sibs.length;
      info.index = sibs.indexOf(el) + 1;
    }
    return info;
  }

  function send(kind, el, value) {
    var payload = { kind: kind, at: Date.now() / 1000, url: location.href,
                    info: el ? selectorInfo(el) : null, value: value || '' };
    try { window.__autocheckinReport(payload); } catch (e) {}
  }

  document.addEventListener('click', function (e) {
    send('click', e.target, '');
  }, true);

  document.addEventListener('input', function (e) {
    send('input', e.target, e.target && e.target.value);
  }, true);

  document.addEventListener('change', function (e) {
    send('change', e.target, e.target && e.target.value);
  }, true);

  document.addEventListener('submit', function (e) {
    send('submit', e.target, '');
  }, true);

  // 单页应用的路由变化
  setInterval(function () {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      send('navigate', null, lastUrl);
    }
  }, 800);

  return 'installed';
})();
"""
