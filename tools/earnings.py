# -*- coding: utf-8 -*-
"""실적 캘린더: 스탁이지 '실적발표' 일정 + 발표 전 체크(이번 분기 추정치·주가 흐름·지난 5분기) + 발표 후 잠정 실적.
daily.py 가 build.py 다음에 실행한다 (Python39-32). 결과는 data/earnings.json.

  · 일정        스탁이지 /stockdata/api/v1/market-calendar?category=earnings   (로그인 세션 필요)
  · 잠정 실적    스탁이지 /stockdata/api/v1/market-calendar/earnings-results   (시총 5,000억 이상만, 로그인)
  · 추정치·실적  네이버 m.stock /api/stock/{code}/finance/quarter — 최근 5분기 + 다음 분기 컨센서스(억원), 공개.
                스탁이지 info-tab 은 연속 호출 시 내 세션을 끊어서(2026-10-28 10여 건 뒤 차단) 쓰지 않는다.
                네이버는 발표되면 그 분기를 실적으로 바꾸므로 발표 전 추정치를 raw/earnings/est_hist.json 에 매일 남긴다.
  · 주가        네이버 일봉(build.fetch_bars) + data/market.json — company-log 와 같은 기준

세션은 C:\\ClaudeCode\\.stockeasy_session.json (_api-maps/stockeasy_session_import.py 로 저장).
세션이 끊기면 exit 3 — 기존 earnings.json 은 그대로 두고 daily.py 가 알림을 보낸다.

2026-11-06: 잠정 실적 목록은 '요청 시점' 시총 5,000억 이상만 오고(서버 고정) 지난 날짜는 캐시라,
  시총이 모자란 종목은 영영 결과가 빠졌다(제주항공 등 35건). → 일정의 DART 접수번호로 공시 원문
  (영업(잠정)실적(공정공시) 정형 서식)을 읽어 같은 모양의 result 를 채운다(src="dart"). 접수된 공시는 안 바뀌니 영구 캐시.
"""
import html
import io
import json
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

ROOT, DATA, KST = build.ROOT, build.DATA, build.KST
SESSION = Path(r"C:\ClaudeCode\.stockeasy_session.json")
CACHE = ROOT / "raw" / "earnings"
OUT = DATA / "earnings.json"
BASE = "https://stockeasy.intellio.kr"
PAST_DAYS = 28            # 지난 발표는 4주 전 월요일부터 담는다 (화면은 이번 주+다음 주 펼침)
MKT = {"코스닥": "KOSDAQ"}  # 나머지(유가증권·코스피)는 KOSPI
SRC = {"dart_settlement_forecast": "예고 공시", "dart_actual_earnings": "실적 공시", "admin_manual": "스탁이지 등록",
       "earnings_results": "잠정 실적"}


def session():
    c = json.loads(SESSION.read_text(encoding="utf-8"))["cookies"]
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150.0",
                      "Accept": "application/json", "Referer": BASE + "/"})
    for k, v in c.items():
        s.cookies.set(k, v, domain="stockeasy.intellio.kr", path="/")
    return s


def get(s, path, **params):
    for attempt in range(3):
        try:
            r = s.get(BASE + path, params=params, timeout=30)
            if r.status_code == 401:
                raise PermissionError("스탁이지 로그인 세션 만료")
            r.raise_for_status()
            return r.json()
        except PermissionError:
            raise
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def calendar(s, today):
    """일정 목록 + 발표된 날짜들. 목록(items)엔 공시된 '예정'만 있고, 이미 발표된 날의 종목은
    earnings_days(날짜별 건수)만 오므로 그 날짜는 잠정 실적 목록으로 채운다."""
    items, days = [], set()
    for dm in (-1, 0, 1):
        y, m = today.year, today.month + dm
        y, m = (y - 1, 12) if m == 0 else (y + 1, 1) if m == 13 else (y, m)
        j = get(s, "/stockdata/api/v1/market-calendar", year=y, month=m, category="earnings")
        items += j["items"]
        days |= set(j.get("earnings_days") or {})
        time.sleep(0.3)
    return items, days


def results(s, d, today):
    """그날 잠정 실적(시총 5,000억 이상). 지난 날짜는 바뀌지 않으니 캐시."""
    p = CACHE / f"results_{d}.json"
    if d < today.isoformat() and p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    out, off = [], 0
    while True:
        j = get(s, "/stockdata/api/v1/market-calendar/earnings-results",
                date=d, offset=off, limit=50, sort="marketCap", kind="all")
        out += j.get("items") or []
        off += 50
        if off >= (j.get("total") or 0):
            break
        time.sleep(0.3)
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


NV_ROWS = {"매출액": "rev", "영업이익": "op", "당기순이익": "net", "영업이익률": "opm"}


def naver_fin(code, today):
    """네이버 분기 재무: {YYYYMM: {rev, op, net, opm, cons}} (억원·%). 하루 1번만 받는다."""
    p = CACHE / f"nv_{code}.json"
    if p.exists():
        j = json.loads(p.read_text(encoding="utf-8"))
        if j.get("fetched") == today.isoformat():
            return j["q"]
    r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter",
                     headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile", "Referer": "https://m.stock.naver.com/"}, timeout=20)
    r.raise_for_status()
    fi = r.json().get("financeInfo") or {}
    q = {t["key"]: {"cons": t.get("isConsensus") == "Y"} for t in fi.get("trTitleList") or []}
    for row in fi.get("rowList") or []:
        k = NV_ROWS.get(row.get("title"))
        for key, cell in (row.get("columns") or {}).items() if k else ():
            v = (cell or {}).get("value", "").replace(",", "")
            if key in q:
                q[key][k] = float(v) if re.fullmatch(r"-?\d+(\.\d+)?", v) else None
    p.write_text(json.dumps({"fetched": today.isoformat(), "q": q}, ensure_ascii=False), encoding="utf-8")
    return q


def period_end(item):
    """이번 발표가 어느 분기 실적인지: 공시 내용의 결산기간 끝, 없으면 발표일 직전 분기 말."""
    m = re.search(r"결산기간(?: 종료)?\s*(?:\d{4}-\d\d-\d\d~)?(\d{4})-(\d\d)-\d\d", item.get("detail") or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    d = date.fromisoformat(item["record_date"])
    q = (d.month - 1) // 3 * 3
    return (d.year - 1, 12) if q == 0 else (d.year, q)


def pct(a, b):
    return round((a / b - 1) * 100, 1) if a is not None and b else None


FIELDS = ("rev", "op", "net", "opm")


def growth(a, b):
    """증감률. 어느 한쪽이 0 이하(적자)면 %가 뜻을 뒤집으므로 말로 쓴다."""
    if a is None or b is None:
        return None
    if a > 0 and b > 0:
        return pct(a, b)
    if b <= 0 < a:
        return "흑자 전환"
    if a <= 0 < b:
        return "적자 전환"
    return "적자 축소" if a > b else "적자 확대" if a < b else "적자 지속"


def checks(code, pe, q, hist, today):
    """발표 전 체크: 이번 분기 추정치(전년·전분기 실적 대비, 기록 시작 뒤 변화) + 지난 5분기.
    q = naver_fin 결과, hist = 발표 전 추정치 기록 {code|YYYYMM: {first, last}} — 여기서 갱신한다."""
    y, m = pe
    key = f"{y}{m:02d}"
    pm = (y, m - 3) if m > 3 else (y - 1, m + 9)
    ya, qa, cur = q.get(f"{y - 1}{m:02d}"), q.get(f"{pm[0]}{pm[1]:02d}"), q.get(key)
    hk = f"{code}|{key}"
    if cur and cur["cons"] and cur.get("op") is not None:          # 아직 발표 전 → 추정치 기록
        snap = {"d": today.isoformat(), **{k: cur.get(k) for k in FIELDS}}
        h = hist.setdefault(hk, {"first": snap})
        h["last"] = snap
    out = {}
    h = hist.get(hk)
    if h:
        e = h["last"]
        out["est"] = {**{k: e.get(k) for k in FIELDS}, "d": e["d"],
                      "yoy": {k: growth(e.get(k), ya.get(k)) if ya else None for k in ("rev", "op", "net")},
                      "qoq": {k: growth(e.get(k), qa.get(k)) if qa else None for k in ("rev", "op", "net")}}
        if h["first"]["d"] != e["d"]:
            out["est"]["chg"] = {"since": h["first"]["d"], **{k: growth(e.get(k), h["first"].get(k)) for k in ("rev", "op", "net")}}
    if cur and not cur["cons"]:
        out["act"] = {k: cur.get(k) for k in FIELDS}
    past = sorted((k for k, v in q.items() if not v["cons"] and k < key), reverse=True)[:5]
    out["past"] = [{"p": f"{k[:4]}.{(int(k[4:]) - 1) // 3 + 1}Q" if int(k[4:]) % 3 == 0 else f"{k[:4]}.{k[4:]}",
                    **{f: q[k].get(f) for f in FIELDS}} for k in past]
    return out


# ── DART 잠정실적 공시 원문 (시총 5,000억 미만 폴백) ─────────────────────────────
DART_ENV = Path(r"C:\ClaudeCode\telegram-report-bot\.env")     # DART_API_KEY 는 리포트 봇과 같이 쓴다
DART_ROWS = {"매출액": "rev", "영업이익": "op", "당기순이익": "net"}
DART_UNIT = {"억원": 1, "백만원": 0.01, "천원": 0.00001, "원": 1e-8}


def dart_key():
    for ln in DART_ENV.read_text(encoding="utf-8").splitlines():
        if ln.startswith("DART_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise RuntimeError("DART_API_KEY 없음")


def dart_parse(xml):
    """영업(잠정)실적 서식: 항목 · 당해실적 · 당기 · 전기 · 전기대비% · 전환여부 · 전년동기 · ... (억원으로 환산)."""
    m = re.search(r"단위\s*:\s*(억원|백만원|천원|원)", re.sub(r"<[^>]+>", " ", xml))
    if not m:
        return None
    mul = DART_UNIT[m.group(1)]
    c = [re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", " ", td)))
         for td in re.findall(r"<T[DEH][^>]*>(.*?)</T[DEH]>", xml, re.S | re.I)]

    def num(i):
        s = c[i].replace(",", "") if i < len(c) else ""
        return round(float(s) * mul, 1) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None
    out = {}
    for i, t in enumerate(c):
        k = DART_ROWS.get(t)
        if k and k not in out:
            j = i + 2 if i + 1 < len(c) and c[i + 1] == "당해실적" else i + 1
            if num(j) is not None:
                out[k] = {"cur": num(j), "prev": num(j + 1), "yago": num(j + 4)}
    return out if "rev" in out and "op" in out else None


def dart_doc(rc, key):
    """접수번호 → 파싱 결과. 공시는 안 바뀌므로 성공한 것만 영구 캐시."""
    p = CACHE / f"dart_{rc}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    r = requests.get("https://opendart.fss.or.kr/api/document.xml", params={"crtfc_key": key, "rcept_no": rc}, timeout=30)
    r.raise_for_status()
    if r.content[:2] != b"PK":
        raise RuntimeError(f"DART 문서 없음: {r.text[:80]}")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    got = dart_parse(z.read(z.namelist()[0]).decode("utf-8", "ignore"))
    if not got:
        raise RuntimeError("서식 파싱 실패")
    p.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
    return got


def eok_str(v):
    """스탁이지 value 와 같은 표기: 7,818억 · 23조8270억."""
    if v is None:
        return None
    a, s = abs(v), "-" if v < 0 else ""
    if a >= 10000:
        return f"{s}{int(a // 10000)}조{round(a % 10000)}억"
    return f"{s}{round(a):,}억"


def dart_result(d, rc, est):
    """DART 파싱 결과 → 화면용 result (스탁이지 earnings_results 와 같은 모양). 예상 대비는 네이버 추정치 기준."""
    est = est or {}
    names = {"rev": ("revenue", "매출"), "op": ("operating_profit", "영업이익"), "net": ("net_profit", "순이익")}
    m, vs = {}, {}
    for k, (rk, _) in names.items():
        x = d.get(k) or {}
        e = est.get(k)
        vs[k] = pct(x.get("cur"), e) if x.get("cur") is not None and e and e > 0 and x["cur"] > 0 else None
        m[rk] = {"value": eok_str(x.get("cur")), "estimate": eok_str(e), "vs_estimate_pct": vs[k],
                 "qoq": growth(x.get("cur"), x.get("prev")), "yoy": growth(x.get("cur"), x.get("yago"))}
    part = [(names[k][1], vs[k]) for k in ("rev", "op") if vs[k] is not None]
    if not part:
        head = f"매출 {eok_str(d['rev']['cur'])} · 영업이익 {eok_str(d['op']['cur'])}"
    elif len(part) == 2 and (part[0][1] >= 0) == (part[1][1] >= 0):
        head = f"매출·영업이익 모두 예상 {'상회' if part[0][1] >= 0 else '하회'}({part[0][1]:+.1f}%·{part[1][1]:+.1f}%)"
    else:
        head = "·".join(f"{n} 예상 {'상회' if v >= 0 else '하회'}({v:+.1f}%)" for n, v in part)
    return {"headline": head, "kind": "interim", "rcept": rc, "src": "dart",
            "surprise": vs["op"] if vs["op"] is not None else vs["rev"], "m": m}


def price(bars, mc, d0):
    """발표 전 주가 흐름(최근 기준) + 발표 뒤 반응(발표일·다음 날 등락)."""
    if not bars:
        return None
    mrows = [b for b in bars if b["d"] in mc]
    out = {"close": bars[-1]["c"], "d": bars[-1]["d"]}
    for k, n in (("1w", 5), ("1m", 21)):
        if len(mrows) > n:
            a, b = mrows[-1], mrows[-1 - n]
            out["r" + k] = pct(a["c"], b["c"])
            out["x" + k] = round(out["r" + k] - pct(mc[a["d"]], mc[b["d"]]), 1)
    if len(bars) >= 20:
        out["ma20"] = bars[-1]["c"] > sum(b["c"] for b in bars[-20:]) / 20
    out["edge"] = build.edge_now(build.edge_states(bars), bars)
    cut = bars[-5]["d"] if len(bars) >= 5 else bars[0]["d"]
    sig = [x for x in build.strength_signals_mc(bars, mc) if x["d"] >= cut]
    sell = [x for x in build.sell_signals(bars) if x["d"] >= cut]
    out["sig"] = sig[-1] if sig else None
    out["sell"] = sell[-1] if sell else None
    i = next((k for k, b in enumerate(bars) if b["d"] == d0), None)
    if i:
        out["react"] = {"d0": pct(bars[i]["c"], bars[i - 1]["c"]),
                        "d1": pct(bars[i + 1]["c"], bars[i]["c"]) if i + 1 < len(bars) else None}
    return out


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(KST).date()
    start = (today - timedelta(days=today.weekday() + PAST_DAYS)).isoformat()
    s = session()
    try:
        if s.get(BASE + "/api/v1/auth/me", timeout=20).status_code != 200:
            raise PermissionError("스탁이지 로그인 세션 만료")
        cal, days = calendar(s, today)
        cal = [it for it in cal if it["record_date"] >= start and it.get("symbol")]
        res = {}
        past = {it["record_date"] for it in cal} | days
        for d in sorted(d for d in past if start <= d <= today.isoformat()):
            for r in results(s, d, today):
                res[r["symbol"]] = {**r, "_d": d}
    except PermissionError as e:
        print(f"SESSION_EXPIRED {e} (기존 earnings.json 유지)")
        sys.exit(3)

    seen = {}
    for it in sorted(cal, key=lambda x: (x["record_date"], x["source"] != "dart_actual_earnings")):
        seen.setdefault(it["symbol"], it)          # 같은 종목이 예고·실적 공시로 두 번 오면 앞(실적 공시) 하나만
    for code, r in res.items():                    # 발표된 날 종목은 잠정 실적 목록에서
        if code not in seen:
            pe = re.match(r"(\d{4})\.(\d)Q", r.get("period") or "")
            seen[code] = {"symbol": code, "company": r["name"], "market": r.get("market"), "record_date": r["_d"],
                          "source": "earnings_results", "detail": "",
                          **({"_pe": (int(pe.group(1)), int(pe.group(2)) * 3)} if pe else {})}
    items = list(seen.values())
    for it in items:
        it.setdefault("_pe", period_end(it))
    mdata = json.loads((DATA / "market.json").read_text(encoding="utf-8"))
    mclose = {m: {x["d"]: x["c"] for x in mdata.get(m, [])} for m in build.MARKETS}
    inlog = {x["code"] for x in json.loads((DATA / "index.json").read_text(encoding="utf-8"))["stocks"]}
    codes = [it["symbol"] for it in items]

    def fin(code):
        try:
            return naver_fin(code, today)
        except Exception as e:
            return e
    with ThreadPoolExecutor(8) as ex:
        bars = dict(zip(codes, ex.map(build.fetch_bars, codes)))
    with ThreadPoolExecutor(4) as ex:
        fins = dict(zip(codes, ex.map(fin, codes)))
    hpath = CACHE / "est_hist.json"
    hist = json.loads(hpath.read_text(encoding="utf-8")) if hpath.exists() else {}

    out, fail = [], []
    key, ndart, dfail = None, 0, []
    for it in items:
        code = it["symbol"]
        mk = MKT.get(it.get("market"), "KOSPI")
        y, m = it["_pe"]
        row = {"code": code, "name": it["company"], "market": mk, "date": it["record_date"],
               "src": SRC.get(it["source"], it["source"]), "period": f"{y}.{(m - 1) // 3 + 1}Q" if m % 3 == 0 else f"{y}.{m:02d}",
               "inlog": code in inlog}
        if isinstance(fins[code], Exception):
            fail.append(f"{it['company']}: {type(fins[code]).__name__}")
        else:
            row.update(checks(code, it["_pe"], fins[code], hist, today))
        row["price"] = price(bars.get(code), mclose.get(mk) or {}, it["record_date"])
        r = res.get(code)
        if r and r.get("period") == row["period"]:
            row["result"] = {"headline": r.get("headline"), "kind": r.get("report_kind"), "rcept": r.get("rcept_no"),
                             "surprise": (r.get("surprise") or {}).get("pct"),
                             "m": {k: {f: v.get(f) for f in ("value", "estimate", "vs_estimate_pct", "qoq", "yoy")}
                                   for k, v in (r.get("metrics") or {}).items()}}
        elif it["source"] == "dart_actual_earnings" and it["record_date"] <= today.isoformat():
            rc = re.search(r"\((\d{14})\)", it.get("detail") or "")      # 시총 5,000억 미만 → 공시 원문
            try:
                key = key or dart_key()
                row["result"] = dart_result(dart_doc(rc.group(1), key), rc.group(1), row.get("est"))
                ndart += 1
            except Exception as e:
                dfail.append(f"{it['company']}: {'접수번호 없음' if not rc else e}")
            time.sleep(0.2)
        out.append(row)
    hpath.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    out.sort(key=lambda x: (x["date"], x["name"]))
    OUT.write_text(json.dumps({"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "start": start, "items": out},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"실적 캘린더 {len(out)}종목 ({start}~) · 잠정 실적 {sum('result' in x for x in out)}(DART 원문 {ndart})"
          f" · 추정치 {sum('est' in x for x in out)}"
          + (f" · 실패 {len(fail)}: {fail[:5]}" if fail else "")
          + (f" · DART 파싱 실패 {len(dfail)}: {dfail[:5]}" if dfail else ""))
    if fail:
        sys.exit(2)


if __name__ == "__main__":
    main()
