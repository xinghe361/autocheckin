"""验证 NodeSeek 的响应分类（含 HTTP 500 + 已签到）以及积分提取。

用 mock 响应，不发真实请求也不消耗签到机会。
NodeSeek 的坑：
  * 应用层错误会配 **HTTP 500** 返回（QD 模板里 success_asserts 就写了 200/500），
    所以 5xx 不能被当成传输失败，必须读 body 再判定。
  * "今天已经签到过" 的响应同时含 '"success":false' 和 '"status":404' ——
    如果 fail 的关键词先命中就会误报失败并触发重试。
    所以 already 必须优先于 fail（classify_page 的既有优先级）。
"""
import sys
import unittest

sys.path.insert(0, '.')
from app.engine import run_http_flow                        # noqa: E402
from app.httpclient import HttpResponse                     # noqa: E402
from app.templates import BUILTIN                           # noqa: E402

TPL = [t for t in BUILTIN if t['id'] == 'nodeseek'][0]

SUCCESS = ('{"success":true,"message":"运气爆棚，恭喜你签到获得了7个鸡腿",'
           '"gain":7,"current":408}')
ALREADY = ('{"success":false,"message":"您今天已经签到过了","status":404}')
ALREADY2 = ('{"success":false,"message":"今天已经签到，请明天再来","status":404}')
BAD_COOKIE = '{"message":"USER NOT FOUND","status":404,"success":false}'
NEED_LOGIN = '{"message":"請先登入","status":404,"success":false}'


def run(status, text):
    def fetcher(method, url, data):
        return HttpResponse(status=status, text=text, url=url)
    return run_http_flow(
        fetcher, TPL['flow'],
        success_kw=list(TPL['success_keywords']),
        fail_kw=list(TPL['fail_keywords']),
        already_kw=list(TPL['already_keywords']))


class TestNodeSeekClassify(unittest.TestCase):
    def test_http_200_success(self):
        out = run(200, SUCCESS)
        self.assertTrue(out.success)
        self.assertEqual(out.hit, 'success')

    def test_already_signed_beats_fail_keywords(self):
        """已签到的响应也含 "success":false，必须判为 already 而不是失败。"""
        for text in (ALREADY, ALREADY2):
            with self.subTest(text=text[:40]):
                out = run(200, text)
                self.assertTrue(out.success, '已签到应算成功：%s' % out.message)
                self.assertEqual(out.hit, 'already')

    def test_http_500_with_json_body_is_read(self):
        """应用层错误会配 500 返回：必须读 body 才能判定。

        这条是 QD 模板里 success_asserts=['200','500'] 的由来。
        """
        out = run(500, BAD_COOKIE)
        self.assertFalse(out.success)
        self.assertEqual(out.hit, 'fail')
        # 轨迹里应保留真实的 500，便于排查
        self.assertTrue(any('500' in t for t in out.trace), out.trace)

    def test_http_500_already_signed(self):
        """500 + 已签到：仍要判成功（不能因为状态码就报失败）。"""
        out = run(500, ALREADY)
        self.assertTrue(out.success, out.message)
        self.assertEqual(out.hit, 'already')

    def test_not_logged_in_is_fail(self):
        out = run(500, NEED_LOGIN)
        self.assertFalse(out.success)
        self.assertEqual(out.hit, 'fail')

    def test_gain_is_extracted(self):
        """成功时应把鸡腿数提取出来，并显示在消息里（便于通知查看）。"""
        out = run(200, SUCCESS)
        self.assertEqual(out.vars.get('gain'), '7')
        self.assertEqual(out.vars.get('current'), '408')
        self.assertIn('7 个鸡腿', out.message)
        self.assertIn('408', out.message)

    def test_token_is_not_leaked_into_message(self):
        """内部令牌之类不该被打印到消息里。"""
        out = run(200, SUCCESS)
        self.assertNotIn('once', out.message)

    def test_gain_absent_on_failure(self):
        out = run(500, BAD_COOKIE)
        self.assertIsNone(out.vars.get('gain'))

    def test_flow_uses_post_with_random(self):
        urls = [s.get('url', '') for s in TPL['flow'] if s.get('action') == 'post']
        self.assertTrue(any('attendance' in u for u in urls), urls)
        self.assertTrue(any('random=true' in u for u in urls),
                        '「试试手气」用 random=true')

    def test_origin_and_referer_preset(self):
        """实测：缺 Origin/Referer 会 403（不是 Cloudflare 拦的）。"""
        h = {k.lower(): v for k, v in TPL['headers'].items()}
        self.assertEqual(h.get('origin'), 'https://www.nodeseek.com')
        self.assertEqual(h.get('referer'), 'https://www.nodeseek.com/board')


if __name__ == '__main__':
    unittest.main(verbosity=2)
