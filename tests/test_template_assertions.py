"""内置模板的判定关键词，用【实测得到的真实响应】来固定。

为什么需要这个：
    判定是纯文本包含匹配，写关键词时很容易只覆盖一种格式。
    踩过的坑：NodeSeek 失效时返回 {"status":404,...}（无空格），
    而另一个响应可能是 "status": 404（有空格）；
    只写一种就会让"Cookie 失效"被判成别的东西。
    这类错误不会报异常，只会静默给出错误结论，所以必须用真实响应钉住。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import templates as T  # noqa: E402
from app.engine import classify_page  # noqa: E402


def tpl(sid):
    return T.template_by_id(sid)


class TestNodeSeekAssertions(unittest.TestCase):
    """NodeSeek 的真实响应（实测抓的原文，值已改成占位）。"""

    SUCCESS = '{"success":true,"message":"运气爆棚，恭喜你签到获得了7个鸡腿","gain":7,"current":408}'
    ALREADY = '{"success":false,"message":"你今天已经签到过了","status":404}'
    EXPIRED = '{"message":"USER NOT FOUND","status":404,"success":false}'
    EXPIRED_SPACED = '{"message":"请先登录","status": 404, "success": false}'
    NOT_LOGGED = '{"message":"請先登入","status":404,"success":false}'

    def test_success_response_is_success(self):
        t = tpl('nodeseek')
        kind, hit = classify_page(self.SUCCESS, t['success_keywords'],
                                  t['fail_keywords'], t.get('already_keywords'))
        self.assertEqual(kind, 'success', '签到成功应判成功（命中 %s）' % hit)

    def test_expired_cookie_detected(self):
        t = tpl('nodeseek')
        for body in (self.EXPIRED, self.EXPIRED_SPACED, self.NOT_LOGGED):
            with self.subTest(body=body):
                kind, hit = classify_page(body, t['success_keywords'],
                                          t['fail_keywords'],
                                          t.get('already_keywords'))
                self.assertEqual(kind, 'fail',
                                 'Cookie 失效应判失败（实际 %s / 命中 %s）'
                                 % (kind, hit))

    def test_already_signed_today_wins_over_fail(self):
        """"今天已签到"的响应里也带 status:404，但必须判 already 而不是 fail。

        这就是判定优先级存在的原因：already 要优先于 fail，
        否则会把"今天已签"误报成失败并触发重试。
        """
        t = tpl('nodeseek')
        kind, hit = classify_page(self.ALREADY, t['success_keywords'],
                                  t['fail_keywords'], t.get('already_keywords'))
        self.assertEqual(kind, 'already',
                         '已签到应判 already（实际 %s / 命中 %s）' % (kind, hit))

    def test_keywords_cover_both_spacing_styles(self):
        """不能只写一种空格风格 —— 这是实际踩过的坑。"""
        fails = tpl('nodeseek')['fail_keywords']
        self.assertIn('"status":404', fails)
        self.assertIn('"status": 404', fails)
        self.assertIn('"success":false', fails)
        self.assertIn('"success": false', fails)


class TestChiphellAssertions(unittest.TestCase):
    def test_logged_in_page_is_success(self):
        t = tpl('chiphell')
        body = '<a>token361</a> 积分: 639 | 用户组: 大天使 退出'
        kind, hit = classify_page(body, t['success_keywords'], t['fail_keywords'])
        self.assertEqual(kind, 'success', '登录页应判成功（命中 %s）' % hit)

    def test_logged_out_page_is_fail(self):
        t = tpl('chiphell')
        # 实测未登录页面里有"登录"入口，没有"用户组"
        body = '设为首页 / 收藏本站 / 账号 / 自动登录 / 找回密码 / 登录'
        kind, hit = classify_page(body, t['success_keywords'], t['fail_keywords'])
        self.assertNotEqual(kind, 'success',
                            '未登录页面不能判成功（命中 %s）' % hit)

    def test_success_keyword_is_user_group(self):
        self.assertIn('用户组', tpl('chiphell')['success_keywords'])


class TestV2exAssertions(unittest.TestCase):
    def test_redeem_success(self):
        t = tpl('v2ex')
        kind, _ = classify_page('每日登录奖励已领取，铜币 +8',
                                t['success_keywords'], t['fail_keywords'],
                                t.get('already_keywords'))
        self.assertIn(kind, ('success', 'already'))

    def test_not_logged_in(self):
        t = tpl('v2ex')
        kind, hit = classify_page('需要先登录才能领取', t['success_keywords'],
                                  t['fail_keywords'], t.get('already_keywords'))
        self.assertEqual(kind, 'fail', '未登录应判失败（命中 %s）' % hit)


class TestTemplateSanity(unittest.TestCase):
    """模板层面的基本约束：这些字段缺了会让签到静默失灵。"""

    def test_all_builtin_need_cookie_and_are_http(self):
        for t in T.BUILTIN:
            with self.subTest(site=t['id']):
                self.assertFalse(t['need_browser'],
                                 '%s 实测走纯 HTTP' % t['id'])
                self.assertTrue(t.get('flow'), '%s 必须有可执行的 flow' % t['id'])
                self.assertTrue(t.get('headers'),
                                '%s 应预置请求头（缺 Origin/Referer 会 403）' % t['id'])
                self.assertTrue(t.get('success_keywords'),
                                '%s 必须有成功判据' % t['id'])
                self.assertTrue(t.get('fail_keywords'),
                                '%s 必须有失败判据，否则失效时无法识别' % t['id'])

    def test_no_cookie_keyword_misuse(self):
        """success_keywords 里不能出现明显是失败含义的词。"""
        bad = ('未登录', '请先登录', '失效', '过期', '失败')
        for t in T.BUILTIN:
            for kw in (t.get('success_keywords') or []):
                if any(b in kw for b in bad):
                    self.fail('%s 的成功判据里含失败词：%s' % (t['id'], kw))


if __name__ == '__main__':
    unittest.main(verbosity=2)
