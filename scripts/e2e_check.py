#!/usr/bin/env python3
"""端到端实测脚本：对运行中的演练服务复现连锁跳闸、旧启动编号被拒、法定人数恢复。

用法：
    python3 scripts/e2e_check.py [BASE_URL]
默认 BASE_URL=http://127.0.0.1:8000
仅使用标准库，便于在宿主机或容器外对 `docker compose up` 后的服务复测。
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PASS = "✅"
FAIL = "❌"
steps = 0


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name, cond, detail=""):
    global steps
    steps += 1
    if cond:
        print(f"{PASS} {name} {detail}")
    else:
        print(f"{FAIL} {name} {detail}")
        sys.exit(1)


def get_html(path="/"):
    with urllib.request.urlopen(BASE + path, timeout=8) as resp:
        return resp.read().decode()


# 0. 复位到样例一
st, body = call("POST", "/api/reset", {"sample": "cross"})
check("复位样例一", st == 200)
state = body["state"]
check("初始 3/3 就绪、集群可用",
      state["cluster"]["available"] and state["cluster"]["ready_count"] == 3)

# 1. 断开 WL1 -> 两轮连锁，第一轮 WL2/WL3 同时跳闸
st, body = call("POST", "/api/branches/WL1/disconnect")
rounds = [[t["branch"] for t in r["tripped"]] for r in body["operation"]["rounds"]]
check("断开 WL1 后跳闸轮次为 [WL2,WL3] -> [WL4]",
      rounds == [["WL2", "WL3"], ["WL4"]], f"实际={rounds}")
state = body["state"]
check("连锁后 0/3 有电、集群不可用",
      state["cluster"]["powered_count"] == 0 and not state["cluster"]["available"])

# 2. 掉电期间用旧编号 #1 报就绪 -> 409 unpowered
st, body = call("POST", "/api/replicas/db-1/ready", {"boot_no": 1})
check("掉电中就绪上报返回 409/unpowered",
      st == 409 and body["report"]["code"] == "unpowered", f"HTTP {st}")

# 3. 合 WL1 -> db-1/db-2 复电，新启动编号 #2
st, body = call("POST", "/api/branches/WL1/close")
servers = {s["id"]: s for s in body["state"]["servers"]}
check("合 WL1 后 db-1/db-2 启动编号为 #2",
      servers["db-1"]["boot_no"] == 2 and servers["db-2"]["boot_no"] == 2
      and servers["db-1"]["powered"] and not servers["db-3"]["powered"])

# 4. 复电后拿旧启动编号 #1 报就绪 -> 409 stale_boot
st, body = call("POST", "/api/replicas/db-1/ready", {"boot_no": 1})
check("旧启动编号 #1 被拒（409/stale_boot，当前 #2）",
      st == 409 and body["report"]["code"] == "stale_boot"
      and body["report"]["current_boot_no"] == 2, f"HTTP {st}")

# 5. 用当前编号 #2 报就绪 -> 200；仅 1 副本时集群仍不可用
st, body = call("POST", "/api/replicas/db-1/ready", {"boot_no": 2})
check("当前启动编号 #2 被接受", st == 200 and body["ok"])
check("仅 1/3 就绪时集群不可用", not body["state"]["cluster"]["available"])

# 6. db-2 报就绪 -> 2/3，集群可用
st, body = call("POST", "/api/replicas/db-2/ready", {"boot_no": 2})
check("db-2 就绪后 2/3、集群可用",
      st == 200 and body["state"]["cluster"]["ready_count"] == 2
      and body["state"]["cluster"]["available"])

# 7. 合 WL2 稳定；单合 WL3 会再跳；WL3+WL4 并列合闸完成全恢复
st, body = call("POST", "/api/branches/WL2/close")
check("合 WL2 无跳闸", st == 200 and body["operation"]["rounds"] == [])

st, body = call("POST", "/api/branches/WL3/close")
wl3 = next(b for b in body["state"]["branches"] if b["id"] == "WL3")
check("单独合 WL3 因 1500W>1000W 再次跳闸",
      bool(body["operation"]["rounds"]) and wl3["closed"] is False)

st, body = call("POST", "/api/branches/close-many", {"ids": ["WL3", "WL4"]})
check("WL3/WL4 同时合闸成功且无跳闸",
      st == 200 and body["operation"]["rounds"] == [])
check("三服务器全部复电", body["state"]["cluster"]["powered_count"] == 3)

st, body = call("POST", "/api/replicas/db-3/ready", {"boot_no": 2})
check("db-3 用新编号 #2 就绪，3/3",
      st == 200 and body["state"]["cluster"]["ready_count"] == 3)

# 8. 服务端渲染的网页确实包含轮次、容量、旧编号被拒记录
html = get_html("/")
for marker in ["第 1 轮", "WL2", "WL3", "第 2 轮", "2200 W",
               "数据库集群可用", "旧编号", "启动编号"]:
    check(f"网页 HTML 含 {marker!r}", marker in html)

print(f"\n全部 {steps} 项实测通过 ✅  目标={BASE}")
