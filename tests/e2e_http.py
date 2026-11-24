"""
端到端试跑脚本：对运行中的 Flask 服务（默认 http://127.0.0.1:8000）
走一遍完整业务流，并打印关键校验结果。
用法：python tests/e2e_http.py
"""
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
       "2026-01-09", "2026-01-12", "2026-01-13"]
# (code, date, price)
PRICES = [
    ("IF2601", "2026-01-05", 38000), ("IF2601", "2026-01-06", 38100),
    ("IF2601", "2026-01-07", 38200),
    ("IF2602", "2026-01-07", 38300), ("IF2602", "2026-01-08", 38400),
    ("IF2602", "2026-01-09", 38500),
    ("IF2603", "2026-01-09", 38750), ("IF2603", "2026-01-12", 38800),
    ("IF2603", "2026-01-13", 38900),
]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 不自动跟随，让调用方直接看到 302


opener = urllib.request.build_opener(NoRedirect)


def post(path, data):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read().decode("utf-8-sig")


def main():
    checks = []

    # 1) 交易日历
    st, _ = post("/calendar", {"dates": "\n".join(CAL)})
    checks.append(("保存交易日历", st == 302))

    # 2) 换月计划：初始 IF2601，01-08 切 IF2602，01-12 切 IF2603
    st, _ = post("/plan", {
        "initial_code": "IF2601",
        "switch_date": ["2026-01-08", "2026-01-12"],
        "switch_code": ["IF2602", "IF2603"],
    })
    checks.append(("登记换月计划", st == 302))

    # 取合约 id（从 /prices 页面解析 option value）
    st, html = get("/prices")
    import re
    cid = {code: cid_ for cid_, code, _ in re.findall(
        r'value="(\d+)">([^（]+)（ord=(\d+)）', html)}
    assert len(cid) == 3, f"合约数异常: {cid}"

    # 3) 补录全部报价
    for code, day, px in PRICES:
        st, _ = post("/prices", {"contract_id": cid[code], "date": day, "price": px})
        assert st == 302, (code, day, st)
    checks.append(("补录 9 笔报价", True))

    # 4) 取数：基准日 01-13
    q = "/query?start=2026-01-05&end=2026-01-13&base=2026-01-13"
    st, html = get(q)
    checks.append(("首次取数 HTTP 200", st == 200))
    # 首日连续价 = 38000 +(38300-38200)+(38750-38500)= 38000+100+250=38350
    checks.append(("首日连续价 38350", "38350" in html))
    checks.append(("末日连续价 38900", "38900" in html))

    # 5) 下载 CSV
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    lines = csv_text.strip().splitlines()
    checks.append(("CSV 下载", st == 200 and len(lines) == 8 and lines[0].startswith("交易日")))
    first = lines[1].split(",")
    checks.append(("CSV 首行连续价 38350", first[6] == "38350"))
    checks.append(("CSV 标明原价实报", first[3] == "实报"))

    # 6) 改价：IF2602 在 01-07（roll1 参考日）38300 → 38360
    st, _ = post("/prices", {"contract_id": cid["IF2602"],
                             "date": "2026-01-07", "price": 38360})
    checks.append(("更正报价", st == 302))
    # 新 delta1 = 160，首日连续价 = 38000+160+250 = 38410
    st, html = get(q)
    checks.append(("改价后联动连续价 38410", "38410" in html))
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    checks.append(("改价后 CSV 同步", "38410" in csv_text))

    # 7) 缺价场景：撤下 IF2603 在 01-09 的实报（roll2 参考日新合约价），
    #    走应用删除路由（同事务重算换月价差与 Fenwick），再取数验证留空与提示
    st, _ = post("/prices/delete", {"contract_id": cid["IF2603"], "date": "2026-01-09"})
    checks.append(("撤下 01-09 实报", st == 302))
    st, html = get(q)
    checks.append(("缺换月报价时留空", "待补报价：IF2603(2026-01-09)" in html))
    checks.append(("指出最早缺价换月 IF2602 → IF2603", "IF2602 → IF2603" in html))

    # ======================================================================
    # 8) 价差表补数（网页实测补报对取数的影响）
    # ======================================================================
    # 8a) 01-09 保存价差 IF2603 - IF2602 = 250（交易所同日公布）
    st, _ = post("/spreads", {
        "date": "2026-01-09",
        "code_a": ["IF2602"], "code_b": ["IF2603"], "spread": [250],
    })
    checks.append(("保存 01-09 价差表", st == 302))
    # 推算 IF2603 = 38500 + 250 = 38750；roll2 delta 恢复为 250 且标记含推算价
    st, html = get("/prices?spread_date=2026-01-09")
    checks.append(("页面显示推算价 38750", "38750" in html))
    checks.append(("页面显示推算成立", "推算成立" in html))
    st, html = get(q)
    checks.append(("补数后首日连续价恢复 38410", "38410" in html))
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    csv_lines = csv_text.strip().splitlines()
    first_cells = csv_lines[1].split(",")
    checks.append(("CSV 连续价恢复", first_cells[6] == "38410"))
    # 01-05/06/07 的换月调整区间含 roll2 → 标记“是”
    checks.append(("CSV 标明换月调整含推算价", first_cells[5] == "是"))

    # 8b) 换表：价差 250 → 260，推算价应整体替换为 38760，delta2=260
    st, _ = post("/spreads", {
        "date": "2026-01-09",
        "code_a": ["IF2602"], "code_b": ["IF2603"], "spread": [260],
    })
    checks.append(("替换 01-09 价差表", st == 302))
    st, html = get("/prices?spread_date=2026-01-09")
    checks.append(("换表后推算价 38760", "38760" in html))
    st, html = get(q)
    # 首日 = 38000 + 160 + 260 = 38420
    checks.append(("换表后连续价 38420", "38420" in html))
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
    checks.append(("换表后 CSV 同步 38420", "38420" in csv_text))

    # 8c) 同值实报替代推算价：补录 IF2603=38760 实报，来源标记应切回实报
    st, _ = post("/prices", {"contract_id": cid["IF2603"],
                             "date": "2026-01-09", "price": 38760})
    checks.append(("同值实报补录", st == 302))
    st, html = get("/prices?spread_date=2026-01-09")
    checks.append(("推算行被实报替代（不再显示推算成立行）", "推算成立" not in html))
    st, csv_text = get("/download?start=2026-01-09&end=2026-01-09&base=2026-01-13")
    cells = csv_text.strip().splitlines()[1].split(",")
    checks.append(("原价来源切回实报", cells[3] == "实报"))
    # 换月价差此时两腿都是实报，roll2 不再标记推算价
    st, csv_text = get("/download?start=2026-01-05&end=2026-01-05&base=2026-01-13")
    checks.append(("换月调整标记撤回（否）",
                   csv_text.strip().splitlines()[1].split(",")[5] == "否"))

    # 8d) 清空价差表：推算撤下；但实报已在，供数不受影响
    st, _ = post("/spreads", {"date": "2026-01-09",
                              "code_a": [], "code_b": [], "spread": []})
    checks.append(("清空 01-09 价差表", st == 302))
    st, html = get("/prices?spread_date=2026-01-09")
    checks.append(("清空后无推算记录", "推算成立" not in html))
    st, html = get(q)
    checks.append(("实报仍在，连续价 38420 不变", "38420" in html))

    # 8e) 无实报组：撤下 01-09 两笔实报后只给 IF2602-IF2603 价差，应撤下并提示
    post("/prices/delete", {"contract_id": cid["IF2602"], "date": "2026-01-09"})
    post("/prices/delete", {"contract_id": cid["IF2603"], "date": "2026-01-09"})
    st, _ = post("/spreads", {
        "date": "2026-01-09",
        "code_a": ["IF2602"], "code_b": ["IF2603"], "spread": [250],
    })
    checks.append(("无实报组保存价差表", st == 302))
    st, html = get("/prices?spread_date=2026-01-09")
    checks.append(("无实报提示撤下原因", "没有任何实报" in html and "已撤下" in html))
    st, html = get(q)
    checks.append(("无实报时连续价留空", "待补报价" in html))

    # 8f) 矛盾组：补 IF2602=38500 实报，再给两条矛盾路径
    st, _ = post("/prices", {"contract_id": cid["IF2602"],
                             "date": "2026-01-09", "price": 38500})
    checks.append(("补锚点实报", st == 302))
    st, _ = post("/spreads", {
        "date": "2026-01-09",
        "code_a": ["IF2602", "IF2602", "IF2603"],
        "code_b": ["IF2603", "IF2603", "IF2602"],
        "spread": [250, 260, -250],
    })
    # 同对重复直接被表单校验拒绝
    checks.append(("重复合约对被拒", st == 302))
    st, html = get("/prices?spread_date=2026-01-09")
    # 换成不重复但路径矛盾的表：IF2603-IF2602=250 与 IF2602-IF2603=-260 矛盾
    st, _ = post("/spreads", {
        "date": "2026-01-09",
        "code_a": ["IF2602", "IF2603"],
        "code_b": ["IF2603", "IF2602"],
        "spread": [250, -260],
    })
    checks.append(("矛盾价差表可保存但推算撤下", st == 302))
    st, html = get("/prices?spread_date=2026-01-09")
    checks.append(("矛盾原因提示", "矛盾" in html and "已撤下" in html))
    st, html = get(q)
    checks.append(("矛盾时连续价留空、实报照用",
                   "待补报价" in html and "38500" in html))

    # 9) 基准日早于结束日应被拒（页面渲染错误提示，HTTP 200 + flash）
    st, html = get("/query?start=2026-01-05&end=2026-01-13&base=2026-01-08")
    checks.append(("拒绝基准日早于结束日", st == 200 and "不能早于结束日" in html))

    print("\n===== 端到端试跑结果 =====")
    ok = True
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("==========================")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
