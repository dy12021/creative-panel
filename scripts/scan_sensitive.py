# -*- coding: utf-8 -*-
"""开源前敏感信息扫描。

扫「将要发布的内容」= git 跟踪的文件 + 工作区未跟踪的新文件。
只看会被推上去的路径，不扫 data/ 与缓存。

用法（在仓库根目录跑）：
    python scripts/scan_sensitive.py

输出：命中清单。**0 命中才算干净**；有命中就按下面的类型逐条处理。
本脚本只读，不改任何文件。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (名字, 正则, 类型)
RULES = [
    ("私有密钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), "token"),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "key"),
    ("OpenAI 风格 key", re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "key"),
    ("api_key / token 赋值", re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|secret[_-]?key)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"), "key"),
    ("Bearer 头带值", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), "token"),
    # 面板自己的本机地址是产品的一部分（127.0.0.1:8472），不算隐私；
    # 内网 IP 才算。
    ("内网地址", re.compile(
        r"https?://(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)[:/\w\-]*"), "地址"),
    ("绝对路径含用户名", re.compile(
        r"[A-Za-z]:\\Users\\[^\\\s\"']+|/Users/[^/\s\"']+|/home/[^/\s\"']+"), "本机路径"),
    ("本机项目绝对路径", re.compile(r"[A-Za-z]:\\(?:coding|Users|Projects?)\\[^\\\s\"',)]+", re.I), "本机路径"),
    ("邮箱", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "邮箱"),
    ("长 base64/hex 串", re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9+/=]{40,}(?![A-Za-z0-9_])"), "长串"),
]

# 长 token 里像 python 标识符的（snake_case）是函数名/变量名，不是凭据
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 这些目录永远不入库，扫它们没意义
SKIP_TOP = {".git", "__pycache__", ".pytest_cache", ".superpowers", "data", "node_modules"}


def git(*args):
    return subprocess.run(("git",) + args, cwd=ROOT, capture_output=True, text=True).stdout


def publishable_files():
    files = [f.strip() for f in git("ls-files").splitlines() if f.strip()]
    for line in git("status", "--porcelain", "--untracked-files=all").splitlines():
        if line.startswith("?? "):
            files.append(line[3:].strip())
    return sorted(set(files))


def main():
    files = publishable_files()
    print("将要发布的文件 %d 个：\n" % len(files))
    for f in files:
        print("   ", f)
    print()

    hits = []
    for rel in files:
        if rel.split("/")[0] in SKIP_TOP:
            continue
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError as e:
            print("读不了 %s: %s" % (rel, e))
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for name, pat, kind in RULES:
                m = pat.search(line)
                if not m:
                    continue
                if kind == "长串":
                    tok = m.group(0)
                    if _IDENT.match(tok) and "_" in tok:
                        continue          # snake_case 标识符 = 函数名
                    if len([c for c in tok if c.isalpha()]) < 8 or \
                       len([c for c in tok if c.isdigit()]) < 6:
                        continue          # 真凭据得有足够多的字母+数字混排
                # 本脚本自己的正则里必然写着要查的那些字面量，自指不算命中
                if kind == "本机路径" and rel.replace("\\", "/").endswith(
                        "scripts/scan_sensitive.py"):
                    continue
                hits.append((rel, i, name, kind, line.strip()[:150]))

    if not hits:
        print("=== 0 命中：干净 ===")
        return 0
    print("=== 命中 %d 处 ===" % len(hits))
    for rel, i, name, kind, line in hits:
        print("  [%s/%s] %s:%d\n      %s" % (kind, name, rel, i, line))
    return 1


if __name__ == "__main__":
    sys.exit(main())
