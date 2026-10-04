"""真实 HTTP 冒烟测试：起服务、拉页面、走几个接口（不需要浏览器）。

用法: python smoke_web.py
"""
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import http.cookiejar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import secrets as S
from app import service as SV
from app import web as WB

data_dir = tempfile.mkdtemp(prefix='autocheckin_smoke_')
svc = SV.Service(data_dir, version='1.0.0-smoke', env_key=S.generate_key(),
                 proxy='')
app = WB.WebApp(svc, version='1.0.0-smoke')
httpd = WB.serve(app, host='127.0.0.1', port=0, block=False)
port = httpd.server_address[1]
base = 'http://127.0.0.1:%d' % port
# 用 CookieJar 保存会话 Cookie；直连不经代理
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPCookieProcessor(jar))

fails = []
def check(name, ok, extra=''):
    print(('  [OK]   ' if ok else '  [FAIL] ') + name + ((' -> ' + extra) if extra and not ok else ''))
    if not ok:
        fails.append(name)

def get(path):
    """GET 会把 4xx/5xx 也当正常返回，方便断言"该被拒"。"""
    try:
        with opener.open(base + path, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                                      # noqa: BLE001
        return 0, str(e).encode('utf-8')

def post(path, payload):
    req = urllib.request.Request(base + path,
                                data=json.dumps(payload).encode('utf-8'),
                                headers={'Content-Type': 'application/json'},
                                method='POST')
    try:
        with opener.open(req, timeout=10) as r:
            raw = r.read().decode('utf-8')
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8')
        try:
            return e.code, (json.loads(raw) if raw else {})
        except ValueError:
            return e.code, {'raw': raw[:200]}
    except Exception as e:                                      # noqa: BLE001
        return 0, {'error': str(e)}

try:
    print('服务地址：%s\n' % base)

    # --- 认证与访问控制 ---
    print('认证与访问控制：')
    code, _ = post('/api/settings', {'proxy': 'http://attacker:1'})
    check('未登录时改设置被拒', code == 403, '实际 HTTP %s' % code)
    code, _ = post('/api/site/delete', {'id': 'v2ex'})
    check('未登录时删站点被拒', code == 403, '实际 HTTP %s' % code)
    st, _ = get('/api/sites')
    check('未登录时读站点被拒', st == 403, '实际 HTTP %s' % st)
    st, _ = get('/api/settings')
    check('未登录时读设置被拒', st == 403, '实际 HTTP %s' % st)
    st, _ = get('/healthz')
    check('健康检查仍可匿名访问', st == 200, '')

    st, _ = get('/api/auth/state')
    check('状态接口匿名可用且提示需要初始化',
          st == 200 and json.loads(_).get('needs_setup') is True, _[:120])

    code, body = post('/api/auth/setup', {'password': 'smoke-test-pw'})
    check('可完成口令初始化', code == 200, str(body)[:120])
    check('初始化后已下发会话 Cookie', len(jar) > 0, '')
    check('初始化响应体不含口令或令牌明文',
          'smoke-test-pw' not in json.dumps(body), str(body)[:160])

    st, _ = get('/api/sites')
    check('登录后可正常读取', st == 200, 'HTTP %s' % st)

    code, _ = post('/api/auth/login', {'password': 'wrong-password'})
    check('错误口令被拒', code == 401, str(_)[:120])

    st, body = get('/')
    html = body.decode('utf-8')
    check('首页可访问', st == 200)
    check('首页含三个标签页', all(t in html for t in ('tab-sites', 'tab-add', 'tab-settings')))
    check('首页含新增站点按钮文案', '开始录制' in html and '结束并保存' in html)
    check('首页含告警测试按钮', '测试"分析失败"告警' in html or '分析失败' in html)
    check('首页含 AI 失败阈值输入', 'set_aifail' in html and 'set_aiauto' in html)
    check('首页含登录界面', 'tab-login' in html and 'doLogin' in html)

    # ---- 承接 QD 的 get-cookies 扩展 ----
    check('页面挂了扩展消息监听', 'cookieRaw' in html and 'listenQdExtension' in html)
    check('页面含扩展识别的按钮生成逻辑',
          'data-toggle=\\"get-cookie' in html or "data-toggle=\"get-cookie" in html)
    check('页面区分扩展消息与无关消息', "d.info !== 'cookieRaw'" in html)
    check('自动导入复用现有会话（不需要配对码）',
          '/api/pair/redeem' not in html)   # 页面不该再去换脚本令牌

    st, body = get('/autocheckin-cookie.user.js?host=nas.local:28999')
    js = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body)
    check('油猴脚本可下载', st == 200 and 'UserScript' in js)
    check('油猴脚本注入了正确的容器地址',
          'http://nas.local:28999' in js, js[:160])
    check('油猴脚本无占位符残留',
          not any(p in js for p in ('__ORIGIN__', '__MATCHES__', '__SITES__')))

    # ---- 界面：正在编辑时不能被自动刷新冲掉 ----
    # 用户反馈"还没填完就刷新，填不了"。根因是 30 秒定时 refreshAll()，
    # 而它会重渲染整个站点卡片，把表单里的内容替换掉。
    check('页面有"正在编辑"保护标记', '_formDirty' in html and 'formHasEdits' in html)
    check('自动刷新会跳过未保存的表单', '检测到你正在编辑' in html)
    # 自动刷新默认关闭：站点数据只在"用户操作"或"调度跑完"时才变，
    # 而调度一般凌晨跑，填表单时定时刷新只会打断填写。
    check('自动刷新默认关闭',
          'AUTO_REFRESH_KEY' in html and 'autoRefreshMs' in html
          and "'0'" in html)
    check('自动刷新频率存在浏览器本地（不进后端配置）',
          'localStorage' in html and 'applyAutoRefresh' in html)
    check('设置页有自动刷新开关', 'set_autoref' in html
          and 'toggleAutoRefresh' in html)
    check('不再是无条件定时刷新',
          'setInterval(function(){\n  if($(\'mainBody\').className === \'\') refreshAll();\n}, 60000);'
          not in html)

    # ---- 代理：全局"直连/走代理" + 站点级三选一 ----
    check('设置页有全局联网方式单选', 'name="set_pmode"' in html
          and 'value="direct"' in html and 'value="custom"' in html)
    check('设置页按模式显隐代理地址框', 'set_proxyBox' in html and 'swGlobalProxy' in html)
    check('站点表单有站点级代理三选一',
          "value=\\\"inherit\\\"" in html or 'value="inherit"' in html)
    check('站点表单按模式显隐独立代理地址', 'swSiteProxy' in html)

    # 刷新时不该重建 DOM（这是"动不动就刷新"的根治）
    check('列表用结构签名判断是否需要重建',
          'sitesSignature' in html and '_sitesSig' in html)

    # ---- AI 防烧 token：每天尝试/调用上限 ----
    check('设置页有每日尝试上限开关', 'set_dailylimit' in html and 'swAiLimits' in html)
    check('设置页可配每天尝试次数', 'set_maxattempt' in html)
    check('设置页可配每天问 AI 次数', 'set_aiperday' in html)
    check('设置页可配全局每日总量上限', 'set_aiglobal' in html)
    st, body = get('/api/settings')
    stt = json.loads(body) if isinstance(body, bytes) else body
    ai = stt.get('ai') or {}
    for k in ('daily_limit_enabled', 'max_attempts_per_day', 'ai_calls_per_day',
              'global_max_calls_per_day'):
        check('设置接口回传 %s' % k, k in ai, str(ai)[:140])

    # 站点列表要带上"今天的问题"信息，界面才能提示为什么不再试
    st, body = get('/api/sites')
    one = (json.loads(body)['sites'] or [{}])[0]
    check('站点列表回传 day_problem', 'day_problem' in one, str(one)[:140])
    check('站点列表回传 day_problem_reason', 'day_problem_reason' in one)

    st, body = get('/api/settings')
    stt = json.loads(body) if isinstance(body, bytes) else body
    check('设置接口回传 proxy_mode', 'proxy_mode' in stt, str(stt)[:120])

    st, body = get('/api/sites')
    one = (json.loads(body)['sites'] or [{}])[0]
    check('站点列表回传 proxy_mode', 'proxy_mode' in one, str(one)[:120])
    check('站点列表回传 proxy_url', 'proxy_url' in one)

    # 用 HTML 解析器确认脚本闭合正常
    from html.parser import HTMLParser
    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.d = 0
            self.chunks = []
            self.buf = []
        def handle_starttag(self, tag, attrs):
            if tag == 'script':
                self.d += 1
                self.buf = []
        def handle_endtag(self, tag):
            if tag == 'script':
                self.chunks.append(''.join(self.buf))
                self.d -= 1
        def handle_data(self, data):
            if self.d:
                self.buf.append(data)
    p = P(); p.feed(html)
    check('script 标签成对闭合', p.d == 0, 'depth=%d' % p.d)
    check('脚本块内无提前闭合', all('<' + '/script>' not in c for c in p.chunks))

    st, body = get('/healthz')
    check('健康检查', st == 200 and json.loads(body)['ok'])

    st, body = get('/api/overview')
    ov = json.loads(body)
    check('概览接口', st == 200 and ov['site_count'] == 3)

    st, body = get('/api/meta')
    meta = json.loads(body)
    check('元数据含三个内置站点',
          sorted(t['id'] for t in meta['templates']) == ['chiphell', 'nodeseek', 'v2ex'])
    check('元数据含四个通知渠道',
          set(meta['channels']) == {'pushplus', 'serverchan', 'wecom', 'telegram'})
    check('元数据含代理模式', set(meta['proxy_modes']) == {'inherit', 'direct', 'custom'})

    st, body = get('/api/sites')
    check('站点列表', st == 200 and len(json.loads(body)['sites']) == 3)

    st, body = get('/api/record/script.js')
    check('录制脚本可下载', st == 200 and b'__autocheckinRecorder' in body)

    # 写入设置：含凭据，然后确认不会被明文回显
    post('/api/settings', {
        'proxy': 'http://10.0.0.1:7890',
        'notify': {'channels': ['telegram', 'wecom'], 'proxy_mode': 'custom',
                   'proxy_url': 'http://n:1',
                   'channel_proxy': {'wecom': {'mode': 'direct', 'url': ''}},
                   'tg_bot_token': 'SECRET-BOT', 'tg_chat_id': '123',
                   'wecom_webhook': 'https://qyapi.weixin.qq.com/SECRET'},
        'ai': {'api_key': 'sk-SECRET', 'fail_threshold': 3, 'auto_disable': True},
        'webdav': {'url': 'https://dav.example.com/dav/', 'password': 'WD-SECRET'},
    })
    st, body = get('/api/settings')
    settings_text = body.decode('utf-8')
    check('设置接口不回显通知 token', 'SECRET-BOT' not in settings_text)
    check('设置接口不回显企微 webhook secret', 'SECRET' not in settings_text)
    check('设置接口不回显 AI key', 'sk-SECRET' not in settings_text)
    check('设置接口不回显 WebDAV 密码', 'WD-SECRET' not in settings_text)
    s = json.loads(settings_text)
    check('设置含 AI 失败阈值', s['ai']['fail_threshold'] == 3)
    check('设置含渠道代理覆盖',
          s['notify']['channel_proxy'].get('wecom', {}).get('mode') == 'direct')

    # 新增站点 + 录制 + 保存
    post('/api/record/start', {'id': 'smoke', 'name': '冒烟站',
                               'start_url': 'https://example.com/'})
    post('/api/record/events', {'id': 'smoke', 'events': [
        {'kind': 'click', 'info': {'tag': 'button', 'id': 'checkin'}},
        {'kind': 'input', 'info': {'tag': 'input', 'id': 'u'}, 'value': 'alice'},
    ]})
    st, body = get('/api/record/status?id=smoke')
    rc = json.loads(body)
    check('录制状态有记录', rc['event_count'] == 2 and len(rc['steps']) >= 3)
    act = [x['action'] for x in rc['steps']]
    check('步骤含 goto/click/fill',
          act[0] == 'goto' and 'click' in act and 'fill' in act)
    st, body = post('/api/record/stop', {'id': 'smoke', 'save': True})
    check('录制保存成功', json.loads(json.dumps(body)).get('saved') is True)

    st, body = get('/api/site?id=smoke')
    site = json.loads(body)['site']
    check('站点已保存步骤', len(site['steps']) >= 3)
    check('站点标为自建', site['kind'] == 'custom')

    # ---- 登录 Cookie 管理 ----
    st, body = get('/api/site/cookie?id=smoke')
    ck = json.loads(body)
    check('Cookie 状态接口可用（初始未配置）',
          st == 200 and ck['has_cookie'] is False and ck['count'] == 0)

    # 用 cURL 形式保存（这是推荐给用户的粘贴方式）
    st, body = post('/api/site/cookie', {
        'id': 'smoke',
        'cookie': "curl 'https://x.com/' -H 'cookie: session=abc123; sid=xyz'",
    })
    check('保存 cURL 形式的 Cookie 成功',
          st == 200 and body.get('count') == 2 and 'cURL' in (body.get('format') or ''),
          str(body)[:120])

    st, body = get('/api/site/cookie?id=smoke')
    ck = json.loads(body)
    check('Cookie 已记录且只回显字段名',
          ck['has_cookie'] is True and ck['count'] == 2
          and 'session' in ck['names'] and 'abc123' not in json.dumps(ck),
          json.dumps(ck)[:140])

    st, body = get('/api/sites')
    sites = json.loads(body)['sites']
    one = [s for s in sites if s['id'] == 'smoke'][0]
    check('站点列表显示登录已配置', one.get('has_cookie') is True)
    check('站点列表不含 Cookie 内容', 'abc123' not in json.dumps(sites))

    # 坏输入必须被拒（400），不能静默存垃圾
    st, body = post('/api/site/cookie', {'id': 'smoke', 'cookie': '这段文字没有 Cookie'})
    check('无法解析的 Cookie 返回 400', st == 400, str(body)[:110])

    st, body = post('/api/site/cookie', {'id': 'smoke', 'cookie': ''})
    check('空 Cookie 被拒', st == 400)

    st, body = post('/api/site/cookie/clear', {'id': 'smoke'})
    check('清除 Cookie 成功', st == 200 and body.get('ok') is True)
    st, body = get('/api/site/cookie?id=smoke')
    check('清除后状态归零', json.loads(body)['has_cookie'] is False)

    # ---- 自定义请求头（NodeSeek 靠它，缺了就 403）----
    st, body = post('/api/site', {
        'id': 'smoke',
        'headers': {'Origin': 'https://a.com', 'Referer': 'https://a.com/b'},
    })
    check('保存自定义请求头成功', st == 200, str(body)[:110])
    st, body = get('/api/site?id=smoke')
    hdrs = json.loads(body)['site'].get('headers') or {}
    check('自定义请求头读回正确',
          hdrs.get('Origin') == 'https://a.com'
          and hdrs.get('Referer') == 'https://a.com/b', json.dumps(hdrs)[:120])

    # ---- 内联编辑表单需要的字段 ----
    st, body = get('/api/site?id=smoke')
    s = json.loads(body)['site']
    for k in ('mode', 'daily_hour', 'daily_minute', 'jitter_enabled',
              'jitter_seconds', 'retry_enabled', 'retry_count',
              'retry_interval_minutes', 'ai_enabled', 'ai_after_failures',
              'notify', 'need_browser', 'verify_ssl'):
        if k not in s:
            check('站点字段 %s 存在' % k, False)
            break
    else:
        check('站点表单需要的字段齐全', True)

    # 内置站点的模板预置头（NodeSeek 的 Origin/Referer 是实测必需项）
    st, body = get('/api/sites')
    builtin = {x['id']: x for x in json.loads(body)['sites']}
    ns = builtin.get('nodeseek') or {}
    check('NodeSeek 预置了 Origin/Referer',
          (ns.get('headers') or {}).get('Origin') == 'https://www.nodeseek.com'
          and bool((ns.get('headers') or {}).get('Referer')),
          json.dumps(ns.get('headers'), ensure_ascii=False)[:140])
    check('NodeSeek 不再标记为需要浏览器', ns.get('need_browser') is False)

    # 告警测试在没有渠道以外的错误都该正常
    st, body = post('/api/notify/alert-test', {})
    check('告警测试端点可用（渠道已配置）', st == 200 and 'report' in body)

    # 404
    code, _ = post('/api/nope', {})
    check('未知接口返回 404', code == 404)

    print()
    if fails:
        print('失败 %d 项：%s' % (len(fails), '、'.join(fails)))
    else:
        print('全部通过')
finally:
    httpd.shutdown()
    httpd.server_close()
    import shutil
    shutil.rmtree(data_dir, ignore_errors=True)

sys.exit(1 if fails else 0)
