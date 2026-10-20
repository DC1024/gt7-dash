# -*- coding: utf-8 -*-
"""P0-3 真实 HTTP 冒烟：起一个真服务，用真 socket 打五条路径。

与 pytest 里的测试互补：这里走的是 main() 之外的**真实服务器对象**，
且额外验证长连接上的第二条请求（keep-alive 污染）与拒绝路径的日志。
"""
import http.client
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib.util

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "dash", REPO / "gt7-dashboard.py")
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

hist = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./_smoke_data")
hist.mkdir(parents=True, exist_ok=True)


class Handler(dash.DashboardHandler):
    pass


srv = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
srv.daemon_threads = True
srv.history_dir = str(hist)
srv.allow_hosts = ()
dash.HUB = dash.TelemetryHub(hist / "status.json")
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

LAN = "192.168.43.96"   # 换成本机 LAN IP 才有意义；用 127 也能跑逻辑
ok = 0
fail = 0


def req(method, path, headers=None, body=None, host=LAN):
    h = {"Host": f"{host}:{port}"}
    h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        return r.status, r.read(), dict(r.getheaders())
    finally:
        c.close()


def check(name, got, want, extra=""):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  [OK]   {name}: {got}")
    else:
        fail += 1
        print(f"  [FAIL] {name}: 期望 {want} 实际 {got} {extra}")


S = "/api/v1/settings/trash-retention"
P = '{"value": 30}'

print("=" * 62)
print("P0-3 跨站防护 · 真实 HTTP 冒烟")
print("=" * 62)

print("\n[1] 局域网内正常使用（不能被误伤）")
check("同源写(浏览器点收藏)",
      req("POST", S, {"Content-Type": "application/json",
                      "Origin": f"http://{LAN}:{port}",
                      "Sec-Fetch-Site": "same-origin"}, P)[0], 200)
check("旧 Safari 无 Sec-Fetch-Site",
      req("POST", S, {"Content-Type": "application/json",
                      "Origin": f"http://{LAN}:{port}"}, P)[0], 200)
check("curl 脚本无 Origin",
      req("POST", S, {"Content-Type": "application/json"}, P)[0], 200)
st, _, hd = req("GET", "/api/v1/laps")
check("读接口可用", st, 200)
check("读接口保留 ACAO:*", hd.get("Access-Control-Allow-Origin"), "*")

print("\n[2] 跨站写必须被挡")
st, bd, _ = req("POST", S, {"Content-Type": "application/json",
                            "Origin": "http://evil.example"}, P)
check("伪造 Origin 跨站", st, 403, bd[:80])
st, bd, _ = req("POST", S, {"Content-Type": "application/json",
                            "Origin": f"http://{LAN}:{port}",
                            "Sec-Fetch-Site": "cross-site"}, P)
check("Origin 一致但 Sec-Fetch-Site 跨站", st, 403, bd[:80])
st, _, hd = req("POST", S, {"Content-Type": "application/json",
                            "Origin": "http://evil.example"}, P)
check("拒绝响应不带 ACAO", hd.get("Access-Control-Allow-Origin"), None)
st, _, hd = req("OPTIONS", "/api/v1/laps", {
    "Origin": "http://evil.example",
    "Access-Control-Request-Method": "POST"})
check("预检不批准 POST", st, 403)
check("预检不批准时不回 Allow-Methods",
      hd.get("Access-Control-Allow-Methods"), None)

print("\n[3] 长连接污染（提前 return 漏读 body）")
c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
try:
    hdr = {"Host": f"{LAN}:{port}", "Content-Type": "application/json",
           "Origin": "http://evil.example"}
    c.request("POST", S, body=P, headers=hdr)
    r = c.getresponse()
    r.read()
    first = r.status
    closing = (r.getheader("Connection") or "").lower() == "close"
    if closing:
        print(f"  [OK]   拒绝后回 Connection: close（连接作废，无残留）")
        ok += 1
    else:
        c.request("GET", "/api/v1/laps", headers={"Host": f"{LAN}:{port}"})
        r2 = c.getresponse()
        b2 = r2.read()
        check(f"拒绝({first})后同连接下一请求", r2.status, 200, b2[:100])
finally:
    c.close()

print("\n" + "=" * 62)
print(f"结果: {ok} 通过 / {fail} 失败")
print("=" * 62)
srv.shutdown()
srv.server_close()
sys.exit(0 if fail == 0 else 1)
