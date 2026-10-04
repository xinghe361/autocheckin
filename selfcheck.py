"""项目本地自检：确认要上传/构建的东西是干净、完整的。

不需要任何网络或凭据，随时可跑。
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 绝不能被提交/打包的文件（含密钥或运行数据）
SECRET_FILES = ('secret.key', 'config.json', 'state.json', 'nas.env', '.env',
                'credentials.json', 'id_rsa')

# 关键交付文件
REQUIRED = (
    'README.md', 'Dockerfile', 'docker-compose.yml', 'requirements.txt',
    'build_image.py', 'smoke_web.py', '.dockerignore', '.gitignore',
    'app/__init__.py', 'app/__main__.py', 'app/main.py', 'app/service.py',
    'app/web.py', 'app/scheduler.py', 'app/runner.py', 'app/engine.py',
    'app/templates.py', 'app/notify.py', 'app/deepseek.py', 'app/browser.py',
    'app/recorder.py', 'app/secrets.py', 'app/store.py', 'app/webdav.py',
    '.github/workflows/build.yml',
)

# 需要在本机额外拦截的敏感内容。
# 默认留空：仓库本身不应包含任何具体凭据，所以没有"默认要拦的字符串"。
# 如果你在本地用过某些真实地址/账号，可以在这里补上（值请自行填写，
# 不要把它提交到公开仓库）。
PRIVACY_PATTERNS = []

# 隐私扫描的豁免文件。
# tests/test_selfcheck_secrets.py 是"疑似真实 Cookie"探测器自己的测试 ——
# 它必须在源码里写出看起来像真凭据的样例，才能证明探测器有效（自指问题）。
# 这些样例是刻意编的，不是任何人的凭据。
PRIVACY_SCAN_ALLOWLIST = (
    os.path.join('tests', 'test_selfcheck_secrets.py'),
)


def _default_patterns():
    """始终检查的通用凭据形态（令牌、私钥等）。"""
    return [
        (re.compile(r'ghp_[A-Za-z0-9]{30,}'), 'GitHub token'),
        (re.compile(r'dckr_pat_[A-Za-z0-9_\-]{20,}'), 'Docker Hub token'),
        (re.compile(r'sk-[A-Za-z0-9]{20,}'), 'API Key'),
        (re.compile(r'BEGIN [A-Z ]*PRIVATE KEY'), '私钥'),
        # 会话 Cookie 的【真实值】。踩过的坑：为了写"真实格式"的测试，
        # 把用户的真实 Cookie 复制进了 tests/ 里 —— 测试文件是要进公开仓库的。
        # 真实会话值是高熵随机串，而测试里的假值（abc / xxx / FAKE…）熵很低，
        # 所以用"长度 + 字符集多样性"来区分。
        (re.compile(COOKIE_VALUE_RE), '疑似真实 Cookie 值'),
        (re.compile(r'\bcf_clearance=[A-Za-z0-9._\-]{40,}'), 'cf_clearance'),
    ]


# 形如 session=<16 位以上、且同时含大小写或数字> 的赋值。
# 只拦"看起来像随机串"的值：
#   session=abc            -> 不拦（太短/太单一）
#   session=FAKESESSION001 -> 不拦（全大写，熵低）
#   session=jgnXCpo1vy7... -> 拦（长且大小写数字混合）
COOKIE_VALUE_RE = (
    r'\b(?:session|sid|auth|token|pjwt|smac|hmti_|_t|csrf|saltkey|key)'
    r'["\']?\s*[=:]\s*["\']?'          # 允许 JSON 那种 "name": "value"
    r'(?=[A-Za-z0-9._\-%+/]{16,})'
    r'(?=[A-Za-z0-9._\-%+/]*[A-Z])'
    r'(?=[A-Za-z0-9._\-%+/]*[a-z])'
    r'(?=[A-Za-z0-9._\-%+/]*[0-9])'
    r'[A-Za-z0-9._\-%+/]{16,}'
)


def main() -> int:
    problems = []

    print('=== 1. 敏感文件检查（不应存在）===')
    found_secret = False
    for name in SECRET_FILES:
        if os.path.exists(os.path.join(HERE, name)):
            print('  !! 存在 %s —— 请先删除' % name)
            found_secret = True
    if not found_secret:
        print('  OK 数据目录、密钥、凭据文件都不在项目里')

    print('\n=== 2. 关键文件检查 ===')
    missing = [r for r in REQUIRED if not os.path.exists(os.path.join(HERE, r))]
    if missing:
        for m in missing:
            print('  !! 缺少 %s' % m)
    else:
        print('  OK %d 个关键文件齐全' % len(REQUIRED))

    print('\n=== 3. 隐私扫描（源码里不能有你的账号/密码）===')
    hits = []
    bad_encoding = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs
                   if d not in ('__pycache__', '.git', 'node_modules',
                                '.venv', 'venv')]
        for fn in files:
            if not re.search(r'\.(py|md|json|txt|ya?ml|sh|toml|cfg|ini)$', fn):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, HERE)
            if rel in PRIVACY_SCAN_ALLOWLIST:
                continue
            raw = open(p, 'rb').read()
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError as e:
                # 曾经踩过的坑：用 PowerShell 的 Add-Content 写文件会变成 GBK，
                # 在 Linux/容器里就可能出问题。这里报出来而不是崩溃。
                bad_encoding.append((rel, str(e)))
                text = raw.decode('utf-8', 'replace')
            for pat, label in (PRIVACY_PATTERNS + _default_patterns()):
                if pat.search(text):
                    hits.append((rel, label))
    if hits:
        for rel, label in hits:
            print('  !! %s 含 %s' % (rel, label))
    else:
        print('  OK 未发现你的账号与密码')
    if bad_encoding:
        for rel, err in bad_encoding:
            print('  !! %s 不是合法 UTF-8：%s' % (rel, err))
        problems.append('encoding')

    print('\n=== 4. 忽略规则检查 ===')
    for name, musts in (('.gitignore', ('secret.key', 'config.json', 'state.json',
                                        '__pycache__', 'data/')),
                        ('.dockerignore', ('secret.key', 'config.json',
                                           'tests/', '__pycache__'))):
        p = os.path.join(HERE, name)
        if not os.path.exists(p):
            print('  !! 缺少 %s' % name)
            problems.append(name)
            continue
        text = open(p, encoding='utf-8').read()
        miss = [m for m in musts if m not in text]
        if miss:
            print('  !! %s 缺少规则：%s' % (name, ', '.join(miss)))
        else:
            print('  OK %s 规则完整' % name)

    print('\n=== 5. 代码可导入性 ===')
    sys.path.insert(0, HERE)
    try:
        import app.main  # noqa: F401
        print('  OK app.main 可导入')
    except Exception as e:                                  # noqa: BLE001
        print('  !! 导入失败：%s' % e)
        problems.append('import')

    print('\n=== 6. 统计 ===')
    py_files = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.git')]
        py_files += [os.path.join(root, f) for f in files if f.endswith('.py')]
    total = sum(len(open(f, encoding='utf-8', errors='replace').readlines())
                for f in py_files)
    print('  Python 文件 %d 个，共 %d 行' % (len(py_files), total))

    print()
    bad = bool(missing or hits or found_secret or problems)
    print('结论：%s' % ('发现问题，见上' if bad else '可以安全上传与构建'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
