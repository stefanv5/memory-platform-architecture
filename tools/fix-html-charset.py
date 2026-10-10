#!/usr/bin/env python3
"""HTML charset 门禁/自动修正脚本。

规则：浏览器只在页面前 1024 字节内识别 <meta charset> 声明。
本脚本扫描目标目录下的全部 .html，凡是前 1024 字节内没有 charset
声明的，一律在字节 0 前置 <meta charset="utf-8">。

用法：
  python3 tools/fix-html-charset.py [目录...]            # 修正模式（默认 docs/hindsight）
  python3 tools/fix-html-charset.py --check [目录...]    # 检查模式：发现问题则退出码 1（可作 CI 门禁）

幂等：已合规的文件不动；修正过的文件再跑一遍零变更。
"""
import re
import sys
from pathlib import Path

META = b'<meta charset="utf-8">'
WINDOW = 1024
HAS_CHARSET = re.compile(rb'charset\s*=\s*["\']?[\w-]+', re.I)


def needs_fix(body: bytes) -> bool:
    return not HAS_CHARSET.search(body[:WINDOW])


def fix(path: Path) -> bool:
    body = path.read_bytes()
    if not needs_fix(body):
        return False
    nl = b"\r\n" if b"\r\n" in body[:200] else b"\n"
    path.write_bytes(META + nl + body)
    return True


def main() -> int:
    argv = sys.argv[1:]
    check = "--check" in argv
    roots = [Path(a) for a in argv if not a.startswith("--")] or [Path("docs/hindsight")]
    scanned = fixed = 0
    offenders = []
    for root in roots:
        for path in sorted(root.rglob("*.html")):
            scanned += 1
            if needs_fix(path.read_bytes()):
                offenders.append(path)
                if not check and fix(path):
                    fixed += 1
    mode = "检查" if check else "修正"
    print(f"扫描 {scanned} 个 HTML（{mode}模式）：{'需要修正 ' + str(len(offenders)) if offenders else '全部合规'}")
    for p in offenders:
        print(f"  {'[未修]' if check else '[已修]'} {p}")
    return 1 if (check and offenders) else 0


if __name__ == "__main__":
    sys.exit(main())
