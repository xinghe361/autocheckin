"""校验 docker-compose.yml 的结构（不依赖 pyyaml，用缩进解析）。

为什么值得单独做：compose 文件写错一个缩进，用户拿到的就是
"yaml: line N: did not find expected key" 这种没头没脑的报错，
而这个项目是要给非技术用户在极空间界面上直接粘贴的。
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def strip_comments(text: str):
    """去掉整行注释，返回 [(行号, 缩进, 内容)]。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        out.append((i, indent, line.strip()))
    return out


def check_indentation(rows):
    """同一层级里，缩进必须一致；子键必须比父键更深。"""
    problems = []
    stack = []          # [(indent, key)]
    for lineno, indent, content in rows:
        if indent % 2 != 0:
            problems.append('第 %d 行缩进是 %d（不是 2 的倍数）：%s'
                            % (lineno, indent, content))
        # 列表项
        if content.startswith('- '):
            if not stack:
                problems.append('第 %d 行出现列表项但没有父键：%s' % (lineno, content))
            continue
        m = re.match(r'^([\w.\-]+):(.*)$', content)
        if not m:
            problems.append('第 %d 行不是合法的 key: value：%s' % (lineno, content))
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack and indent <= stack[-1][0]:
            problems.append('第 %d 行缩进未比父键更深：%s' % (lineno, content))
        stack.append((indent, m.group(1)))
    return problems


def volumes_section(text: str):
    """只取 volumes: 段下的列表项，返回 [(宿主侧, 容器侧, 附加项)]。

    为什么不拿全文正则：环境变量里像 PROXY=http://192.168.1.1:7890 这样的值
    会被 `- x:y` 这类宽正则误当成挂载项，得出莫名其妙的结论（踩过）。
    """
    out = []
    lines = text.splitlines()
    inside = False
    base_indent = 0
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        stripped = line.strip()
        if stripped == 'volumes:':
            inside, base_indent = True, indent
            continue
        if inside:
            if indent <= base_indent:
                inside = False
                continue
            m = re.match(r'-\s+(\S+?):(\S+)', stripped)
            if m:
                parts = m.group(2).split(':')
                out.append((m.group(1), parts[0],
                            ':'.join(parts[1:]) if len(parts) > 1 else ''))
    return out


def main() -> int:
    path = os.path.join(HERE, 'docker-compose.yml')
    text = open(path, encoding='utf-8').read()
    rows = strip_comments(text)

    print('=== docker-compose.yml 校验 ===')
    print('  有效配置行数: %d' % len(rows))

    problems = check_indentation(rows)
    if problems:
        for p in problems:
            print('  !! %s' % p)
    else:
        print('  缩进与键值结构: OK')

    # 关键字段必须在
    body = '\n'.join(c for _, _, c in rows)
    required = {
        'services:': '顶层 services',
        'autocheckin:': '服务名',
        'image:': '镜像',
        'security_opt:': '安全加固',
        'no-new-privileges:true': 'no-new-privileges 值',
        'shm_size:': '/dev/shm 大小',
        'volumes:': '数据卷',
        'environment:': '环境变量',
    }
    print('\n=== 关键字段 ===')
    missing = []
    for key, label in required.items():
        ok = key in body
        print('  %s %s' % ('[OK]  ' if ok else '[缺]  ', label))
        if not ok:
            missing.append(key)

    # host 模式下不需要 ports；桥接模式才需要。两种都算合法，但要有其一说明。
    if ('PORT:' in body or 'PORT=' in body) or any(
            c.startswith('ports:') for c in body.splitlines()):
        print('  [OK]   端口')
    else:
        # 端口不是硬性字段：镜像内已有默认 PORT=28999，
        # 但模板里最好显式写出来，免得用户改了访问地址却不知道改哪。
        print('  [提示] 未显式写 PORT / ports（镜像内默认 28999）')

    # host 模式不应出现 ports
    print('\n=== 模式一致性 ===')
    active_rows = [c for _, _, c in rows]
    has_network_host = 'network_mode: host' in active_rows
    has_ports = any(c.startswith('ports:') for c in active_rows)
    if has_network_host and has_ports:
        print('  !! 同时出现 network_mode: host 与 ports:（host 模式下 ports 无效）')
        missing.append('ports-conflict')
    elif has_network_host:
        print('  OK host 模式，且未被注释掉的部分没有 ports')
    else:
        print('  [提示] 未启用 host 模式')

    # 数据卷必须挂到容器内的 /data（与 DATA_DIR 一致）。
    # 宿主侧可以是绝对路径（指定磁盘）或相对路径（./data，随 compose 文件走），
    # 两种都是用户明确要的用法，都接受 —— 关键是【容器侧】必须对：
    # 挂错到 /app/data 之类的位置，数据会写进镜像层，容器一重建就丢。
    vols = volumes_section(text)
    container_sides = [v[1] for v in vols]
    if not vols:
        print('  !! volumes: 段里没有解析到任何挂载项')
        missing.append('volume-none')
    elif '/data' not in container_sides:
        print('  !! 数据卷没有挂到容器内的 /data（实际：%s）'
              % (', '.join(container_sides) or '无'))
        print('     提示：容器里 DATA_DIR=/data；挂到别处数据会写进镜像层，重建即丢。')
        missing.append('volume-target')
    else:
        host_side = [v[0] for v in vols if v[1] == '/data'][0]
        kind = '绝对路径' if host_side.startswith('/') else '相对路径'
        print('  OK 数据卷 %s，宿主侧=%s -> 容器侧=/data' % (kind, host_side))
        if host_side.startswith('/data/'):
            # 极空间的系统分区很小（实测只剩几百 MB），写满会影响整机
            print('  !! 宿主侧看起来是系统分区 /data/...，请改用数据盘（/data_XXXX...）')
            missing.append('system-partition')

    # DATA_DIR 与挂载点必须一致，否则数据会写到别处
    m_dir = re.search(r'DATA_DIR[=:]\s*["\']?([^"\'\s]+)', body)
    if m_dir and container_sides and m_dir.group(1) not in container_sides:
        print('  !! DATA_DIR=%s 与挂载的容器侧 %s 不一致'
              % (m_dir.group(1), ', '.join(container_sides)))
        missing.append('data_dir-mismatch')
    elif m_dir:
        print('  OK DATA_DIR 与挂载点一致（%s）' % m_dir.group(1))

    print()
    bad = bool(problems or missing)
    print('结论：%s' % ('有问题，见上' if bad else '可以安全使用'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
