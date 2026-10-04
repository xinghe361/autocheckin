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
    # "删了函数忘改调用"检查。
    # 为什么需要：我删掉 swAiLimits() 时漏改了两个调用点，语法检查照样通过，
    # 但用户每次勾选站点开关都会抛 ReferenceError（控制台报错、以为坏了）。
    #
    # 做法上刻意保守：先从源码里取出**所有被定义的函数名**，
    # 再只看"明显是调用语句"的形态（`name(`），且要求该名字在整份源码里
    # 既没有 `function name(` 也没有 `name =`。这样：
    #   * 不会把 Math.floor(...) 这类成员调用算进来（有前缀 .）
    #   * 不会把内建函数算进来（内建在源码里没有定义，但也没被当语句调用
    #     到我们关心的这层 —— 用一份极小的黑名单兜住常见的几个）
    checker.write_text(
        'const fs=require("fs"),vm=require("vm");\n'
        'const src=fs.readFileSync(process.argv[2],"utf8");\n'
        'try{ new vm.Script(src,{filename:"web.js"});\n'
        '  console.log("JS 语法 OK"); }\n'
        'catch(e){ console.log("JS 语法错误: "+e.message); process.exit(1); }\n'
        # 去掉注释，避免把注释里提到的函数名当成调用
        'const code=src.replace(/\\/\\*[\\s\\S]*?\\*\\//g," ")\n'
        '              .replace(/(^|[^:])\\/\\/[^\\n]*/g,"$1 ");\n'
        # 定义名
        'const defs=new Set();\n'
        'for(const m of code.matchAll(/function\\s+([A-Za-z_$][\\w$]*)\\s*\\(/g))'
        ' defs.add(m[1]);\n'
        'for(const m of code.matchAll(/(?:var|let|const)\\s+([A-Za-z_$][\\w$]*)\\s*=/g))'
        ' defs.add(m[1]);\n'
        # 语句位置的调用（行首缩进 + name(），排除成员调用
        'const called=new Set();\n'
        'for(const m of code.matchAll(/^[ \\t]*([A-Za-z_$][\\w$]*)\\s*\\(/gm))'
        ' called.add(m[1]);\n'
        'for(const m of code.matchAll(/[;{)]\\s*([A-Za-z_$][\\w$]*)\\s*\\(/g))'
        ' called.add(m[1]);\n'
        'const builtin=new Set(["if","for","while","switch","catch","return",'
        '"typeof","function","new","do","else","try","finally","String","Number",'
        '"Boolean","Array","Object","parseInt","parseFloat","isNaN","alert",'
        '"setTimeout","setInterval","clearTimeout","clearInterval","fetch"]);\n'
        'const undef=[...called].filter(n=>!defs.has(n)&&!builtin.has(n));\n'
        'if(undef.length){ console.log("被调用但未定义: "+undef.join(", "));'
        ' process.exit(2); }\n',
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
