"""recorder 单测：选择器生成与事件归一化（不需要浏览器）。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import recorder as RC  # noqa: E402
from app.models import (ACTION_CLICK, ACTION_FILL, ACTION_GOTO,  # noqa: E402
                        ACTION_WAIT, Step)


def ev(kind, **kw):
    return RC.RawEvent(kind=kind, **kw)


class TestBuildSelector(unittest.TestCase):
    def test_id_wins(self):
        self.assertEqual(
            RC.build_selector({'tag': 'button', 'id': 'sign',
                               'attrs': {'name': 'x'}, 'classes': ['btn']}),
            '#sign')

    def test_id_needs_escaping(self):
        got = RC.build_selector({'tag': 'div', 'id': 'a b:c'})
        self.assertTrue(got.startswith('#'))
        # 空格与冒号必须被反斜杠转义，否则选择器语法错误
        self.assertIn('\\ ', got)
        self.assertIn('\\:', got)
        self.assertNotIn('a b:c', got)

    def test_data_testid_priority(self):
        got = RC.build_selector({'tag': 'button',
                                 'attrs': {'data-testid': 'checkin', 'name': 'n'}})
        self.assertEqual(got, 'button[data-testid="checkin"]')

    def test_name_attribute(self):
        got = RC.build_selector({'tag': 'input', 'attrs': {'name': 'username'}})
        self.assertEqual(got, 'input[name="username"]')

    def test_class_fallback_skips_utility_classes(self):
        got = RC.build_selector({'tag': 'a', 'classes': ['ng-star', 'signin-btn']})
        self.assertEqual(got, 'a.signin-btn')

    def test_path_fallback_with_index(self):
        got = RC.build_selector({'tag': 'button', 'path': ['div', 'button'],
                                 'index': 2, 'index_total': 3})
        self.assertEqual(got, 'div > button:nth-of-type(2)')

    def test_path_without_index_when_unique(self):
        got = RC.build_selector({'tag': 'button', 'path': ['div', 'button'],
                                 'index': 1, 'index_total': 1})
        self.assertEqual(got, 'div > button')

    def test_tag_only_fallback(self):
        self.assertEqual(RC.build_selector({'tag': 'button'}), 'button')

    def test_empty_info(self):
        self.assertEqual(RC.build_selector({}), '')
        self.assertEqual(RC.build_selector(None), '')

    def test_attribute_value_quotes_escaped(self):
        got = RC.build_selector({'tag': 'input', 'attrs': {'name': 'a"b'}})
        self.assertIn('\\"', got)


class TestRecorderNormalization(unittest.TestCase):
    def test_goto_then_click(self):
        r = RC.StepRecorder()
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'sign'}))
        steps = r.to_steps('https://a.b/')
        self.assertEqual(steps[0].action, ACTION_GOTO)
        self.assertEqual(steps[0].target, 'https://a.b/')
        self.assertTrue(any(s.action == ACTION_CLICK and s.target == '#sign'
                            for s in steps))

    def test_ignores_body_and_html_clicks(self):
        r = RC.StepRecorder()
        r.add(ev(RC.EV_CLICK, info={'tag': 'body', 'id': 'x'}))
        r.add(ev(RC.EV_CLICK, info={'tag': 'html'}))
        steps = r.to_steps()
        self.assertEqual([s for s in steps if s.action == ACTION_CLICK], [])

    def test_consecutive_typing_becomes_one_fill(self):
        """逐字符输入不能被记成每字符一步。"""
        r = RC.StepRecorder()
        for i, ch in enumerate('hello'):
            r.add(ev(RC.EV_INPUT, info={'tag': 'input', 'id': 'user'},
                     value='hello'[:i + 1]))
        steps = r.to_steps()
        fills = [s for s in steps if s.action == ACTION_FILL]
        self.assertEqual(len(fills), 1, '连续输入应合并为一步：%r' % fills)
        self.assertEqual(fills[0].value, 'hello')

    def test_fill_then_new_field_creates_second_fill(self):
        r = RC.StepRecorder()
        r.add(ev(RC.EV_INPUT, info={'tag': 'input', 'id': 'user'}, value='a'))
        r.add(ev(RC.EV_INPUT, info={'tag': 'input', 'id': 'pw'}, value='b'))
        fills = [s for s in r.to_steps() if s.action == ACTION_FILL]
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[0].target, '#user')
        self.assertEqual(fills[1].target, '#pw')

    def test_duplicate_adjacent_clicks_deduped(self):
        r = RC.StepRecorder()
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'sign'}))
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'sign'}))
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'sign'}))
        clicks = [s for s in r.to_steps() if s.action == ACTION_CLICK]
        self.assertEqual(len(clicks), 1)

    def test_distinct_clicks_kept(self):
        r = RC.StepRecorder()
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'a'}))
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'b'}))
        clicks = [s for s in r.to_steps() if s.action == ACTION_CLICK]
        self.assertEqual(len(clicks), 2)

    def test_wait_inserted_before_click_by_default(self):
        r = RC.StepRecorder()
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'a'}))
        steps = r.to_steps()
        self.assertTrue(any(s.action == ACTION_WAIT for s in steps))

    def test_wait_can_be_disabled(self):
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'a'}))
        steps = r.to_steps()
        self.assertFalse(any(s.action == ACTION_WAIT for s in steps))

    def test_navigate_creates_goto(self):
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_NAVIGATE, url='https://a.b/checkin'))
        steps = r.to_steps('https://a.b/')
        gotos = [s.target for s in steps if s.action == ACTION_GOTO]
        self.assertIn('https://a.b/', gotos)
        self.assertIn('https://a.b/checkin', gotos)

    def test_repeated_navigate_to_same_url_ignored(self):
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_NAVIGATE, url='https://a.b/'))
        steps = r.to_steps('https://a.b/')
        gotos = [s for s in steps if s.action == ACTION_GOTO]
        self.assertEqual(len(gotos), 1, '同地址重复导航不该重复记')

    def test_submit_treated_as_click(self):
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_SUBMIT, info={'tag': 'form', 'id': 'f'}))
        clicks = [s for s in r.to_steps() if s.action == ACTION_CLICK]
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0].target, '#f')

    def test_events_without_selector_skipped(self):
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_CLICK, info={}))
        self.assertEqual([s for s in r.to_steps() if s.action == ACTION_CLICK], [])

    def test_click_resets_fill_coalescing(self):
        """点击后再回到同一个输入框，应该重新记一步（不能覆盖之前那次填写）。"""
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_INPUT, info={'tag': 'input', 'id': 'q'}, value='one'))
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'go'}))
        r.add(ev(RC.EV_INPUT, info={'tag': 'input', 'id': 'q'}, value='two'))
        fills = [s for s in r.to_steps() if s.action == ACTION_FILL]
        self.assertEqual(len(fills), 2, '点击后应重新计一步')
        self.assertEqual([f.value for f in fills], ['one', 'two'])

    def test_add_dict_parses_payload(self):
        r = RC.StepRecorder(include_wait=False)
        r.add_dict({'kind': 'click', 'url': 'https://a.b/',
                    'info': {'tag': 'button', 'id': 'x'}})
        clicks = [s for s in r.to_steps() if s.action == ACTION_CLICK]
        self.assertEqual(clicks[0].target, '#x')

    def test_add_dict_accepts_type_key(self):
        r = RC.StepRecorder(include_wait=False)
        r.add_dict({'type': 'click', 'info': {'tag': 'button', 'id': 'y'}})
        self.assertEqual(len([s for s in r.to_steps() if s.action == ACTION_CLICK]), 1)

    def test_realistic_signin_sequence(self):
        """模拟一次真实签到：打开 → 点签到 → 可能弹验证 → 再点。"""
        r = RC.StepRecorder()
        r.add(ev(RC.EV_CLICK, info={'tag': 'a', 'id': 'checkin-btn'}))
        r.add(ev(RC.EV_NAVIGATE, url='https://a.b/mission'))
        r.add(ev(RC.EV_CLICK, info={'tag': 'button',
                                    'attrs': {'data-testid': 'confirm'}}))
        steps = r.to_steps('https://a.b/')
        actions = [s.action for s in steps]
        self.assertEqual(actions[0], ACTION_GOTO)
        self.assertIn(ACTION_CLICK, actions)
        self.assertIn(ACTION_GOTO, actions[1:])
        targets = [s.target for s in steps if s.action == ACTION_CLICK]
        self.assertIn('#checkin-btn', targets)
        self.assertIn('button[data-testid="confirm"]', targets)


class TestJsonRoundTrip(unittest.TestCase):
    def test_to_json_and_back(self):
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_CLICK, info={'tag': 'button', 'id': 'x'}))
        text = r.to_json('https://a.b/')
        self.assertIn('"action"', text)
        steps = RC.StepRecorder.from_json(text)
        self.assertEqual(steps[0].action, ACTION_GOTO)
        self.assertEqual(steps[1].action, ACTION_CLICK)
        self.assertEqual(steps[1].target, '#x')

    def test_from_json_adds_goto_if_missing(self):
        steps = RC.StepRecorder.from_json(
            json.dumps([{'action': 'click', 'target': '#a'}]), 'https://a.b/')
        self.assertEqual(steps[0].action, ACTION_GOTO)

    def test_from_json_does_not_duplicate_goto(self):
        steps = RC.StepRecorder.from_json(
            json.dumps([{'action': 'goto', 'target': 'https://a.b/'}]), 'https://a.b/')
        self.assertEqual(len([s for s in steps if s.action == ACTION_GOTO]), 1)

    def test_from_json_empty(self):
        self.assertEqual(RC.StepRecorder.from_json(''), [])
        self.assertEqual(RC.StepRecorder.from_json('[]'), [])

    def test_round_trip_preserves_fields(self):
        r = RC.StepRecorder(include_wait=False)
        r.add(ev(RC.EV_INPUT, info={'tag': 'input', 'id': 'u'}, value='alice'))
        steps = RC.StepRecorder.from_json(r.to_json())
        fill = [s for s in steps if s.action == ACTION_FILL][0]
        self.assertEqual(fill.value, 'alice')
        self.assertEqual(fill.timeout_ms, 15000)


class TestMergeSteps(unittest.TestCase):
    def test_merge_drops_duplicate_leading_goto(self):
        a = [Step(action=ACTION_GOTO, target='https://a.b/'),
             Step(action=ACTION_CLICK, target='#x')]
        b = [Step(action=ACTION_GOTO, target='https://a.b/'),
             Step(action=ACTION_CLICK, target='#y')]
        merged = RC.merge_steps(a, b)
        gotos = [s for s in merged if s.action == ACTION_GOTO]
        self.assertEqual(len(gotos), 1)
        self.assertEqual(len([s for s in merged if s.action == ACTION_CLICK]), 2)

    def test_merge_keeps_different_goto(self):
        a = [Step(action=ACTION_GOTO, target='https://a.b/')]
        b = [Step(action=ACTION_GOTO, target='https://c.d/')]
        merged = RC.merge_steps(a, b)
        self.assertEqual(len([s for s in merged if s.action == ACTION_GOTO]), 2)

    def test_merge_empty_sides(self):
        a = [Step(action=ACTION_CLICK, target='#x')]
        self.assertEqual(RC.merge_steps([], a), a)
        self.assertEqual(RC.merge_steps(a, []), a)


class TestInjectedScript(unittest.TestCase):
    def test_script_has_no_tag_closer(self):
        """注入脚本里若含结束标签字面量，会提前闭合脚本元素（踩过这个坑）。"""
        closer = '<' + '/script>'
        self.assertNotIn(closer, RC.RECORDER_SCRIPT)

    def test_script_listens_to_key_events(self):
        for e in ('click', 'input', 'change', 'submit'):
            self.assertIn("'%s'" % e, RC.RECORDER_SCRIPT)
        self.assertIn('navigate', RC.RECORDER_SCRIPT)

    def test_script_is_guarded_against_double_install(self):
        self.assertIn('__autocheckinRecorder', RC.RECORDER_SCRIPT)


if __name__ == '__main__':
    unittest.main(verbosity=2)
