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
CONF = ROOT / "raw" / "screen" / "confirmed.json"   # screen.py 가 남기는 분기보고서 확정 값 {code|2026.3Q: {d, rc, fs, rev, op, net}}
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


def naver_fin(code, today, since=None):
    """네이버 분기 재무: {YYYYMM: {rev, op, net, opm, cons}} (억원·%). 하루 1번만 받는다.
    since = 지난 발표일. 그날 이후 받아 둔 게 있으면 다시 받지 않는다(발표 뒤 추정치는 안 쓰고 지난 분기는 그대로)."""
    p = CACHE / f"nv_{code}.json"
    if p.exists():
        j = json.loads(p.read_text(encoding="utf-8"))
        if j.get("fetched") == today.isoformat() or (since and j.get("fetched", "") >= since):
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


def checks(code, pe, q, hist, today, rd):
    """발표 전 체크: 이번 분기 추정치(전년·전분기 실적 대비, 기록 시작 뒤 변화) + 지난 5분기.
    q = naver_fin 결과, hist = 발표 전 추정치 기록 {code|YYYYMM: {first, last}} — 발표일(rd)까지만 갱신한다.
    네이버는 분기보고서가 나올 때까지 그 분기를 추정으로 두므로 cons 만으로는 발표 여부를 모른다."""
    y, m = pe
    key = f"{y}{m:02d}"
    pm = (y, m - 3) if m > 3 else (y - 1, m + 9)
    ya, qa, cur = q.get(f"{y - 1}{m:02d}"), q.get(f"{pm[0]}{pm[1]:02d}"), q.get(key)
    hk = f"{code}|{key}"
    if cur and cur["cons"] and cur.get("op") is not None and today.isoformat() <= rd:   # 아직 발표 전 → 추정치 기록
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


NO_DART = "--no-dart" in sys.argv     # 가벼운 실행(08:30·11:30·14:00): DART 는 받아 둔 캐시만, 새로 부르지 않는다


def dart_doc(rc, key):
    """접수번호 → 파싱 결과. 공시는 안 바뀌므로 성공한 것만 영구 캐시. NO_DART 면 캐시에 없을 때 None."""
    p = CACHE / f"dart_{rc}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    if NO_DART:
        return None
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
        head = " · ".join(f"{n} {eok_str(d[k]['cur'])}" for k, n in (("rev", "매출"), ("op", "영업이익")) if d[k]["cur"] is not None) \
            or (f"순이익 {eok_str(d['net']['cur'])}" if d["net"]["cur"] is not None else "분기보고서 제출")   # 금융사는 순이익만 오기도 함
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


QFIN = DATA / "qfin"     # qfin.py 가 쓰는 종목별 「상세 실적」 (11개 분기)


def qend(y, q):
    return date(y, q * 3, 30 if q in (2, 3) else 31)


def due_date(y, q):
    """정기보고서 법정 제출 마감일: 분기·반기 = 분기말 + 45일, 사업보고서 = 결산일 + 90일. 주말이면 다음 월요일.
    (공휴일은 안 따진다 — 3.31·5.15·8.14·11.14 무렵은 공휴일과 겹치는 해가 드물다)"""
    d = qend(y, q) + timedelta(days=90 if q == 4 else 45)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prev_q(y, q):
    return (y, q - 1) if q > 1 else (y - 1, 4)


def rep_result(x):
    """분기보고서 확정 값 → 잠정 실적과 같은 모양의 result (kind=regular). 전년·전분기는 네이버 지난 분기(past) 기준,
    예상 대비는 제출 전 기록한 네이버 추정치. 사업보고서(4Q)는 연간 값이라 네이버 1~3Q 를 빼서 4Q 로 만든다."""
    c = x.get("conf")
    if not c or c.get("ccy") != "KRW":
        return None
    y, q = int(x["period"][:4]), int(x["period"][5])
    past = {p["p"]: p for p in x.get("past") or []}
    py, pq = prev_q(y, q)
    ya, qa = past.get(f"{y - 1}.{q}Q") or {}, past.get(f"{py}.{pq}Q") or {}
    cur = {k: c.get(k) for k in ("rev", "op", "net")}
    if q == 4:
        q123 = [past.get(f"{y}.{i}Q") for i in (1, 2, 3)]
        if not all(q123):
            return None
        cur = {k: None if v is None or any(p.get(k) is None for p in q123) else round(v - sum(p[k] for p in q123), 1)
               for k, v in cur.items()}
    d = {k: {"cur": cur[k], "prev": qa.get(k), "yago": ya.get(k)} for k in cur}
    return {**dart_result(d, c["rc"], x.get("est")), "kind": "regular"}


def add_conf(items, start, today=None):
    """잠정 발표는 그대로 두고, 그 분기 분기보고서(DART)가 나왔으면 확정 값을 conf 로 덧붙인다 (사용자 결정 11-11).
    잠정 없이 분기보고서만 내는 종목도 rep=True 로 넣는다 (사용자 요청 11-16 — 전 상장사 실적 리뷰):
      · 냈으면 접수일에, 아직이면 법정 마감일에 (due). 직전 분기 보고서를 그 분기 안에 낸 회사만 '낼 회사'로 본다
        (3월 결산 등은 직전 분기 보고서가 늦게 잡혀서 빠진다).
      · 마감일이 지났는데 없으면 late (미제출 — 화면에 빨간색, 사용자 결정 11-16). 마감일 다음 날 이후에
        screen.py 가 한 번 더 돌아 confirmed.json 이 그날 뒤에 쓰였을 때만 판정한다(마감일 밤 접수분 오판 방지).
    네이버 추정치·지난 분기·주가(est·past·price)는 enrich_rep 이 채우고, 여기선 이전 값을 이어 받는다."""
    today = today or datetime.now(KST).date()
    conf = json.loads(CONF.read_text(encoding="utf-8")) if CONF.exists() else {}
    cdate = datetime.fromtimestamp(CONF.stat().st_mtime, KST).date() if CONF.exists() else today
    old = {(x["code"], x["period"]): x for x in items if x.get("rep")}
    items[:] = [x for x in items if not x.get("rep")]
    have = {(x["code"], x["period"]) for x in items}
    uni = json.loads(build.UNIVERSE.read_text(encoding="utf-8")) if build.UNIVERSE.exists() else {}
    inlog = {x["code"] for x in json.loads((DATA / "index.json").read_text(encoding="utf-8"))["stocks"]}
    ok = lambda code: code in uni and code[-1] == "0" and "스팩" not in uni[code]["name"]  # noqa: E731

    def add(code, per, d, **kw):
        row = {"code": code, "name": uni[code]["name"], "market": MKT.get(uni[code].get("market"), uni[code].get("market") or "KOSPI"),
               "date": d, "src": "분기보고서", "period": per, "inlog": code in inlog, "rep": True, **kw}
        o = old.get((code, per)) or {}
        row.update({k: o[k] for k in ("est", "past", "price") if k in o})
        if o.get("date") == d and "_pf" in o:          # 마감일 → 접수일로 옮겨 가면 발표일 반응을 다시 계산
            row["_pf"] = o["_pf"]
        items.append(row)

    for k, c in conf.items():
        code, per = k.split("|")
        if (code, per) not in have and c["d"] >= start and ok(code):
            add(code, per, c["d"])
    late = {}                                       # (code, 분기) → 마감일 — 캘린더 종목(잠정 발표만 하고 보고서 미제출)도 표시
    y, q = today.year, (today.month - 1) // 3       # 막 끝난 분기부터 거꾸로, 마감일이 캘린더 기간에 걸친 분기만
    if q == 0:
        y, q = y - 1, 4
    for _ in range(2):
        due = due_date(y, q)
        per, (py, pq) = f"{y}.{q}Q", prev_q(y, q)
        if start <= due.isoformat() and due <= today + timedelta(days=PAST_DAYS):
            gone = today > due and cdate > due
            for k, c in conf.items():
                code, p0 = k.split("|")
                if p0 != f"{py}.{pq}Q" or c["d"] > qend(y, q).isoformat() or f"{code}|{per}" in conf or not ok(code):
                    continue
                # 리츠는 결산 주기가 달라 「반기보고서 (2026.09)」처럼 내서 분기 마감일로 판정 못 함 → 미제출 판정 빼고 표시만 (사용자 결정 11-17)
                # 해외 상장사(900·950 코드)는 기한이 다를 수 있어 미제출은 두되 (해외) 꼬리표 — 사용자가 참고만 함
                kw = {"odd": "reit"} if "리츠" in uni[code]["name"] else {"frn": True} if code[0] == "9" else {}
                if gone and "odd" not in kw:
                    late[(code, per)] = due.isoformat()
                if (code, per) not in have:
                    add(code, per, due.isoformat(), due=due.isoformat(), **kw, **({"late": True} if gone and "odd" not in kw else {}))
        y, q = prev_q(y, q)
    for x in items:
        c = conf.get(f"{x['code']}|{x['period']}")
        x.pop("conf", None)
        if c:
            x["conf"] = c
        if not x.get("rep"):
            x.pop("late", None)
            if (x["code"], x["period"]) in late and (x.get("result") or {}).get("kind") != "regular":   # 스탁이지가 보고서로 바꿔 줬으면 낸 것
                x["late"] = late[(x["code"], x["period"])]
                if x["code"][0] == "9":
                    x["frn"] = True
        elif c:
            r = rep_result(x)
            x.pop("result", None)
            if r:
                x["result"] = r
        x["qf"] = (QFIN / f"{x['code']}.json").exists()
    check_late(items, today)
    items.sort(key=lambda x: (x["date"], x["name"]))


FEED = ROOT / "raw" / "screen" / "feed.json"   # screen.py 가 받는 DART 공시 목록 (정기보고서 per=[연, 분기])
CORPS = Path(r"C:\ClaudeCode\tryingpig.github.io\.claude\skills\dart-financials\cache\corp_codes.json")
LATE_CHK = CACHE / "late_check.json"
PERIOD = re.compile(r"(분기|반기|사업)보고서\s*\((\d{4})\.(\d{2})\)")


def check_late(items, today):
    """미제출(late)로 잡힌 종목을 DART 공시 목록과 한 번 더 대조한다 (사용자 요청 11-17 — 11-17 에 43곳 중 20곳이 실제로는 냈음).
    ① screen 공시 목록(feed)에 그 분기 정기보고서가 있으면 낸 것 ② 없으면 회사별로 DART 목록을 직접 조회 (하루 한 번, 결과 캐시).
    냈으면 late 를 풀고 filed(접수번호·날짜·보고서 이름)를 붙인다 — 값(매출·이익)은 screen 이 다음에 받아 오면 채워진다."""
    lt = [x for x in items if x.get("late")]
    if not lt:
        return
    feed = json.loads(FEED.read_text(encoding="utf-8"))["items"] if FEED.exists() else []
    fa = {}
    for f in feed:
        if f.get("ty") == "A" and f.get("per"):
            fa.setdefault((f["corp"], f"{f['per'][0]}.{f['per'][1]}Q"), f)
    corps = json.loads(CORPS.read_text(encoding="utf-8")) if CORPS.exists() else {}
    chk = json.loads(LATE_CHK.read_text(encoding="utf-8")) if LATE_CHK.exists() else {}
    key, nq, nf = None, 0, 0
    for x in lt:
        corp = (corps.get(x["code"]) or {}).get("corp_code")
        if not corp:
            continue
        k = f"{x['code']}|{x['period']}"
        hit = fa.get((corp, x["period"]))
        if hit:
            hit = {"rc": hit["rc"], "d": hit["d"], "nm": hit["nm"]}
        elif (chk.get(k) or {}).get("on") == today.isoformat():
            hit = chk[k].get("hit")
        else:
            try:
                key = key or dart_key()
                y, q = int(x["period"][:4]), int(x["period"][5])
                j = requests.get("https://opendart.fss.or.kr/api/list.json", params={"crtfc_key": key, "corp_code": corp, "pblntf_ty": "A",
                                 "bgn_de": (qend(y, q) + timedelta(days=1)).strftime("%Y%m%d"), "end_de": today.strftime("%Y%m%d"),
                                 "page_count": "100"}, timeout=20).json()
            except Exception:                           # 조회 실패 → 판정 그대로 둔다
                continue
            nq += 1
            r = next((r for r in j.get("list", []) if (m := PERIOD.search(r.get("report_nm", "")))
                      and (int(m.group(2)), int(m.group(3)) // 3) == (y, q)), None)
            hit = {"rc": r["rcept_no"], "d": r["rcept_dt"], "nm": re.sub(r"\s+", " ", r["report_nm"]).strip()} if r else None
            chk[k] = {"on": today.isoformat(), "hit": hit}
        if hit:
            nf += 1
            d = f"{hit['d'][:4]}-{hit['d'][4:6]}-{hit['d'][6:8]}"
            x.pop("late", None)
            x.pop("frn", None)
            x["filed"] = {**hit, "d": d}
            if x.get("rep") and not x.get("conf"):      # 마감일에 있던 카드 → 실제 접수일로
                x["date"] = d
                x.pop("due", None)
    LATE_CHK.write_text(json.dumps(chk, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  미제출 DART 대조 {len(lt)}곳(직접 조회 {nq}) → 실제 제출 {nf}곳 해제")


NV_MAX = 500      # 분기보고서 종목 네이버 재무 새로 받기 — 한 번 실행에 최대 (시총 큰 순, 사용자 결정 11-16)
BAR_MAX = 800     # 분기보고서 종목 일봉 새로 받기 — 한 번 실행에 최대 (주가는 하루 한 번이면 충분)


def enrich_rep(items, today, hist, mclose):
    """분기보고서 종목(rep)에 네이버 추정치·지난 5분기·주가를 붙인다. 1,000곳이 넘어서 시총 큰 순으로 나눠 받고,
    못 받은 곳은 지난 실행 값을 그대로 둔다(다음 실행이 이어 받음). 네이버 재무는 종목당 하루 1번(naver_fin 캐시),
    이미 낸 종목은 접수일 이후 받은 게 있으면 다시 안 받는다."""
    caps = json.loads((DATA / "caps.json").read_text(encoding="utf-8"))["caps"] if (DATA / "caps.json").exists() else {}
    reps = sorted((x for x in items if x.get("rep")), key=lambda x: -caps.get(x["code"], 0))
    t = today.isoformat()

    def fresh(x):
        p = CACHE / f"nv_{x['code']}.json"
        if not p.exists():
            return False
        f = json.loads(p.read_text(encoding="utf-8")).get("fetched", "")
        return f == t or (x.get("conf") and f >= x["date"])
    nv = [x for x in reps if fresh(x)]
    nv += [x for x in reps if not fresh(x)][:NV_MAX]
    br = [x for x in reps if (x.get("_pf") != t)][:BAR_MAX]

    def fin(x):
        try:
            return naver_fin(x["code"], today, x["date"] if x.get("conf") else None)
        except Exception:
            return None
    with ThreadPoolExecutor(4) as ex:
        fins = list(ex.map(fin, nv))
    with ThreadPoolExecutor(8) as ex:
        bars = list(ex.map(lambda x: build.fetch_bars(x["code"]), br))
    for x, q in zip(nv, fins):
        if q:
            y, n = int(x["period"][:4]), int(x["period"][5])
            x.update({k: v for k, v in checks(x["code"], (y, n * 3), q, hist, today, x["date"]).items() if k in ("est", "past")})
    for x, b in zip(br, bars):
        if b:
            x["price"] = price(b, mclose.get(x["market"]) or {}, x["date"] if x.get("conf") else "")
            x["_pf"] = t
    return len(nv), len(br)


def attach_conf():
    """screen.py·qfin.py 가 끝난 뒤 부른다 — 실적 캘린더 다음 실행을 기다리지 않고 확정 값만 갈아 끼운다."""
    if OUT.exists():
        j = json.loads(OUT.read_text(encoding="utf-8"))
        add_conf(j["items"], j["start"])
        OUT.write_text(json.dumps(j, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


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

    since = {it["symbol"]: it["record_date"] for it in items if it["record_date"] < today.isoformat()}

    def fin(code):
        try:
            return naver_fin(code, today, since.get(code))
        except Exception as e:
            return e
    with ThreadPoolExecutor(8) as ex:
        bars = dict(zip(codes, ex.map(build.fetch_bars, codes)))
    with ThreadPoolExecutor(4) as ex:
        fins = dict(zip(codes, ex.map(fin, codes)))
    hpath = CACHE / "est_hist.json"
    hist = json.loads(hpath.read_text(encoding="utf-8")) if hpath.exists() else {}

    out, fail = [], []
    key, ndart, ndefer, dfail = None, 0, 0, []
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
            row.update(checks(code, it["_pe"], fins[code], hist, today, it["record_date"]))
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
                d = dart_doc(rc.group(1), key)
                if d is None:                                          # --no-dart: 다음 정규 실행이 받는다
                    ndefer += 1
                else:
                    row["result"] = dart_result(d, rc.group(1), row.get("est"))
                    ndart += 1
            except Exception as e:
                dfail.append(f"{it['company']}: {'접수번호 없음' if not rc else e}")
            if not NO_DART:
                time.sleep(0.2)
        out.append(row)
    if OUT.exists():                               # 분기보고서 종목은 지난 실행에서 받은 추정치·주가를 이어 받는다
        out += [x for x in json.loads(OUT.read_text(encoding="utf-8"))["items"] if x.get("rep")]
    add_conf(out, start, today)
    nnv, nbar = enrich_rep(out, today, hist, mclose)
    add_conf(out, start, today)                    # 받은 추정치로 예상 대비 다시
    hpath.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    OUT.write_text(json.dumps({"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "start": start, "items": out},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"실적 캘린더 {len(out)}종목 ({start}~) · 잠정 실적 {sum('result' in x for x in out)}(DART 원문 {ndart}{f', 정규 실행 때 받을 것 {ndefer}' if ndefer else ''})"
          f" · 추정치 {sum('est' in x for x in out)}"
          f" · 분기보고서 {sum(bool(x.get('rep') and x.get('conf')) for x in out)}(마감 대기 {sum(bool(x.get('due') and not x.get('late')) for x in out)}"
          f" · 미제출 {sum(bool(x.get('late') and not x.get('frn')) for x in out)} · 해외 미제출 {sum(bool(x.get('late') and x.get('frn')) for x in out)}"
          f" · 리츠 {sum(x.get('odd') == 'reit' for x in out)} · 네이버 {nnv} · 일봉 {nbar})"
          + (f" · 실패 {len(fail)}: {fail[:5]}" if fail else "")
          + (f" · DART 파싱 실패 {len(dfail)}: {dfail[:5]}" if dfail else ""))
    if fail:
        sys.exit(2)


if __name__ == "__main__":
    main()
