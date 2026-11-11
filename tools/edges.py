"""매수 에지 — 전 상장사(보통주) 중 지금 매수 에지가 켜진 종목 (사용자 요청 2026-11-11).

리포트 로그 종목만 보던 에지(build.py edge_states)를 전 상장사로 넓힌다. 매도 에지는 너무 많아 싣지 않는다(사용자 결정).
  - 일봉: 네이버 fchart(build.fetch_bars), 시총: 네이버 m.stock 시가총액 순위(억원)
  - 장중 값은 하루 봉이 덜 끝나 에지가 켜졌다 꺼질 수 있으므로 장 마감(15:30) 뒤 실행에서만 계산한다.
    10:00 실행은 건너뛰고 앞 저장본을 그대로 둔다. --force 면 시각과 상관없이 계산.
  - 출력 data/edges.json: 종목마다 최근 122일 종가(화면이 5·10·20·60일선을 직접 계산) + 최근 62일 에지 문자열.
  - 리포트 로그에 없는 매수 에지 종목은 data/charts/{code}.json(종목 페이지와 같은 모양, 리포트 없음)도 쓴다 → 차트만 보는 종목 페이지.
    data/stocks 가 아니라 따로 두는 이유: analyze.py(AI 정리)가 data/stocks 를 전부 돌기 때문. 목록에서 빠진 종목 파일은 지운다.

실행: Python39-32 python tools/edges.py [--force]
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

import build

ROOT, DATA, KST = build.ROOT, build.DATA, build.KST
OUT = DATA / "edges.json"
CHARTS = DATA / "charts"
CLOSES = 122          # 화면 차트 62일 + 60일선 워밍업
SHOW = 62
NV_H = {"User-Agent": "Mozilla/5.0 (iPhone) Mobile", "Referer": "https://m.stock.naver.com/"}


def market_caps():
    """{code: 시총(억원)} — 코스피·코스닥 시가총액 순위 전부(100개씩, 약 29번)."""
    out = {}
    for mk in ("KOSPI", "KOSDAQ"):
        page, total = 1, None
        while total is None or (page - 1) * 100 < total:
            j = requests.get(f"https://m.stock.naver.com/api/stocks/marketValue/{mk}",
                             params={"page": page, "pageSize": 100}, headers=NV_H, timeout=20).json()
            total = j.get("totalCount") or 0
            for s in j.get("stocks") or []:
                v = (s.get("marketValue") or "").replace(",", "")
                if v.isdigit():
                    out[s["itemCode"]] = int(v)
            page += 1
            time.sleep(0.2)
    return out


def write_charts(items, bars, now):
    """리포트 없는 매수 에지 종목의 차트 화면용 파일 — build.py 가 쓰는 data/stocks/{code}.json 과 같은 키, 리포트만 비움."""
    mpath = DATA / "market.json"
    mdata = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    CHARTS.mkdir(parents=True, exist_ok=True)
    keep = set()
    for x in items:
        b = bars[x["code"]]
        for b_ in b:                                         # 거래정지일: 시가·고가·저가 0 → 종가로 (build.py 와 같게)
            if not (b_["o"] and b_["h"] and b_["l"]):
                b_["o"] = b_["h"] = b_["l"] = b_["c"]
        mc = {r["d"]: r["c"] for r in mdata.get(x["market"], [])}
        st = build.edge_states(b)
        (CHARTS / f"{x['code']}.json").write_text(json.dumps({
            "code": x["code"], "name": x["name"], "updated": now.strftime("%Y-%m-%d %H:%M"), "market": x["market"],
            "chart_only": True, "cap": x["cap"],
            "signals": build.strength_signals_mc(b, mc) if mc else [], "sells": build.sell_signals(b),
            "edges": "".join({1: "+", -1: "-", 0: "."}[v] for v in st),
            "close": b[-1]["c"], "close_date": b[-1]["d"], "consensus": None, "brokers": [],
            "prices": b, "reports": []}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        keep.add(f"{x['code']}.json")
    for p in CHARTS.glob("*.json"):
        if p.name not in keep:
            p.unlink()


def main():
    now = datetime.now(KST)
    if "--force" not in sys.argv and (now.hour, now.minute) < (15, 30):
        print("장중이라 매수 에지는 건너뜀 (장 마감 뒤 실행에서 계산)")
        return
    uni = json.loads(build.UNIVERSE.read_text(encoding="utf-8"))
    stocks = {c: v for c, v in uni.items() if c[-1] == "0" and "스팩" not in v["name"]}    # 보통주만
    codes = sorted(stocks)
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        bars = dict(zip(codes, ex.map(build.fetch_bars, codes)))
    ok = {c: b for c, b in bars.items() if b and len(b) > 61}
    last = max(b[-1]["d"] for b in ok.values())
    try:
        caps = market_caps()
    except Exception as e:                      # 시총은 정렬·필터용 — 못 받아도 목록은 낸다
        print(f"시총 못 받음: {type(e).__name__}")
        caps = {}
    inlog = {x["code"] for x in json.loads((DATA / "index.json").read_text(encoding="utf-8"))["stocks"]}

    items = []
    for c, b in ok.items():
        if b[-1]["d"] != last:                  # 거래정지 등으로 오늘 봉이 없으면 뺀다
            continue
        st = build.edge_states(b)
        e = build.edge_now(st, b)
        if not e or e["s"] != 1:
            continue
        i0 = len(b) - e["run"]                  # 켜진 날
        base = b[i0 - 1]["c"]                   # 켜지기 전날 종가 대비
        tv = sum(x["c"] * x["v"] for x in b[-20:]) / 20 / 1e8
        items.append({"code": c, "name": stocks[c]["name"], "market": stocks[c]["market"], "cap": caps.get(c),
                      "run": e["run"], "since": e["since"], "chg": round((b[-1]["c"] / base - 1) * 100, 1),
                      "tv": round(tv, 1), "inlog": c in inlog,
                      "c": [x["c"] for x in b[-CLOSES:]],
                      "e": "".join("+" if v == 1 else "-" if v == -1 else "." for v in st[-SHOW:])})
    items.sort(key=lambda x: (x["run"], -(x["cap"] or 0)))
    write_charts([x for x in items if not x["inlog"]], ok, now)
    OUT.write_text(json.dumps({"updated": now.strftime("%Y-%m-%d %H:%M"), "d": last, "universe": len(ok),
                               "items": items}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    miss = len(codes) - len(ok)
    print(f"매수 에지 {len(items)}종목 (오늘 켜짐 {sum(x['run'] == 1 for x in items)}) / {len(ok)}종목 {last} 기준"
          f" · {time.time() - t0:.0f}초" + (f" · 일봉 못 받음 {miss}" if miss else ""))


if __name__ == "__main__":
    main()
