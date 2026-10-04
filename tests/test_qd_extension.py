"""提取 web.py 里的 QD 扩展对接逻辑，在 Node 里真跑一遍。

为什么必须测这个：
    扩展用我们页面上的 data-site / data-domain 去查 Cookie：
        browser.cookies.getAll({url: data-site})
        browser.cookies.getAll({domain: data-domain})
    这两个值填错，扩展**不会报错**，只会返回空 —— 用户看到的是
    "点了没反应"，完全无从排查。所以必须把拼装逻辑钉死。

做法：把页面 JS 里的相关函数抽出来，喂一组 URL 进去，断言输出。
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent
WEB = ROOT / 'app' / 'web.py'
NODE = (r'C:\Users\xingh\.dsh\dsh-runtimes\dsh-primary-runtime'
        r'\dependencies\node\bin\node.exe')

# 需要从页面 JS 里抠出来的函数
WANTED = ('hostOf', 'rootDomain', 'qdButtonHtml', 'esc', 'markCookieImportStart')


def extract_page_js() -> str:
    text = WEB.read_text(encoding='utf-8')
    blocks = re.findall(r'<script[^>]*>(.*?)</script>', text, re.S)
    return '\n;\n'.join(blocks)


def extract_function(js: str, name: str) -> str:
    """按大括号配对抠出一个 function 定义。"""
    m = re.search(r'function\s+%s\s*\(' % re.escape(name), js)
    if not m:
        raise AssertionError('页面 JS 里找不到函数 %s' % name)
    start = m.start()
    i = js.index('{', m.end() - 1)
    depth = 0
    for j in range(i, len(js)):
        ch = js[j]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError('函数 %s 的大括号不配对' % name)


def run_in_node(cases):
    js = extract_page_js()
    parts = []
    for name in ('esc', 'hostOf', 'rootDomain'):
        parts.append(extract_function(js, name))
    # qdButtonHtml 依赖 esc/hostOf/rootDomain，单独放最后
    parts.append(extract_function(js, 'qdButtonHtml'))

    # 桩：esc 依赖的 DOM 无关，markCookieImportStart 不需要
    harness = '\n'.join(parts) + '''

var out = [];
var cases = JSON.parse(process.argv[2]);
for (var i = 0; i < cases.length; i++) {
  var c = cases[i];
  var host = hostOf(c.homepage);
  var btn = qdButtonHtml({id: c.id, name: c.name, homepage: c.homepage});
  out.push({
    homepage: c.homepage,
    host: host,
    root: rootDomain(host),
    btn: btn
  });
}
console.log(JSON.stringify(out));
'''
    tmp = ROOT / '_qdtest.js'
    tmp.write_text(harness, encoding='utf-8')
    try:
        r = subprocess.run([NODE, str(tmp), json.dumps(cases)],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace')
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    if r.returncode != 0:
        raise AssertionError('Node 执行失败：%s %s' % (r.stdout, r.stderr))
    return json.loads(r.stdout.strip())


CASES = [
    # (homepage, 期望的 host, 期望的 rootDomain)
    ('https://www.nodeseek.com/board', 'www.nodeseek.com', 'nodeseek.com'),
    ('https://www.v2ex.com/', 'www.v2ex.com', 'v2ex.com'),
    ('https://www.chiphell.com/forum.php', 'www.chiphell.com', 'chiphell.com'),
    ('https://nodeseek.com/', 'nodeseek.com', 'nodeseek.com'),
    ('https://a.b.example.com/x', 'a.b.example.com', 'example.com'),
    # 两段式后缀要保留三段，否则会算成 co.uk 这种无效域名
    ('https://www.example.co.uk/', 'www.example.co.uk', 'example.co.uk'),
    ('https://www.example.com.cn/', 'www.example.com.cn', 'example.com.cn'),
    ('https://www.example.com.hk/', 'www.example.com.hk', 'example.com.hk'),
    # 异常输入不能崩，也不能编出一个瞎域名
    ('', '', ''),
    ('not a url', '', ''),
    ('http://localhost:8080/', 'localhost', 'localhost'),
]


@unittest.skipUnless(os.path.exists(NODE), '本机没有 node，跳过')
class TestQdDomainLogic(unittest.TestCase):
    def test_host_and_root_domain(self):
        out = run_in_node([
            {'id': 'x', 'name': 'X', 'homepage': hp} for hp, _h, _r in CASES
        ])
        for (homepage, want_host, want_root), got in zip(CASES, out):
            with self.subTest(homepage=homepage):
                self.assertEqual(got['host'], want_host,
                                 'hostOf(%r) 不对' % homepage)
                self.assertEqual(got['root'], want_root,
                                 'rootDomain(%r) 不对' % homepage)

    def test_button_carries_required_attributes(self):
        """扩展只认 data-toggle="get-cookie"，且靠 data-site 取值。"""
        out = run_in_node([{
            'id': 'nodeseek',
            'name': 'NodeSeek 试试手气',
            'homepage': 'https://www.nodeseek.com/board',
        }])
        btn = out[0]['btn']
        self.assertIn('data-toggle="get-cookie"', btn)
        self.assertIn('data-site="https://www.nodeseek.com/board"', btn)
        self.assertIn('data-domain="nodeseek.com"', btn)
        self.assertIn('data-name="NodeSeek 试试手气"', btn)
        # 扩展在 click 事件里读属性，所以必须是个可点击元素。
        # 注意：这里刻意**不再**把 id 拼进 onclick 字符串 ——
        # onclick="markCookieImportStart('ID')" 是 JS 字符串上下文，
        # HTML 实体转义挡不住引号逃逸（浏览器会先解码再交给 JS），
        # 已改成 data-fact + data-sid 由事件委托处理。
        self.assertTrue(btn.startswith('<button'))
        self.assertIn('data-fact="markCookieImport"', btn)
        self.assertIn('data-sid="nodeseek"', btn)
        self.assertNotIn('onclick=', btn, '不该再把 id 拼进内联 onclick')

    def test_attributes_are_escaped(self):
        """站点名/地址里的引号必须转义，否则会破坏属性、按钮失效。"""
        out = run_in_node([{
            'id': 'x', 'name': 'A "quoted" name',
            'homepage': 'https://a.com/?q="x"',
        }])
        btn = out[0]['btn']
        # 不能出现未被转义的裸双引号切断属性
        self.assertNotIn('name" name', btn)
        self.assertIn('&quot;', btn)


if __name__ == '__main__':
    unittest.main(verbosity=2)
