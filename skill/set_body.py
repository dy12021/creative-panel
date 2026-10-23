#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 UTF-8 文件里的正文写回创意面板的卡片。

为什么不用 PowerShell 内联 Invoke-RestMethod：
PowerShell 5.1 的请求体编码走 cp936，中文会被**静默**转成乱码
（不报错、不回滚），写进去的正文就废了。
所以：正文放 UTF-8 文件，这里用 urllib 读出来、按 UTF-8 发出去。

用法：python set_body.py <card_id> <body_file> [field]
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8472"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    card_id = sys.argv[1]
    body_path = sys.argv[2]
    field = sys.argv[3] if len(sys.argv) > 3 else "body"

    with open(body_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    payload = json.dumps({field: text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/ideas/{card_id}",
        data=payload,
        method="PATCH",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    data = json.loads(body)
    print(json.dumps({
        "id": data.get("id"),
        "status": data.get("status"),
        "body_len": len(data.get("body") or ""),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
