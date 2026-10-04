"""提取 web.py 内嵌的 JS 并用 Node 的 vm.Script 真解析。

不能只靠括号计数（会被正则/字符串里的括号骗），必须让真正的 JS 引擎解析。
同时检查几个容易出错的地方：
  * 顶层 return（放在 IIFE 外面就是语法错误）
  * 模板字符串/引号是否闭合
  * 我新加的 onclick 里嵌套引号是否正确
"""
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).parent          # docs/
ROOT = HERE.parent                            # 项目根
WEB = ROOT / 'app' / 'web.py'
NODE = (r'C:\Users\xingh\.dsh\dsh-runtimes\dsh-primary-runtime'
        r'\dependencies\node\bin\node.exe')


def extract_js(py_text: str) -> str:
    """取出 <script> ... </script> 里的内容。"""
    blocks = re.findall(r'<script[^>]*>(.*?)</script>', py_text, re.S)
    return '\n;\n'.join(blocks)


def main() -> int:
    text = WEB.read_text(encoding='utf-8')

    # web.py 里的 HTML/JS 是包在 Python 字符串里的，可能含 \' 之类的转义。
    # 这里不做完整反转义，只把 Python 的三引号块原样取出即可 ——
    # 实际发到浏览器的内容由 HTML 模板字符串决定，转义不多。
    js = extract_js(text)
    if not js.strip():
        print('没有找到 <script> 块')
        return 1

    print('提取到 JS：%d 字符，%d 行' % (len(js), len(js.splitlines())))

    # 写临时文件交给 node 解析
    tmp = ROOT / '_webextract.js'
    tmp.write_text(js, encoding='utf-8')

    checker = ROOT / '_jscheck.js'
    checker.write_text(
        'const fs=require("fs"),vm=require("vm");\n'
        'const src=fs.readFileSync(process.argv[2],"utf8");\n'
        'try{ new vm.Script(src,{filename:"web.js"});\n'
        '  console.log("JS 语法 OK"); }\n'
        'catch(e){ console.log("JS 语法错误: "+e.message);\n'
        '  process.exit(1); }\n',
        encoding='utf-8')

    r = subprocess.run([NODE, str(checker), str(tmp)],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    print((r.stdout or '').strip())
    if r.stderr.strip():
        print('stderr:', r.stderr.strip()[:400])

    # 额外检查：顶层 return（IIFE 外的 return 是语法错误）
    top_return = re.findall(r'^\s*return\b', js, re.M)
    if top_return:
        print('警告：发现 %d 处顶层缩进的 return，请确认它们在函数内' % len(top_return))

    for f in (tmp, checker):
        try:
            f.unlink()
        except Exception:
            pass

    return r.returncode


if __name__ == '__main__':
    sys.exit(main())
