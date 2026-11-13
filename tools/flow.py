# -*- coding: utf-8 -*-
"""투자자별 순매수(외국인·기관·개인, 주) — 네이버 모바일 /trend. 종목 페이지 차트 아래 '수급' 칸용.

  - 대상: data/stocks + data/charts 에 있는 종목 전부
  - 원본은 raw/flow/{code}.json 에 날짜별로 쌓고(PC 보관), data/flow/{code}.json 에는 차트 창(6개월)만 쓴다
  - 매 실행 최근 60거래일을 다시 받아 덮어쓴다 → 장중·잠정 값이 뒤에 확정치로 바뀐다
  - 18:00 전에는 오늘 줄을 버린다(장중 잠정치가 섞이지 않게)
  - 처음 받는 종목은 날짜를 거슬러 6개월을 채운다

    python tools/flow.py            # 전 종목
    python tools/flow.py 000270     # 한 종목
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = ROOT / "raw" / "flow"
OUT = DATA / "flow"
KST = timezone(timedelta(hours=9))
NV_H = {"User-Agent": "Mozilla/5.0 (iPhone) Mobile", "Referer": "https://m.stock.naver.com/"}
PAGE = 60            # /trend 한 번에 최대 60거래일 (70 은 빈 응답)
KEEP_DAYS = 190      # data/flow 에 남길 기간 — 차트 6개월 + 여유
CONFIRM = "18:00"    # 이 시각 전이면 오늘 줄은 잠정치라 버린다


def _n(s):
    return int(str(s).replace(",", "").replace("+", "") or 0)


def fetch(code, before=None):
    """최근(또는 before 이전) PAGE 거래일. 실패하면 None."""
    params = {"pageSize": PAGE}
    if before:
        params["bizdate"] = before
    for attempt in range(3):
        try:
            r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend", params=params,
                             headers=NV_H, timeout=15)
            r.raise_for_status()
            return [{"d": f"{x['bizdate'][:4]}-{x['bizdate'][4:6]}-{x['bizdate'][6:]}",
                     "f": _n(x["foreignerPureBuyQuant"]), "o": _n(x["organPureBuyQuant"]),
                     "i": _n(x["individualPureBuyQuant"])} for x in r.json()]
        except Exception:
            time.sleep(1 + attempt)
    return None


def one(code, now):
    rp = RAW / f"{code}.json"
    rows = {r["d"]: r for r in json.loads(rp.read_text(encoding="utf-8"))} if rp.exists() else {}
    got = fetch(code)
    if got is None:
        return code, False
    first = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for r in got:
        rows[r["d"]] = r
    # 처음 받는 종목: 6개월이 찰 때까지 거슬러 받는다 (상장 6개월 미만이면 덜 찬 쪽에서 멈춤)
    while rows and min(rows) > first and len(got) == PAGE:
        prev = (datetime.strptime(min(rows), "%Y-%m-%d") - timedelta(days=1)).strftime("%Y%m%d")
        time.sleep(0.3)
        got = fetch(code, prev) or []
        if not got or min(r["d"] for r in got) >= min(rows):
            break
        for r in got:
            rows[r["d"]] = r
    today = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") < CONFIRM:
        rows.pop(today, None)
    out = sorted(rows.values(), key=lambda r: r["d"])
    RAW.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    keep = [[r["d"], r["f"], r["o"], r["i"]] for r in out if r["d"] >= first]
    op = OUT / f"{code}.json"
    body = {"unit": "주", "cols": ["d", "외국인", "기관", "개인"], "rows": keep}
    old = json.loads(op.read_text(encoding="utf-8")) if op.exists() else None
    if not old or old.get("rows") != keep:            # 값이 그대로면 파일을 건드리지 않는다(불필요한 커밋 방지)
        body["updated"] = now.strftime("%Y-%m-%d %H:%M")
        op.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    return code, True


def main():
    now = datetime.now(KST)
    codes = sys.argv[1:] or sorted({p.stem for d in ("stocks", "charts") for p in (DATA / d).glob("*.json")})
    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda c: one(c, now), codes))
    fail = [c for c, ok in res if not ok]
    # 목록에서 빠진 종목의 화면용 파일은 지운다(원본은 raw 에 남김)
    live = {p.stem for d in ("stocks", "charts") for p in (DATA / d).glob("*.json")}
    if not sys.argv[1:]:
        for p in OUT.glob("*.json"):
            if p.stem not in live:
                p.unlink()
    print(f"수급 {len(codes) - len(fail)}/{len(codes)}종목" + (f" · 실패 {len(fail)}: {fail[:10]}" if fail else ""))
    if len(fail) > len(codes) / 2:
        sys.exit(2)


if __name__ == "__main__":
    main()
