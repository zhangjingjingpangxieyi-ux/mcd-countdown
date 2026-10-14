"""麦麦倒计时 —— 把麦当劳账户里所有会过期的资产拉成一条时间轴。

只读：不领券、不下单、不抽奖、不改任何用户状态。

用法：
    python countdown.py                 # 终端时间轴
    python countdown.py --json          # 输出结构化 JSON
    python countdown.py --html out.html # 生成可视化页面
    python countdown.py --demo          # 注入示例数据（截图/README 用）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcd_mcp import McdMcpClient, unwrap  # noqa: E402

NOW = datetime.now()

# ---------------------------------------------------------------- 数据模型


def asset(kind, title, deadline, value="", window=None, note="", status=None):
    if status is None and deadline:
        status = "expired" if deadline < NOW else "live"
    return {
        "kind": kind,
        "title": title,
        "deadline": deadline.strftime("%Y-%m-%d %H:%M") if deadline else None,
        "days_left": (deadline - NOW).days if deadline else None,
        "value": value,
        "window": window,
        "note": note,
        "status": status or "live",
    }


KIND_LABEL = {
    "point": "积分",
    "coupon": "优惠券",
    "mall": "积分商城",
    "campaign": "营销活动",
    "lottery": "抽奖活动",
    "prize": "奖品",
}

# ---------------------------------------------------------------- 解析层

VALIDITY_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})"
    r"(?:\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2}))?"
)


def month_end(dt: datetime) -> datetime:
    """返回 dt 所在月份的最后时刻（23:59）。"""
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + timedelta(days=32)).replace(day=1) - timedelta(minutes=1)


def parse_dt(s: str):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def parse_coupons(text: str):
    """券列表是纯 Markdown 报告，逐块正则提取。"""
    out = []
    if not isinstance(text, str):
        return out
    for block in re.split(r"\n(?=##\s)", text):
        m = re.search(r"^##\s+(.+)$", block, re.M)
        if not m:
            continue
        title = m.group(1).strip()
        v = re.search(r"\*\*有效期\*\*[:：]\s*(.+)", block)
        if not v:
            continue
        vm = VALIDITY_RE.search(v.group(1))
        if not vm:
            continue
        end = parse_dt(vm.group(2))
        window = None
        if vm.group(3) and vm.group(4):
            window = f"{vm.group(3)}-{vm.group(4)}"
        price = re.search(r"\*\*优惠\*\*[:：]\s*(.+)", block)
        out.append(asset(
            "coupon", title, end,
            value=(price.group(1).strip() if price else ""),
            window=window,
            note=(f"每日仅 {window} 可用" if window else "全天可用"),
        ))
    return out


def parse_calendar(text: str):
    """活动日历是 Markdown，按日期分组。

    拆成两部分返回：
      - deadlines: 精确到分钟的抢购时刻（这才是真正的倒计时）
      - days:      未来有活动的日期（属于"机会日历"，不混进失效时间轴）
    """
    deadlines, days = [], []
    if not isinstance(text, str):
        return deadlines, days
    cur_date = None
    bucket: dict[str, list[str]] = {}
    for line in text.splitlines():
        dm = re.search(r"^####\s+(\d{4})年(\d{1,2})月(\d{1,2})日\s*(.*)$", line)
        if dm:
            cur_date = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            status = dm.group(4).strip()
            cur_date = cur_date if "往期" not in status else None
            if cur_date and cur_date.date() >= NOW.date():
                bucket.setdefault(cur_date.strftime("%Y-%m-%d"), [])
            continue
        tm = re.search(r"\*\*活动标题\*\*[:：]\s*(.+)", line)
        if tm and cur_date and cur_date.date() >= NOW.date():
            bucket.setdefault(cur_date.strftime("%Y-%m-%d"), []).append(tm.group(1).strip())
    for day, titles in sorted(bucket.items())[:5]:
        days.append({"date": day, "titles": titles[:3], "count": len(titles)})

    # 正文里的精确开售时刻："10月8日10:45开售" / "10:40开抢"
    for m in re.finditer(r"(\d{1,2})月(\d{1,2})日[^\d]{0,6}(\d{1,2}):(\d{2})\s*(开售|开抢|开卖)", text):
        mo, d, h, mi, verb = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)
        try:
            when = datetime(NOW.year, mo, d, h, mi)
        except Exception:
            continue
        if when < NOW - timedelta(days=200):
            when = when.replace(year=NOW.year + 1)
        if when < NOW:
            continue
        deadlines.append(asset("campaign", f"限量抢购 · {verb}", when,
                               value=f"{mo}月{d}日 {h:02d}:{mi:02d}", note="精确到分钟"))
    return deadlines, days


def parse_account(data):
    """积分账户：本月/下月将过期 + 已白白过期。"""
    out = []
    d = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return out, {}
    def f(k):
        try:
            return float(d.get(k) or 0)
        except Exception:
            return 0.0
    info = {
        "available": f("availablePoint"),
        "expired": f("expiredPoint"),
        "this_month": f("currentMouthExpirePoint"),
        "next_month": f("nextMouthExpirePoint"),
        "last_month": f("lastMouthExpirePoint"),
        "accumulative": f("accumulativePoint"),
    }
    if info["this_month"] > 0:
        out.append(asset("point", "本月将过期积分", month_end(NOW),
                         value=f"{info['this_month']:g} 分", note="月底清零"))
    if info["next_month"] > 0:
        out.append(asset("point", "下月将过期积分",
                         month_end(month_end(NOW) + timedelta(days=1)),
                         value=f"{info['next_month']:g} 分", note="下月底清零"))
    return out, info


def parse_mall(data, available: float = 0.0):
    """积分商城：只保留"当前积分就够换"的商品，按下架时间合并成一组。

    理由：换不起的商品对用户没有倒计时意义；而 12 件商品共用同一个
    downTime，逐条列出只会淹没真正紧急的项。
    """
    items = (data or {}).get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    groups: dict[str, dict] = {}
    for it in items:
        end = parse_dt(it.get("downTime", ""))
        if not end or end < NOW:
            continue
        try:
            pt = float(it.get("point") or 0)
        except Exception:
            continue
        if pt <= 0 or pt > available:      # 换不起的不算"你的资产"
            continue
        key = end.strftime("%Y-%m-%d %H:%M")
        g = groups.setdefault(key, {"end": end, "n": 0, "low": pt, "names": []})
        g["n"] += 1
        g["low"] = min(g["low"], pt)
        if len(g["names"]) < 3:
            g["names"].append(it.get("spuName", "商品"))
    out = []
    for g in sorted(groups.values(), key=lambda x: x["end"]):
        out.append(asset(
            "mall", f"积分商城 · {g['n']} 件现在就换得起的商品", g["end"],
            value=f"最低 {g['low']:g} 分起",
            note="、".join(g["names"]) + ("等" if g["n"] > 3 else ""),
        ))
    return out


def parse_lottery(data):
    d = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return []
    end = parse_dt(d.get("endTime", ""))
    if not end:
        return []
    return [asset("lottery", d.get("activityName", "积分抽奖"), end,
                  value=f"单次 {d.get('drawPoint', '?')} 积分",
                  note=d.get("activityStatusText", ""))]


PRIZE_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")


def parse_prizes(data):
    """奖品：提醒文案里有明确日期的进时间轴，没有的进「奖品清单」。

    接口不返回结构化过期时间，只有可选的 timeRemindText 文案；
    提取不到日期时宁可不放时间轴，也不编一个出来。
    """
    out, listing = [], []
    d = (data or {}).get("data") if isinstance(data, dict) else None
    prizes = d.get("prizes") if isinstance(d, dict) else None
    if not isinstance(prizes, list):
        return out, listing
    for p in prizes:
        if not isinstance(p, dict) or p.get("status") != 0:
            continue  # 只关心「可用」状态的奖品
        name = (p.get("name") or "奖品").strip()
        ptype = (p.get("prizeTypeText") or "").strip()
        title = f"{name}（{ptype}）" if ptype and ptype not in name else name
        remind = (p.get("timeRemindText") or "").strip()
        m = PRIZE_DATE_RE.search(remind)
        if m:
            y, mo, dd = (int(x) for x in m.groups())
            try:
                end = datetime(y, mo, dd, 23, 59)
            except ValueError:
                end = None
            if end and 0 <= (end - NOW).days <= 365:
                out.append(asset("prize", title, end, note=remind))
                continue
        listing.append({"name": title,
                        "status": (p.get("statusText") or "可用").strip(),
                        "remind": remind})
    return out, listing


# ---------------------------------------------------------------- 采集


def collect(demo: bool = False):
    if demo:
        return (sorted(DEMO_ASSETS, key=lambda a: a["deadline"]),
                DEMO_INFO, DEMO_DAYS, DEMO_PRIZES)
    c = McdMcpClient()
    points, info = parse_account(unwrap(c.call_tool("query-my-account")))
    items, days, prizes = list(points), [], []
    try:
        items += parse_coupons(unwrap(c.call_tool("query-my-coupons")))
    except Exception as e:
        print(f"[warn] query-my-coupons 采集失败：{e}", file=sys.stderr)
    try:
        items += parse_mall(unwrap(c.call_tool("mall-points-products")), info.get("available", 0.0))
    except Exception as e:
        print(f"[warn] mall-points-products 采集失败：{e}", file=sys.stderr)
    try:
        items += parse_lottery(unwrap(c.call_tool("query-lottery-info")))
    except Exception as e:
        print(f"[warn] query-lottery-info 采集失败：{e}", file=sys.stderr)
    try:
        dl, days = parse_calendar(unwrap(c.call_tool("campaign-calendar")))
        items += dl
    except Exception as e:
        print(f"[warn] campaign-calendar 采集失败：{e}", file=sys.stderr)
    try:
        pitems, prizes = parse_prizes(unwrap(c.call_tool("query-my-prizes")))
        items += pitems
    except Exception as e:
        print(f"[warn] query-my-prizes 采集失败：{e}", file=sys.stderr)
    items = [a for a in items
             if a.get("deadline") and a["days_left"] is not None and 0 <= a["days_left"] <= 365]
    items.sort(key=lambda a: a["deadline"])
    return items, info, days, prizes


# 示例数据：真实账号只有 1 张券、85 分，截图太空，README/演示时补上
DEMO_INFO = {"available": 1240.0, "expired": 868.3, "this_month": 240.0,
             "next_month": 130.0, "last_month": 96.0, "accumulative": 5214.6}
DEMO_DAYS = [
    {"date": (NOW + timedelta(days=1)).strftime("%Y-%m-%d"),
     "titles": ["麦麦脆汁鸡回归", "麦咖啡买一送一"], "count": 2},
    {"date": (NOW + timedelta(days=6)).strftime("%Y-%m-%d"),
     "titles": ["开心乐园餐新玩具上线"], "count": 1},
]
DEMO_ASSETS = [
    asset("coupon", "招行火腿扒麦满分组合", NOW + timedelta(days=2), "¥0 用券价", "05:00-10:29", "每日仅 05:00-10:29 可用"),
    asset("point", "本月将过期积分", NOW + timedelta(days=11), "240 分", None, "月底清零"),
    asset("campaign", "限量抢购 · 开售", NOW + timedelta(days=5, hours=2), "10月18日 10:45", None, "精确到分钟"),
    asset("mall", "麦麦开心乐园餐兑换券", NOW + timedelta(days=19), "680 积分", None, "商品券"),
    asset("point", "下月将过期积分", NOW + timedelta(days=41), "130 分", None, "下月底清零"),
    asset("lottery", "积分抽奖 · 麦麦好运到", NOW + timedelta(days=63), "单次 24 积分", None, "进行中"),
    asset("prize", "麦麦全家桶（门票）", NOW + timedelta(days=33), None, None, "奖品 · 请在有效期内使用"),
]
DEMO_PRIZES = [
    {"name": "麦咖啡买一送一（奖品）", "status": "可用", "remind": ""},
    {"name": "派Day半价券（奖品）", "status": "可用", "remind": "详询门店"},
]

# ---------------------------------------------------------------- 终端输出


def urgency(a):
    d = a["days_left"]
    if d is None:
        return "·"
    if d < 0:
        return "✗"
    if d <= 3:
        return "‼"
    if d <= 7:
        return "!"
    return "·"


def render_terminal(items, info, days=(), prizes=()):
    W = 68
    print("=" * W)
    print("  麦麦倒计时 · 你的麦当劳资产失效时间表")
    print(f"  数据时间 {NOW:%Y-%m-%d %H:%M}   来源：麦当劳 MCP（只读）")
    print("=" * W)

    if info:
        print("\n  损失账单")
        print("  " + "-" * (W - 4))
        print(f"    累计获得      {info['accumulative']:>10,.1f} 分")
        print(f"    当前可用      {info['available']:>10,.1f} 分")
        if info["expired"] > 0:
            print(f"    已白白过期    {info['expired']:>10,.1f} 分   ← 这部分再也回不来了")
        if info["last_month"] > 0:
            print(f"    上月刚过期    {info['last_month']:>10,.1f} 分")

    live = [a for a in items if a["days_left"] is not None and a["days_left"] >= 0]
    print(f"\n  倒计时时间轴（{len(live)} 项）")
    print("  " + "-" * (W - 4))
    if not live:
        print("    暂无即将过期的资产")
    for a in live:
        d = a["days_left"]
        tag = f"{d} 天" if d > 0 else "今天"
        print(f"  {urgency(a)} {a['deadline']}  [{tag:>5}]  {KIND_LABEL[a['kind']]}")
        print(f"       {a['title']}" + (f"  ·  {a['value']}" if a["value"] else ""))
        if a["note"]:
            print(f"       {a['note']}")

    win = [a for a in live if a["kind"] == "coupon" and a["window"]]
    if win:
        print(f"\n  时段提醒（券不是全天都能用）")
        print("  " + "-" * (W - 4))
        now_hm = NOW.strftime("%H:%M")
        for a in win:
            s, e = a["window"].split("-")
            ok = s <= now_hm <= e
            flag = "现在可用" if ok else f"现在不可用（{s}-{e}）"
            print(f"  {'●' if ok else '○'} {a['title']}  {flag}")

    if prizes:
        print(f"\n  奖品清单（官方未给出失效日期，使用前自行确认）")
        print("  " + "-" * (W - 4))
        for p in prizes:
            extra = f"  ·  {p['remind']}" if p.get("remind") else ""
            print(f"  ◆ {p['name']}  [{p['status']}]{extra}")

    if days:
        print(f"\n  机会日历（未来有活动的日子）")
        print("  " + "-" * (W - 4))
        for d in days[:5]:
            names = "、".join(d["titles"]) or "—"
            print(f"  · {d['date']}  {d['count']} 场  {names}")

    print("\n" + "=" * W)


# ---------------------------------------------------------------- HTML 输出

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
background:#FAFAF8;color:#1C1C1A;padding:40px 20px;line-height:1.6}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:22px;font-weight:600;letter-spacing:-.2px}
.sub{font-size:13px;color:#8A8A82;margin-top:6px}
.loss{background:#FFF4F4;border:1px solid #F3C9C9;border-radius:14px;padding:22px 24px;margin:24px 0}
.loss .big{font-size:34px;font-weight:600;color:#C0392B;letter-spacing:-1px}
.loss .lbl{font-size:13px;color:#8A5A54;margin-top:2px}
.loss .row{display:flex;gap:32px;margin-top:14px;flex-wrap:wrap}
.loss .cell .n{font-size:17px;font-weight:600}
.loss .cell .k{font-size:12px;color:#8A8A82}
h2{font-size:15px;font-weight:600;margin:32px 0 14px}
.item{display:flex;gap:14px;background:#fff;border:1px solid #EAEAE4;border-radius:12px;
padding:14px 16px;margin-bottom:10px;align-items:flex-start}
.item.urgent{border-color:#F0B8B8;background:#FFFBFB}
.item.soon{border-color:#F0DCA8;background:#FFFDF7}
.badge{font-size:12px;font-weight:600;border-radius:6px;padding:3px 8px;white-space:nowrap;
background:#F1EFE8;color:#5F5E5A}
.badge.urgent{background:#FCEBEB;color:#A32D2D}
.badge.soon{background:#FAEEDA;color:#854F0B}
.t{font-size:14px;font-weight:500}
.m{font-size:12px;color:#8A8A82;margin-top:3px}
.n{font-size:12px;color:#B45309;margin-top:3px}
.k{font-size:11px;color:#8A8A82;border:1px solid #E4E4DC;border-radius:5px;padding:1px 6px;margin-left:6px}
.h2note{font-size:11px;font-weight:400;color:#A0A09A;margin-left:8px}
.foot{font-size:12px;color:#A0A09A;margin-top:28px;text-align:center}
"""


def render_html(items, info, path, days=(), prizes=()):
    live = [a for a in items if a["days_left"] is not None and a["days_left"] >= 0]
    rows = []
    for a in live:
        d = a["days_left"]
        cls = "urgent" if d <= 3 else ("soon" if d <= 7 else "")
        tag = f"{d} 天" if d > 0 else "今天"
        note = f'<div class="n">{a["note"]}</div>' if a["note"] else ""
        val = f' · {a["value"]}' if a["value"] else ""
        rows.append(
            f'<div class="item {cls}"><span class="badge {cls}">{tag}</span>'
            f'<div><div class="t">{a["title"]}<span class="k">{KIND_LABEL[a["kind"]]}</span></div>'
            f'<div class="m">{a["deadline"]} 到期{val}</div>{note}</div></div>'
        )
    loss = ""
    if info:
        loss = (
            f'<div class="loss"><div class="big">{info["expired"]:,.1f}</div>'
            f'<div class="lbl">积分已白白过期 · 再也回不来</div>'
            f'<div class="row">'
            f'<div class="cell"><div class="n">{info["available"]:,.1f}</div><div class="k">当前可用</div></div>'
            f'<div class="cell"><div class="n" style="color:#C0392B">{info["this_month"]:,.1f}</div><div class="k">本月将过期</div></div>'
            f'<div class="cell"><div class="n">{info["next_month"]:,.1f}</div><div class="k">下月将过期</div></div>'
            f'<div class="cell"><div class="n">{info["accumulative"]:,.1f}</div><div class="k">累计获得</div></div>'
            f'</div></div>'
        )
    cal = ""
    if days:
        cells = "".join(
            f"<div class='item'><span class='badge'>{d['date'][5:]}</span>"
            f"<div><div class='t'>{d['count']} 场活动</div>"
            f"<div class='m'>{'、'.join(d['titles']) or '—'}</div></div></div>"
            for d in days[:5]
        )
        cal = "<h2>机会日历</h2>" + cells

    pz = ""
    if prizes:
        cells = "".join(
            f"<div class='item'><span class='badge'>奖品</span>"
            f"<div><div class='t'>{p['name']}<span class='k'>{p['status']}</span></div>"
            f"<div class='m'>{p['remind'] or '官方未给出失效日期，使用前自行确认'}</div></div></div>"
            for p in prizes[:8]
        )
        pz = ("<h2>奖品清单 <span class='h2note'>官方未给出失效日期</span></h2>" + cells)

    html = (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>麦麦倒计时</title><style>" + CSS + "</style></head><body><div class='wrap'>"
        "<h1>麦麦倒计时</h1>"
        f"<div class='sub'>你的麦当劳资产失效时间表 · {NOW:%Y-%m-%d %H:%M} · 数据源：麦当劳 MCP（只读）</div>"
        + loss +
        f"<h2>即将失效 · {len(live)} 项</h2>" + ("".join(rows) or "<div class='m'>暂无</div>")
        + pz + cal +
        "<div class='foot'>只读采集，不会领券、下单或抽奖</div>"
        "</div></body></html>"
    )
    Path(path).write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------- PNG 截图


def find_edge():
    import shutil
    exe = shutil.which("msedge")
    if exe:
        return exe
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
              "/usr/bin/microsoft-edge"):
        if Path(p).exists():
            return p
    return None


def shot_png(html_path, png_path, size="800,1600"):
    """用系统自带 Edge 无头模式截图，不引入第三方依赖。"""
    exe = find_edge()
    if not exe:
        print("[warn] 未找到 Edge，无法自动截图；请手动打开 HTML 截图", file=sys.stderr)
        return None
    import subprocess
    url = Path(html_path).resolve().as_uri()
    subprocess.run(
        [exe, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--screenshot={Path(png_path).resolve()}",
         f"--window-size={size}", url],
        check=False, capture_output=True, timeout=90)
    if Path(png_path).exists():
        return png_path
    print("[warn] Edge 截图未产出文件", file=sys.stderr)
    return None


# ---------------------------------------------------------------- 入口


def main():
    ap = argparse.ArgumentParser(description="麦麦倒计时：麦当劳账户资产失效时间轴")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--html", metavar="PATH", help="生成可视化 HTML")
    ap.add_argument("--png", metavar="PATH", help="生成 HTML 后再自动截图（需系统自带 Edge）")
    ap.add_argument("--png-size", default="800,1600", metavar="WxH",
                    help="截图尺寸，默认 800x1600")
    ap.add_argument("--demo", action="store_true", help="使用示例数据（截图用）")
    args = ap.parse_args()

    items, info, days, prizes = collect(demo=args.demo)

    if args.json:
        print(json.dumps({"generated_at": NOW.isoformat(timespec="minutes"),
                          "account": info, "assets": items, "calendar": days,
                          "prizes": prizes},
                         ensure_ascii=False, indent=2))
    elif args.html or args.png:
        html_path = args.html or (str(Path(args.png).with_suffix(".html"))
                                  if args.png else "report.html")
        render_html(items, info, html_path, days, prizes)
        print(f"已生成 {html_path}")
        if args.png:
            out = shot_png(html_path, args.png, args.png_size)
            if out:
                print(f"已生成 {out}")
    else:
        render_terminal(items, info, days, prizes)


if __name__ == "__main__":
    main()
