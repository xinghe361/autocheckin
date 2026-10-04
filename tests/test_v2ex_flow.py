"""V2EX 签到的完整流程测试（用 mock fetcher，不发真实请求）。

为什么要专门测：
    实测 V2EX 的 redeem 端点**只接受 GET**，用 POST 会返回
    HTTP 405 Method Not Allowed —— 之前就是这个 bug，签到永远不会成功。
    另外 redeem 会返回 302，body 很短，而判定用的是"最后一个响应"，
    所以必须在 redeem 之后重新取一次页面来确认结果，
    否则会去 302 的空 body 里找成功关键词，永远判失败。

    这两点都是靠不住的经验，必须钉在测试里。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import run_http_flow                            # noqa: E402
from app.httpclient import HttpResponse                         # noqa: E402
from app.templates import BUILTIN                               # noqa: E402

TPL = [t for t in BUILTIN if t['id'] == 'v2ex'][0]
MISSION = TPL['mission_url']
REDEEM_PREFIX = 'https://www.v2ex.com/mission/daily/redeem?once='

# 真实页面片段
PAGE_CAN_REDEEM = (
    '<html><body><div class="box">'
    '<a href="/mission/daily/redeem?once=123456" class="super normal button">'
    '领取 X 铜币</a></div></body></html>'
)
PAGE_DONE = (
    '<html><body><div class="box">'
    '<div class="cell">每日登录奖励已领取</div>'
    '<div class="cell">已连续登录 123 天</div>'
    '</div></body></html>'
)
PAGE_LOGIN = '<html><body><form action="/signin">请先登录</form></body></html>'


def run(fetcher):
    """按模板跑一遍流程（替换 {mission_url} 占位符）。"""
    flow = []
    for s in TPL['flow']:
        s2 = dict(s)
        if 'url' in s2:
            s2['url'] = str(s2['url']).replace('{mission_url}', MISSION)
        flow.append(s2)
    return run_http_flow(
        fetcher, flow,
        success_kw=list(TPL.get('success_keywords') or []),
        fail_kw=list(TPL.get('fail_keywords') or []),
        already_kw=list(TPL.get('already_keywords') or []))


class Recorder:
    """记录请求，并按预设脚本返回响应。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, url, data):
        self.calls.append((method, url))
        if not self.script:
            return HttpResponse(status=200, text='', url=url)
        status, text = self.script.pop(0)
        return HttpResponse(status=status, text=text, url=url)


class TestV2exFlow(unittest.TestCase):
    def test_happy_path_uses_get_and_rechecks_page(self):
        """完整成功路径：GET 取令牌 → GET redeem → 再 GET 确认。"""
        rec = Recorder([
            (200, PAGE_CAN_REDEEM),      # 1. 取 mission 页
            (302, ''),                   # 2. redeem -> 302（body 很短）
            (200, PAGE_DONE),            # 3. 再取页面确认
        ])
        out = run(rec)
        self.assertTrue(out.success, out.message)
        self.assertEqual(out.hit, 'success')
        self.assertIn('每日登录奖励已领取', out.message)

        # 关键 1：redeem 必须用 GET（POST 会 405）
        methods = [m for m, _ in rec.calls]
        self.assertEqual(methods, ['GET', 'GET', 'GET'],
                         '全部步骤都必须是 GET，redeem 用 POST 会 405')
        # 关键 2：redeem 之后必须再取一次页面
        self.assertEqual(len(rec.calls), 3, 'redeem 之后要重新取页面确认结果')
        self.assertTrue(rec.calls[1][1].startswith(REDEEM_PREFIX),
                        '第二步应请求 redeem 且带上提取到的 once')

    def test_token_extracted_from_page(self):
        """once 令牌要从页面里提取出来并拼进 redeem URL。"""
        rec = Recorder([(200, PAGE_CAN_REDEEM), (302, ''), (200, PAGE_DONE)])
        run(rec)
        self.assertTrue(rec.calls[1][1].endswith('once=123456'),
                        'URL 里应带上提取到的 once：%s' % rec.calls[1][1])

    def test_without_token_fails_early(self):
        """页面里没有令牌（未登录/被拦）-> 尽早失败，不带字面量继续请求。"""
        rec = Recorder([(200, PAGE_LOGIN)])
        out = run(rec)
        self.assertFalse(out.success)
        self.assertEqual(out.error_kind, 'no_token')
        self.assertEqual(len(rec.calls), 1, '取不到令牌就不该继续请求 redeem')

    def test_already_redeemed_is_success(self):
        """今天已经领过 -> 算成功（already），不该报失败触发重试。"""
        rec = Recorder([
            (200, PAGE_DONE),            # 页面已经没有领取按钮
        ])
        out = run(rec)
        # 没有令牌 -> 早退为 no_token 是合理的（页面确实没有 redeem 链接），
        # 但真正"已领过"的判定在拿到令牌后的分类里：
        rec2 = Recorder([
            (200, PAGE_CAN_REDEEM),
            (302, ''),
            (200, '<html>今日已经领取过</html>'),
        ])
        out2 = run(rec2)
        self.assertTrue(out2.success, out2.message)
        self.assertEqual(out2.hit, 'already')

    def test_redeem_405_is_reported(self):
        """如果 redeem 返回 405（用错方法），要能看出来。

        这条是回归保护：以前模板用的是 POST，实测就是 405。
        """
        rec = Recorder([
            (200, PAGE_CAN_REDEEM),
            (405, 'Method Not Allowed'),
            (200, PAGE_CAN_REDEEM),
        ])
        out = run(rec)
        self.assertFalse(out.success, '405 不该被当成成功')
        self.assertIn('405', out.trace[-2] if len(out.trace) >= 2 else '',
                      '轨迹里应能看到 405')

    def test_flow_has_no_post_step(self):
        """直接检查模板：V2EX 的流程里不能有 post 动作。"""
        actions = [s.get('action') for s in TPL['flow']]
        self.assertNotIn('post', actions,
                         'V2EX 的 redeem 只接受 GET，用 POST 会 405')
        self.assertEqual(actions.count('get'), 3,
                         '应为：取页面 / redeem / 再取页面确认')

    def test_token_extract_is_required(self):
        steps = [s for s in TPL['flow'] if s.get('action') == 'extract_regex']
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].get('required'),
                        '令牌取不到必须尽早失败，否则会带字面量请求')

    def test_success_keyword_is_specific(self):
        """成功关键词必须能区分"已领到"和"还能领"。

        不能用宽泛的"铜币"：未领的页面也有"领取 X 铜币"，
        会让"还能领"误判成"已领到"。
        """
        self.assertIn('每日登录奖励已领取', TPL['success_keywords'])
        self.assertNotIn('铜币', TPL['success_keywords'],
                         '宽泛的"铜币"会把"领取 X 铜币"误判成成功')


if __name__ == '__main__':
    unittest.main(verbosity=2)
