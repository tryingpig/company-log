# -*- coding: utf-8 -*-
"""실적 스크리닝 (사용자 기준, 2026-11-09): 전 상장사 분기 실적 → 1차·2차·3차 필수 → data/screen.json.
daily.py 가 실적 캘린더 다음에 실행한다 (Python39-32).

  1차  최근 분기 YoY 매출 +10%↑ 그리고 영업이익 +10%↑(또는 흑자 전환), 최근 분기 영업이익 흑자. QoQ 는 같이 보여만 준다.
       자료 = DART 주요계정(fnlttMultiAcnt, 100곳씩) 정기보고서. 아직 보고서 전이면 스탁이지 잠정실적(시총 5천억↑,
       실적 캘린더가 raw/earnings/results_*.json 에 받아 둔 것)을 '잠정'으로 먼저 쓴다.
  2차  (1차 통과만) OCF > 0 (올해 누적), FCF = OCF − 유형자산 취득 (표시·칩만, 필수 아님),
       매출채권회전율·재고자산회전율: 최근 3개 사업연도, 3년 전 대비 최근 값이 10% 넘게 떨어지지 않으면 통과(재고 없는 업종은 해당 없음)
  3차  (2차 통과만) 필수 = 5년간 유상증자·CB·BW 결정 공시 없음. 표시만 = 최대주주+특수관계인 지분율, 3년 배당,
       5년 자기주식 취득 결정, 연간 적자 연도, 부채총계 추세, 4년 OCF·FCF, company-log 리포트 수, 시장 대비 주가.

       + 경영진·지분·IR 사실(판정엔 안 씀): 대표이사·재직, 3년 대표/최대주주 변경, 6개월 임원·주요주주 매매,
       5년 횡령·배임·불성실공시·관리종목, 1년 IR·잠정실적 공시 횟수.

매일 흐름 (2026-11-10 사용자 요청: 실적 낸 회사만):
  DART 공시 목록(시장 전체, 유형별 몇 페이지)로 지난 실행 뒤 접수된 것만 본다 — raw/screen/feed.json
    정기보고서(분기·반기·사업, 정정 포함) → 그 회사만 주요계정 다시 받고 2차·3차 캐시 지움
    영업(잠정)실적 → 스탁이지에 없는 종목은 공시 원문을 읽어 '잠정'으로
    증자·CB·자사주 결정 / 임원·주요주주 소유상황보고서 → 그 회사 기록만 다시 받음
  분기 주요계정은 raw/screen/acct_{연}_{분기}.json 에 쌓고, 진행 중 분기는 7일마다 전 종목을 한 번 더 받아 빠진 걸 메운다.

DART 키 = telegram-report-bot/.env DART_API_KEY, 회사코드 = dart-financials 스킬 캐시.
캐시 raw/screen/ — 확정된 보고서는 영구.
"""
import hashlib
import io
import json
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import earnings  # noqa: E402

ROOT, DATA, KST = build.ROOT, build.DATA, build.KST
ENV = Path(r"C:\ClaudeCode\telegram-report-bot\.env")
CORPS = Path(r"C:\ClaudeCode\tryingpig.github.io\.claude\skills\dart-financials\cache\corp_codes.json")
CACHE = ROOT / "raw" / "screen"
EARN_CACHE = ROOT / "raw" / "earnings"
OUT = DATA / "screen.json"
DART = "https://opendart.fss.or.kr/api"
REPRT = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}      # 분기 → 보고서 코드 (4 = 사업보고서)
MIN_REV, MIN_OP = 10.0, 10.0      # 1차 기준 (사용자: 매출·영업이익 각각 최소 10%)
TURN_DROP = 0.9                   # 2차 회전율: 최근 값 ≥ 3년 전 × 0.9 면 '유사 이상'
ISSUE_YEARS = 5                   # 3차 필수: 유상증자·CB·BW 이력 확인 기간
KEY = None


def key():
    global KEY
    if KEY is None:
        KEY = next(ln.split("=", 1)[1].strip().strip('"\'') for ln in ENV.read_text(encoding="utf-8").splitlines()
                   if ln.startswith("DART_API_KEY"))
    return KEY


LOCK, LAST, BLOCKED = threading.Lock(), [0.0], [None]
GAP = 0.5           # DART 는 몰아서 부르면 IP 를 끊는다(2026-11-09 4개 동시·2분 1,800건 → ConnectionReset 차단) → 한 줄로, 0.5초 간격


class DartBlocked(RuntimeError):
    pass


def dart(path, **params):
    """DART 호출. 013(데이터 없음)은 빈 목록, 020(호출 한도)은 예외.
    재시도까지 다 끊기면 그 뒤 호출은 바로 DartBlocked — 받은 것까지로 결과를 만든다."""
    if BLOCKED[0]:
        raise DartBlocked(BLOCKED[0])
    for attempt in range(6):
        try:
            with LOCK:
                time.sleep(max(0.0, LAST[0] + GAP - time.time()))
                try:
                    j = requests.get(f"{DART}/{path}.json", params={"crtfc_key": key(), **params}, timeout=40).json()
                finally:
                    LAST[0] = time.time()
            st = j.get("status")
            if st == "000":
                return j
            if st == "013":
                return {"list": []}
            if st == "020":
                raise RuntimeError("DART 호출 한도 초과")
            raise RuntimeError(f"DART {path} {st} {j.get('message')}")
        except RuntimeError:
            raise
        except Exception as e:
            if attempt == 5:
                BLOCKED[0] = f"DART 접속 끊김({type(e).__name__}) — 과다 호출로 일시 차단됐을 수 있음"
                raise DartBlocked(BLOCKED[0])
            time.sleep(5 * (attempt + 1))


def cached(name, fn, fresh_days=None):
    """raw/screen/{name}.json 캐시. fresh_days=None 이면 영구, 숫자면 그 일수 안에 받은 것만."""
    p = CACHE / f"{name}.json"
    if p.exists():
        j = json.loads(p.read_text(encoding="utf-8"))
        if fresh_days is None or (date.today() - date.fromisoformat(j["_at"])).days < fresh_days:
            return j["v"]
    v = fn()
    p.write_text(json.dumps({"_at": date.today().isoformat(), "v": v}, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.05)
    return v


def num(s):
    s = (s or "").replace(",", "").strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None


def quarters(today):
    """최근 두 분기 [(연, 분기, 진행 중?)] — 진행 중 = 제출 기한(분기말+45일, 4분기 +90일) + 10일이 안 지남."""
    out, y, q = [], today.year, (today.month - 1) // 3          # 막 끝난 분기
    if q == 0:
        y, q = y - 1, 4
    for _ in range(2):
        end = date(y, q * 3, 30 if q in (2, 3) else 31) if q != 4 else date(y, 12, 31)
        due = end + timedelta(days=(90 if q == 4 else 45) + 10)
        out.append((y, q, today <= due))
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    return out


# ── 1차: 전 종목 주요계정 ──────────────────────────────────────────────────────

def multi(corps, y, q, live, fresh=False):
    """{corp: {fs, rev, op, rev_prev, op_prev, rev_cum, op_cum}} — 분기 3개월 값(4분기는 연간), 전년 동기 값.
    fresh=True 면 캐시 없이 바로 받는다(공시 목록에 새 보고서가 잡힌 회사만 넘어올 때)."""
    out = {}
    for i in range(0, len(corps), 100):
        part = corps[i:i + 100]
        get = lambda: dart("fnlttMultiAcnt", corp_code=",".join(part), bsns_year=str(y),  # noqa: E731
                           reprt_code=REPRT[q]).get("list", [])
        rows = get() if fresh else cached(f"multi_{y}_{q}_{hashlib.md5(','.join(part).encode()).hexdigest()[:10]}",
                                          get, fresh_days=1 if live else None)
        for r in rows:
            nm = {"매출액": "rev", "영업이익": "op"}.get(r.get("account_nm"))
            if not nm:
                continue
            d = out.setdefault(r["corp_code"], {})
            fs = r.get("fs_div")
            if d.get("fs") == "CFS" and fs != "CFS":         # 연결 우선
                continue
            if d.get("fs") != fs:
                d.clear()
                d["fs"] = fs
            d["rcept"] = r.get("rcept_no")                   # 앞 8자리 = 공시(접수)일
            d[nm] = num(r.get("thstrm_amount"))
            d[nm + "_prev"] = num(r.get("frmtrm_amount"))
            d[nm + "_cum"] = num(r.get("thstrm_add_amount"))
            d[nm + "_prev_cum"] = num(r.get("frmtrm_add_amount"))
    return out


REBUILD_DAYS = 7     # 진행 중 분기는 공시 목록이 놓친 게 있어도 이 주기로 전 종목을 한 번 더 받는다(26번 호출)


def accounts(corps, y, q, live, fd):
    """분기 주요계정 저장본 raw/screen/acct_{y}_{q}.json. 처음 한 번(진행 중 분기는 7일마다)만 전 종목,
    그 사이엔 공시 목록(feed)에 이 분기 보고서(정정 포함)가 새로 잡힌 회사만 다시 받는다 (사용자 요청 11-10)."""
    p = CACHE / f"acct_{y}_{q}.json"
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    today = date.today()
    cs = set(corps)
    if st is None or (live and (today - date.fromisoformat(st["full"])).days >= REBUILD_DAYS):
        if st:
            done = st["done"]
        else:                   # 진행 중 분기의 전 종목 값은 오늘 받은 것 — 어제까지 접수된 보고서는 이미 들어 있음
            done = [f["rc"] for f in fd if live and f["ty"] == "A" and f.get("per") == [y, q] and f["d"] < today.strftime("%Y%m%d")]
        st = {"full": today.isoformat(), "done": done, "v": multi(corps, y, q, live)}
    new = [f for f in fd if f["ty"] == "A" and f.get("per") == [y, q] and f["corp"] in cs and f["rc"] not in st["done"]]
    need = sorted({f["corp"] for f in new})
    if need:
        st["v"].update(multi(need, y, q, live, fresh=True))
        for c in need:                         # 새 보고서·정정 → 그 보고서로 받은 2차·3차 자료도 다시 받게
            for nm in ("full", "holders", "exctv2"):
                (CACHE / f"{nm}_{c}_{y}_{REPRT[q]}.json").unlink(missing_ok=True)
        st["done"] += [f["rc"] for f in new]
    p.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return st["v"], len(need)


def quarter_values(corps, y, q, live, fd):
    """분기 3개월 값 {corp: {fs, rev, op, rev_y, op_y}}. 4분기 = 사업보고서 연간 − 3분기 누적."""
    cur, _ = accounts(corps, y, q, live, fd)
    if q != 4:
        return {c: {"fs": d["fs"], "rev": d.get("rev"), "op": d.get("op"), "rev_y": d.get("rev_prev"), "op_y": d.get("op_prev"),
                    "rcept": d.get("rcept")} for c, d in cur.items()}
    q3, _ = accounts(corps, y, 3, False, fd)
    out = {}
    for c, d in cur.items():
        b = q3.get(c) or {}
        if b.get("fs") != d.get("fs"):
            continue
        sub = lambda a, k: a - b[k] if a is not None and b.get(k) is not None else None  # noqa: E731
        out[c] = {"fs": d["fs"], "rev": sub(d.get("rev"), "rev_cum"), "op": sub(d.get("op"), "op_cum"),
                  "rev_y": sub(d.get("rev_prev"), "rev_prev_cum"), "op_y": sub(d.get("op_prev"), "op_prev_cum"),
                  "rcept": d.get("rcept")}
    return out


def growth(a, b):
    if a is None or b is None:
        return None
    if a > 0 and b > 0:
        return round((a / b - 1) * 100, 1)
    if b <= 0 < a:
        return "흑자 전환"
    if a <= 0 < b:
        return "적자 전환"
    return "적자 지속"


EOK = re.compile(r"(-)?(?:(\d+)조)?\s*(?:([\d,]+)억)?")


def eok_str(s):
    """스탁이지 '1조9070억' → 원."""
    m = EOK.fullmatch((s or "").strip())
    if not m or not (m.group(2) or m.group(3)):
        return None
    v = int(m.group(2) or 0) * 10000 + int((m.group(3) or "0").replace(",", ""))
    return (-v if m.group(1) else v) * 100_000_000


def prelim(y, q):
    """스탁이지 잠정실적 캐시에서 이 분기 것만 {stock_code: {rev, op, rev_yoy, op_yoy, rev_qoq, op_qoq, d}}."""
    out = {}
    for p in sorted(EARN_CACHE.glob("results_*.json")):
        d = p.stem.split("_", 1)[1]
        for r in json.loads(p.read_text(encoding="utf-8")):
            if r.get("period") != f"{y}.{q}Q":
                continue
            m = r.get("metrics") or {}
            g = lambda k, f: (m.get(k) or {}).get(f)  # noqa: E731
            pv = lambda s: (float(s.replace("%", "")) if re.fullmatch(r"-?[\d.]+%", s or "") else (s or None))  # noqa: E731
            out[r["symbol"]] = {"rev": eok_str(g("revenue", "value")), "op": eok_str(g("operating_profit", "value")),
                                "rev_yoy": pv(g("revenue", "yoy")), "op_yoy": pv(g("operating_profit", "yoy")),
                                "rev_qoq": pv(g("revenue", "qoq")), "op_qoq": pv(g("operating_profit", "qoq")),
                                "d": d, "rcept": r.get("rcept_no")}
    return out


def naver_quarter(code, y, q):
    """실적 캘린더가 받아 둔 네이버 분기 실적(raw/earnings/nv_*.json, 억원)에서 이 분기 확정치와 YoY·QoQ.
    원천(DART 보고서)이 없을 때만 쓰는 보강 자료 — 추가 호출 없음."""
    p = EARN_CACHE / f"nv_{code}.json"
    if not p.exists():
        return None
    qs = json.loads(p.read_text(encoding="utf-8")).get("q") or {}
    k = lambda yy, qq: f"{yy}{qq * 3:02d}"  # noqa: E731
    cur = qs.get(k(y, q))
    if not cur or cur.get("cons") or cur.get("rev") is None:
        return None
    ya = qs.get(k(y - 1, q)) or {}
    pq = qs.get(k(y, q - 1) if q > 1 else k(y - 1, 4)) or {}
    w = lambda v: v * 100_000_000 if v is not None else None  # noqa: E731
    return {"rev": w(cur["rev"]), "op": w(cur.get("op")),
            "rev_yoy": growth(cur["rev"], ya.get("rev")), "op_yoy": growth(cur.get("op"), ya.get("op")),
            "rev_qoq": growth(cur["rev"], pq.get("rev")), "op_qoq": growth(cur.get("op"), pq.get("op"))}


def first_pass(x):
    """1차: 매출 YoY ≥ 10%, 영업이익 YoY ≥ 10% 또는 흑자 전환, 최근 분기 영업이익 흑자."""
    ry, oy = x.get("rev_yoy"), x.get("op_yoy")
    return (isinstance(ry, float) and ry >= MIN_REV and (x.get("op") or 0) > 0
            and ((isinstance(oy, float) and oy >= MIN_OP) or oy == "흑자 전환"))


# ── 2차·3차: 1차 통과 종목만 ───────────────────────────────────────────────────

ID = {"rev": ("ifrs-full_Revenue",), "cogs": ("ifrs-full_CostOfSales",), "op": ("dart_OperatingIncomeLoss",),
      "ocf": ("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
      "capex": ("ifrs-full_PurchaseOfPropertyPlantAndEquipment",),
      "ar": ("ifrs-full_TradeAndOtherCurrentReceivables", "dart_ShortTermTradeReceivable", "ifrs-full_CurrentTradeReceivables"),
      "inv": ("ifrs-full_Inventories",), "liab": ("ifrs-full_Liabilities",)}
NAME = {"rev": ("매출액", "수익(매출액)", "영업수익"), "cogs": ("매출원가",), "op": ("영업이익", "영업이익(손실)"),
        "ocf": ("영업활동현금흐름", "영업활동으로인한현금흐름"), "capex": ("유형자산의취득",),
        "ar": ("매출채권", "매출채권및기타채권", "매출채권및기타유동채권"), "inv": ("재고자산",), "liab": ("부채총계",)}
SJ = {"rev": ("IS", "CIS"), "cogs": ("IS", "CIS"), "op": ("IS", "CIS"), "ocf": ("CF",), "capex": ("CF",),
      "ar": ("BS",), "inv": ("BS",), "liab": ("BS",)}


def full(corp, y, reprt, fs):
    """전체 재무제표 → {항목: [당기, 전기, 전전기]} (없으면 None). 확정 보고서라 영구 캐시."""
    def get():
        rows = dart("fnlttSinglAcntAll", corp_code=corp, bsns_year=str(y), reprt_code=reprt, fs_div=fs).get("list", [])
        if not rows and fs == "CFS":
            rows = dart("fnlttSinglAcntAll", corp_code=corp, bsns_year=str(y), reprt_code=reprt, fs_div="OFS").get("list", [])
        out = {}
        for k in ID:
            hit = [r for r in rows if r.get("sj_div") in SJ[k] and r.get("account_id") in ID[k]]
            if not hit:
                hit = [r for r in rows if r.get("sj_div") in SJ[k] and re.sub(r"\s|\(.*?\)$", "", r.get("account_nm", "")) in NAME[k]]
            if hit:
                r = hit[0]
                cum = r.get("thstrm_add_amount") if r.get("sj_div") in ("IS", "CIS") and r.get("thstrm_add_amount") else None
                out[k] = [num(cum or r.get("thstrm_amount")), num(r.get("frmtrm_amount")), num(r.get("bfefrmtrm_amount"))]
        return out
    return cached(f"full_{corp}_{y}_{reprt}", get)


def turnovers(a25, a23, last_y):
    """연도별 매출채권·재고자산 회전율(평균 잔액). a25 = last_y 사업보고서, a23 = last_y-2 사업보고서."""
    def series(k):          # {연도: 값}
        s = {}
        for rep, y0 in ((a23, last_y - 2), (a25, last_y)):
            for i, v in enumerate((rep or {}).get(k) or []):
                if v is not None:
                    s.setdefault(y0 - i, v)
        return s
    rev, ar, inv = series("rev"), series("ar"), series("inv")
    out = {}
    for name, bal in (("ar", ar), ("inv", inv)):
        t = {}
        for yy in (last_y - 2, last_y - 1, last_y):
            if rev.get(yy) and bal.get(yy) is not None and bal.get(yy - 1) is not None and (bal[yy] + bal[yy - 1]) > 0:
                t[str(yy)] = round(rev[yy] / ((bal[yy] + bal[yy - 1]) / 2), 2)
        out[name] = t
    return out


def turn_ok(t, last_y):
    a, b = t.get(str(last_y - 2)), t.get(str(last_y))
    if not t:
        return None                                   # 해당 없음(재고 없는 업종 등)
    if a is None or b is None:
        return None
    return b >= a * TURN_DROP


NV_H = {"User-Agent": "Mozilla/5.0 (iPhone) Mobile", "Referer": "https://m.stock.naver.com/"}
# 네이버 공시 목록(거래소 공시 제목) — DART 를 덜 부르려고 3차는 여기서 (2026-11-09 DART 차단 뒤 사용자 결정)
ISSUE_PAT = (("유상증자", "유상증자결정"), ("CB", "전환사채권발행결정"), ("BW", "신주인수권부사채권발행결정"),
             ("EB", "교환사채권발행결정"), ("자사주", "자기주식취득"), ("소각", "주식소각결정"))


def naver_disclosures(code, today):
    """네이버 공시 제목 5년치. 처음엔 끝까지, 그 뒤엔 하루 1번 앞 페이지부터 이미 받은 공시를 만날 때까지만."""
    p = CACHE / f"nvdisc_{code}.json"
    old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"at": None, "items": []}
    if old["at"] == today.isoformat():
        return old["items"]
    seen = {x["id"] for x in old["items"]}
    cut = (today - timedelta(days=365 * ISSUE_YEARS)).isoformat()
    new, page = [], 1
    while page <= 15:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/disclosure",
                         params={"page": page, "pageSize": 100}, headers=NV_H, timeout=20)
        r.raise_for_status()
        rows = r.json() or []
        time.sleep(0.2)
        stop = not rows
        for d in rows:
            if d["disclosureId"] in seen or d["datetime"][:10] < cut:
                stop = True
                break
            new.append({"id": d["disclosureId"], "d": d["datetime"][:10], "t": d["title"]})
        if stop:
            break
        page += 1
    items = [x for x in new + old["items"] if x["d"] >= cut]
    p.write_text(json.dumps({"at": today.isoformat(), "items": items}, ensure_ascii=False), encoding="utf-8")
    return items


def dart_issues(corp, today, fd):
    """원천: DART 5년 주요사항보고서(유상증자·CB·BW·EB·자기주식 취득). 상장 전 공시도 있다(에이피알 2023 유증 —
    네이버 목록엔 상장 뒤 것만). 공시 목록에 새 결정이 잡히면(늦어도 30일마다) 다시 받고, DART 가 끊겨 있으면 받아 둔 기록을 쓴다."""
    def get():
        out, page = [], 1
        while True:
            j = dart("list", corp_code=corp, bgn_de=(today - timedelta(days=365 * ISSUE_YEARS)).strftime("%Y%m%d"),
                     end_de=today.strftime("%Y%m%d"), pblntf_ty="B", page_no=str(page), page_count="100")
            for r in j.get("list", []):
                nm = r.get("report_nm", "")
                if "정정" in nm[:8]:
                    continue
                for k, pat in ISSUE_PAT:
                    if pat in nm:
                        out.append({"k": k, "d": r["rcept_dt"], "nm": nm.replace("주요사항보고서", "").strip("()"), "rcept": r["rcept_no"]})
                        break
            if page >= int(j.get("total_page") or 1):
                return out
            page += 1
    p = CACHE / f"issues_{corp}.json"
    if p.exists() and any(f["corp"] == corp and f["ty"] == "B" and f["d"] >= json.loads(p.read_text(encoding="utf-8"))["_at"].replace("-", "")
                          for f in fd):
        p.unlink()                              # 공시 목록에 새 증자·CB·자사주 결정이 잡힘 → 다시 받기
    try:
        return cached(f"issues_{corp}", get, fresh_days=30)
    except DartBlocked:
        p = CACHE / f"issues_{corp}.json"
        return json.loads(p.read_text(encoding="utf-8"))["v"] if p.exists() else None


def issues(code, corp, today, fd):
    """5년 유상증자·CB·BW·EB 발행 결정, 자기주식 취득 결정, 주식소각 결정 = DART(원천) + 네이버 공시 제목(보강).
    정정·자회사·종속회사 공시는 뺀다. 같은 종류·같은 날은 하나로."""
    out = [{**i, "src": "DART"} for i in (dart_issues(corp, today, fd) or [])]
    for x in naver_disclosures(code, today):
        t = x["t"]
        if "정정" in t or "자회사" in t or "종속회사" in t:      # '(종속회사의 주요경영사항)' = 자회사 증자, 그 회사 것이 아님
            continue
        for k, pat in ISSUE_PAT:
            if pat in t:
                out.append({"k": k, "d": x["d"].replace("-", ""), "nm": t, "id": x["id"], "src": "네이버"})
                break
    seen, uniq = set(), []
    for i in sorted(out, key=lambda i: i["d"], reverse=True):
        if (i["k"], i["d"]) not in seen:
            seen.add((i["k"], i["d"]))
            uniq.append(i)
    return uniq


def holders(corp, y, reprt):
    """최대주주+특수관계인 지분율 합계(%)."""
    def get():
        rows = dart("hyslrSttus", corp_code=corp, bsns_year=str(y), reprt_code=reprt).get("list", [])
        # 합계 줄 이름·주식 종류 표기가 회사마다 다름 — 안랩은 "합 계", 의결권 없는 주식 줄은 "-" (2026-11-10)
        sp = lambda s: re.sub(r"\s", "", s or "")  # noqa: E731
        pct = lambda r: sp(r.get("trmend_posesn_stock_qota_rt")).replace(",", "")  # noqa: E731
        tot = [r for r in rows if sp(r.get("nm")) in ("계", "합계") and re.fullmatch(r"\d+(\.\d+)?", pct(r))]
        rank = {"보통주": 0, "의결권있는주식": 1, "합계": 2}
        tot.sort(key=lambda r: rank.get(sp(r.get("stock_knd")), 3))
        return float(pct(tot[0])) if tot else None
    return cached(f"holders_{corp}_{y}_{reprt}", get)


# ── 공시 목록(feed): 지난 실행 이후 시장 전체 공시를 유형별로 몇 페이지만 받아, 실적·이벤트가 난 회사만 처리 ──────────

FEED = CACHE / "feed.json"
FEED_START = "2026-10-01"      # 처음 한 번은 이 날부터(3분기 잠정·보고서 시즌). 지분(D)은 오늘부터 — 매매 내역은 회사별로 따로 받음
PERIOD = re.compile(r"(분기|반기|사업)보고서\s*\((\d{4})\.(\d{2})\)")
INS_NM = "임원ㆍ주요주주특정증권등소유상황보고서"
# 거래소 공시 제목 → 3차 '사실' 항목 (공백 뺀 제목 기준). 정정·자회사·종속회사·우선주 공시는 뺀다
EVT = (("ceo", re.compile(r"대표이사(\(대표집행임원\))?변경")), ("owner", re.compile(r"최대주주변경(?!을수반)")),
       ("ir", re.compile(r"기업설명회")), ("prelim", re.compile(r"영업\(잠정\)실적")),
       ("embz", re.compile(r"횡령|배임")), ("unfair", re.compile(r"불성실공시법인지정(?!예고)")),
       ("unfair_w", re.compile(r"불성실공시법인지정예고")), ("admin", re.compile(r"관리종목지정(?!우려|사유일부해제|해제)")))


def evt_kind(title):
    t = re.sub(r"\s", "", title)
    if any(w in t for w in ("정정", "자회사", "종속회사", "우선주")):
        return None
    return next((k for k, pat in EVT if pat.search(t)), None)


def per_of(d):
    """잠정실적 공시일 'YYYYMMDD' → 그 직전에 끝난 분기 [연, 분기]."""
    y, q = int(d[:4]), (int(d[4:6]) - 1) // 3
    return [y - 1, 4] if q == 0 else [y, q]


def feed(today, corp_set):
    """[{rc, corp, d, ty, k, nm, per}] — A 정기보고서(per=보고 분기), B 증자·CB·자사주 결정, I 잠정실적·이벤트, D 임원·주요주주 매매.
    유형마다 마지막으로 다 받은 날부터 오늘까지(그날은 겹쳐 받음). 끊기면 받은 데까지 두고 다음 실행이 이어 받는다."""
    st = json.loads(FEED.read_text(encoding="utf-8")) if FEED.exists() else {"to": {}, "items": []}
    have = {x["rc"] for x in st["items"]}
    for ty in "ABID":
        start = date.fromisoformat(st["to"].get(ty) or (today.isoformat() if ty == "D" else FEED_START))
        start = max(start, today - timedelta(days=89))          # 회사 지정 없는 목록은 최대 3개월
        page = 1
        while True:
            j = dart("list", bgn_de=start.strftime("%Y%m%d"), end_de=today.strftime("%Y%m%d"), pblntf_ty=ty,
                     page_no=str(page), page_count="100")
            for r in j.get("list", []):
                rc, corp, nm = r["rcept_no"], r.get("corp_code"), r.get("report_nm", "")
                if rc in have or corp not in corp_set:
                    continue
                x = {"rc": rc, "corp": corp, "d": r["rcept_dt"], "ty": ty, "nm": re.sub(r"\s+", " ", nm).strip()}
                if ty == "A":
                    m = PERIOD.search(nm)
                    if not m:
                        continue
                    x["per"] = [int(m.group(2)), int(m.group(3)) // 3]
                elif ty == "B":
                    k = next((k for k, pat in ISSUE_PAT if pat in nm and "정정" not in nm[:8]), None)
                    if not k:
                        continue
                    x["k"] = k
                elif ty == "I":
                    k = evt_kind(nm)
                    if not k:
                        continue
                    x["k"] = k
                    if k == "prelim":
                        x["per"] = per_of(r["rcept_dt"])
                elif INS_NM not in re.sub(r"\s", "", nm):
                    continue
                st["items"].append(x)
                have.add(rc)
            if page >= int(j.get("total_page") or 1):
                break
            page += 1
        st["to"][ty] = today.isoformat()
        FEED.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return st["items"]


def doc_prelim(rc):
    """영업(잠정)실적 공시 원문 → {rev|op|net: {cur, prev, yago}}(억원). 실적 캘린더와 같은 파서·같은 영구 캐시."""
    p = earnings.CACHE / f"dart_{rc}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    with LOCK:
        time.sleep(max(0.0, LAST[0] + GAP - time.time()))
        try:
            r = requests.get(f"{DART}/document.xml", params={"crtfc_key": key(), "rcept_no": rc}, timeout=30)
        finally:
            LAST[0] = time.time()
    if r.content[:2] != b"PK":
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    got = earnings.dart_parse(z.read(z.namelist()[0]).decode("utf-8", "ignore"))
    if got:
        p.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
    return got


def dart_prelim(y, q, fd, code_of, skip):
    """공시 목록의 이 분기 잠정실적 원문 {stock_code: prelim 모양}. 연결 기준 공시 우선, 같은 기준이면 나중 것.
    skip = 보고서가 이미 있거나 스탁이지가 숫자를 준 종목 — 원문을 다시 읽지 않는다(호출 줄이기)."""
    pick = {}
    for f in sorted((f for f in fd if f.get("k") == "prelim" and f.get("per") == [y, q]), key=lambda f: f["rc"]):
        cons = "연결" in f["nm"]
        old = pick.get(f["corp"])
        if old is None or cons or not old[1]:
            pick[f["corp"]] = (f, cons)
    out = {}
    for corp, (f, _) in pick.items():
        code = code_of.get(corp)
        if not code or code in skip:
            continue
        try:
            g = doc_prelim(f["rc"])
        except DartBlocked:
            raise
        except Exception:
            g = None
        if not g:
            continue
        w = lambda v: v * 100_000_000 if v is not None else None  # noqa: E731
        rv, op = g["rev"], g["op"]
        out[code] = {"rev": w(rv["cur"]), "op": w(op["cur"]), "rev_yoy": growth(rv["cur"], rv["yago"]),
                     "op_yoy": growth(op["cur"], op["yago"]), "rev_qoq": growth(rv["cur"], rv["prev"]),
                     "op_qoq": growth(op["cur"], op["prev"]), "d": f"{f['d'][:4]}-{f['d'][4:6]}-{f['d'][6:]}",
                     "rcept": f["rc"], "via": "DART"}
    return out


def events(code, corp, today, fd):
    """경영진·IR 사실: 네이버 공시 제목 5년치(보강) + 공시 목록(DART 원천)의 거래소 공시. 같은 종류·같은 날은 하나."""
    got = {(f["k"], f["d"]) for f in fd if f["corp"] == corp and f["ty"] == "I"}
    for x in naver_disclosures(code, today):
        k = evt_kind(x["t"])
        if k:
            got.add((k, x["d"].replace("-", "")))
    ago = lambda n: (today - timedelta(days=365 * n)).strftime("%Y%m%d")  # noqa: E731
    cnt = lambda k, n: sum(1 for kk, d in got if kk == k and d >= ago(n))  # noqa: E731
    last = lambda k: max((d for kk, d in got if kk == k), default=None)  # noqa: E731
    return {"ceo3": cnt("ceo", 3), "owner3": cnt("owner", 3), "ir1": cnt("ir", 1), "prelim1": cnt("prelim", 1),
            "embz5": cnt("embz", 5), "unfair5": cnt("unfair", 5), "unfair_w5": cnt("unfair_w", 5), "admin5": cnt("admin", 5),
            "last": {k: last(k) for k in ("embz", "unfair", "unfair_w", "admin") if last(k)}}


def tenure_since(pd, today):
    """임원 현황 재직기간 → 'YYYY.MM'. 회사마다 '2013.12~현재' · '80.01.01 ~ 現' · '16개월' · '0년 4개월' 로 다르게 적는다."""
    pd = pd or ""
    m = re.match(r"\s*(\d{4})\.(\d{1,2})", pd)
    if m:
        return f"{m.group(1)}.{int(m.group(2)):02d}"
    m = re.match(r"\s*(\d{2})\.(\d{1,2})\.\d{1,2}", pd)
    if m:
        yy = int(m.group(1))
        return f"{1900 + yy if yy >= 50 else 2000 + yy}.{int(m.group(2)):02d}"
    m = re.fullmatch(r"\s*(?:(\d+)\s*년)?\s*(?:(\d+)\s*개월)?\s*", pd)
    if m and (m.group(1) or m.group(2)):
        n = int(m.group(1) or 0) * 12 + int(m.group(2) or 0)
        k = today.year * 12 + today.month - 1 - n
        return f"{k // 12}.{k % 12 + 1:02d}"
    return None


def exctv(corp, y, reprt, today):
    """정기보고서 임원 현황 → 대표이사 [{nm, since}]. 직위('부회장')가 아니라 담당업무에 '대표이사'라고 적는 회사도 있다(삼성전자).
    since = 그 회사 재직 시작(대표 취임일이 아님)."""
    rows = cached(f"exctv2_{corp}_{y}_{reprt}", lambda: [
        {k: r.get(k) for k in ("nm", "ofcps", "chrg_job", "hffc_pd")}
        for r in dart("exctvSttus", corp_code=corp, bsns_year=str(y), reprt_code=reprt).get("list", [])])
    return [{"nm": r["nm"], "since": tenure_since(r.get("hffc_pd"), today)} for r in rows
            if "대표" in (r.get("ofcps") or "") or "대표이사" in (r.get("chrg_job") or "")]


INS_MONTHS = 6


def insiders(corp, today, fd):
    """임원·주요주주 매매(elestock) 최근 6개월: 늘린 보고·줄인 보고 수, 지분율 증감 합(%p).
    받아 둔 뒤 공시 목록에 그 회사 소유상황보고서가 새로 잡혔을 때만 다시 받는다."""
    p = CACHE / f"ins_{corp}.json"
    old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    if old is None or any(f["corp"] == corp and f["ty"] == "D" and f["d"] >= old["_at"].replace("-", "") for f in fd):
        cut = (today - timedelta(days=365 * 3)).isoformat()
        rows = [{"d": r["rcept_dt"], "nm": r.get("repror"), "who": r.get("isu_exctv_ofcps") if r.get("isu_exctv_ofcps") not in (None, "-")
                 else r.get("isu_main_shrholdr"), "n": num(r.get("sp_stock_lmp_irds_cnt")),
                 "pp": float(r["sp_stock_lmp_irds_rate"].replace(",", "")) if re.fullmatch(r"-?[\d.,]+", r.get("sp_stock_lmp_irds_rate") or "") else None}
                for r in dart("elestock", corp_code=corp).get("list", []) if (r.get("rcept_dt") or "") >= cut]
        old = {"_at": today.isoformat(), "v": rows}
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    cut = (today - timedelta(days=30 * INS_MONTHS)).isoformat()
    rec = [r for r in old["v"] if r["d"] >= cut and r["n"]]
    return {"up": sum(r["n"] > 0 for r in rec), "down": sum(r["n"] < 0 for r in rec),
            "pp": round(sum(r["pp"] or 0 for r in rec), 2), "last": max((r["d"] for r in rec), default=None)}


def dividends(code):
    """네이버 연간 실적의 주당배당금 {연도: 원} — 확정 연도만(컨센서스 열은 뺀다). 7일 캐시."""
    def get():
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/finance/annual", headers=NV_H, timeout=20)
        r.raise_for_status()
        fi = r.json().get("financeInfo") or {}
        keys = {t["key"]: t.get("isConsensus") == "Y" for t in fi.get("trTitleList") or []}
        row = next((x for x in fi.get("rowList") or [] if x.get("title") == "주당배당금"), None)
        out = {}
        for k, cell in ((row or {}).get("columns") or {}).items():
            if k in keys and not keys[k]:
                v = (cell or {}).get("value", "").replace(",", "")
                out[k[:4]] = int(float(v)) if re.fullmatch(r"-?\d+(\.\d+)?", v) else (0 if v in ("", "-") else None)
        time.sleep(0.2)
        return out
    return cached(f"nvdiv_{code}", get, fresh_days=7)


def rel(bars, mc, n):
    rows = [b for b in bars or [] if b["d"] in mc]
    if len(rows) <= n:
        return None
    a, b = rows[-1], rows[-1 - n]
    return round(((a["c"] / b["c"]) - (mc[a["d"]] / mc[b["d"]])) * 100, 1)


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(KST).date()
    uni = json.loads(build.UNIVERSE.read_text(encoding="utf-8"))
    cc = json.loads(CORPS.read_text(encoding="utf-8"))
    stocks = {c: v for c, v in uni.items() if c[-1] == "0" and "스팩" not in v["name"] and c in cc}   # 보통주만
    corp_of = {c: cc[c]["corp_code"] for c in stocks}
    code_of = {v: k for k, v in corp_of.items()}
    corps = sorted(set(corp_of.values()))
    idx = {x["code"]: x for x in json.loads((DATA / "index.json").read_text(encoding="utf-8"))["stocks"]}
    mdata = json.loads((DATA / "market.json").read_text(encoding="utf-8"))
    mclose = {m: {x["d"]: x["c"] for x in mdata.get(m, [])} for m in build.MARKETS}
    last_y = today.year - 1 if today.month > 3 else today.year - 2       # 가장 최근 사업보고서 연도

    rpath = CACHE / "seen.json"
    reg = json.loads(rpath.read_text(encoding="utf-8")) if rpath.exists() else {"since": today.isoformat(), "q": {}}
    out_q = []
    try:
        fd = feed(today, set(corps))
    except DartBlocked:
        fd = json.loads(FEED.read_text(encoding="utf-8"))["items"] if FEED.exists() else []
    nf = lambda ty: sum(f["ty"] == ty and f["d"] == today.strftime("%Y%m%d") for f in fd)  # noqa: E731
    print(f"공시 목록 {len(fd)}건 저장 · 오늘 정기보고서 {nf('A')} · 거래소 {nf('I')} · 지분 {nf('D')}")
    for y, q, live in quarters(today):
        py, pq = (y, q - 1) if q > 1 else (y - 1, 4)
        try:
            cur = quarter_values(corps, y, q, live, fd)
            prev = quarter_values(corps, py, pq, False, fd)
        except DartBlocked as e:
            print(f"DART_BLOCKED {e} — 1차 자료를 못 받아 기존 screen.json 유지")
            sys.exit(4)
        pre = prelim(y, q)                                         # 스탁이지(시총 5천억↑)
        has = {code_of[c] for c, d in cur.items() if d.get("rev") is not None and c in code_of}
        try:                                                       # 그 밖의 잠정실적은 DART 공시 원문으로
            dpre = dart_prelim(y, q, fd, code_of, has | set(pre))
        except DartBlocked:
            dpre = {}
        pre.update(dpre)
        rows = []
        for corp in corps:
            code = code_of[corp]
            d, p = cur.get(corp), pre.get(code)
            nv = None if d and d.get("rev") is not None else naver_quarter(code, y, q)
            if d and d.get("rev") is not None:
                pv = prev.get(corp) or {}
                same = pv.get("fs") == d["fs"]
                rc = d.get("rcept") or ""
                x = {"src": "보고서", "fs": "연결" if d["fs"] == "CFS" else "별도", "rev": d["rev"], "op": d["op"],
                     "d": f"{rc[:4]}-{rc[4:6]}-{rc[6:8]}" if len(rc) >= 8 else None, "rcept": rc or None,
                     "rev_yoy": growth(d["rev"], d["rev_y"]), "op_yoy": growth(d["op"], d["op_y"]),
                     "rev_qoq": growth(d["rev"], pv.get("rev")) if same else None,
                     "op_qoq": growth(d["op"], pv.get("op")) if same else None}
            elif nv:                                               # 원천(DART) 없을 때 1순위 보강: 네이버 확정치
                x = {"src": "네이버", **nv}
            elif p and p.get("rev") is not None:
                x = {"src": "잠정", "d": p["d"], "rcept": p["rcept"], "via": p.get("via", "스탁이지"),
                     **{k: p[k] for k in ("rev", "op", "rev_yoy", "op_yoy", "rev_qoq", "op_qoq")}}
            else:
                continue
            x.update(code=code, name=stocks[code]["name"], market=stocks[code]["market"], corp=corp)
            rows.append(x)
        passed = [x for x in rows if first_pass(x)]
        # 날짜별로 보이게: 1차에 처음 들어온 날(in)·잠정→보고서처럼 출처가 바뀐 날(upd) 기록 (사용자 요청 11-09)
        for x in passed:
            k = f"{y}.{q}Q|{x['code']}"
            s = reg["q"].get(k)
            if not s:
                s = reg["q"][k] = {"in": today.isoformat(), "src": x["src"]}
            elif s["src"] != x["src"]:
                s.update(src=x["src"], upd=today.isoformat(), was=s["src"])
            x["in"], x["upd"], x["was"] = s["in"], s.get("upd"), s.get("was")
            x["d"] = x.get("d") or s["in"]
        print(f"{y}.{q}Q {'진행 중' if live else '확정'}: 실적 {len(rows)}곳(보고서 {sum(x['src'] == '보고서' for x in rows)}"
              f" · 잠정 {sum(x['src'] == '잠정' for x in rows)}, 그중 DART 원문 {sum(x.get('via') == 'DART' for x in rows)}) · 1차 {len(passed)}")

        # 2차
        def second(x):
            fs = "CFS" if x.get("fs", "연결") == "연결" else "OFS"
            curr = full(x["corp"], y, REPRT[q], fs) if x["src"] == "보고서" else {}
            a25, a23 = full(x["corp"], last_y, "11011", fs), full(x["corp"], last_y - 2, "11011", fs)
            ocf = (curr.get("ocf") or [None])[0]
            capex = (curr.get("capex") or [None])[0]
            t = turnovers(a25, a23, last_y)
            cf = {}
            for rep, y0 in ((a23, last_y - 2), (a25, last_y)):
                for i in range(2):
                    o = ((rep.get("ocf") or [None, None])[i])
                    c = ((rep.get("capex") or [None, None])[i])
                    if o is not None:
                        cf[str(y0 - i)] = {"ocf": o, "fcf": o - abs(c) if c is not None else None}
            yrs = {}
            for rep, y0 in ((a23, last_y - 2), (a25, last_y)):
                for i, v in enumerate(rep.get("op") or []):
                    if v is not None:
                        yrs.setdefault(str(y0 - i), {})["op"] = v
                for i, v in enumerate(rep.get("liab") or []):
                    if v is not None:
                        yrs.setdefault(str(y0 - i), {})["liab"] = v
            s2 = {"ocf": ocf, "fcf": ocf - abs(capex) if ocf is not None and capex is not None else None,
                  "cf_period": f"{y}.{q}Q 누적" if x["src"] == "보고서" else None,
                  "turn": t, "ar_ok": turn_ok(t["ar"], last_y), "inv_ok": turn_ok(t["inv"], last_y), "cf": cf, "years": yrs}
            if s2["ocf"] is None and cf.get(str(last_y)):            # 잠정만 있는 종목: 최근 사업연도 OCF 로
                s2["ocf"], s2["fcf"], s2["cf_period"] = cf[str(last_y)]["ocf"], cf[str(last_y)]["fcf"], f"{last_y}년"
            s2["pass"] = bool(s2["ocf"] and s2["ocf"] > 0 and s2["ar_ok"] is not False and s2["inv_ok"] is not False)
            return s2

        def third(x):
            try:
                iss = issues(x["code"], x["corp"], today, fd)      # DART(원천) + 네이버 공시 제목(보강)
                ev = events(x["code"], x["corp"], today, fd)       # 경영진·IR 사실 (판정엔 안 씀, 표시·칩)
            except Exception as e:
                return {"err": f"네이버 공시 목록 실패({type(e).__name__})", "pass": None}
            bad = [i for i in iss if i["k"] in ("유상증자", "CB", "BW")]
            hq = (y, REPRT[q]) if x["src"] == "보고서" else (last_y, "11011")
            try:
                ho = holders(x["corp"], *hq)                       # 지분율만 DART — 끊겨도 3차 판정은 한다
            except DartBlocked:
                ho = None
            try:
                ceo = exctv(x["corp"], *hq, today)
            except DartBlocked:
                ceo = None
            try:
                ins = insiders(x["corp"], today, fd)
            except DartBlocked:
                ins = None
            try:
                dv = dividends(x["code"])
            except Exception:
                dv = {}
            return {"issues": iss, "pass": not bad, "holders": ho, "div": dv, "ev": ev, "ceo": ceo, "ins": ins,
                    "buyback": sum(i["k"] == "자사주" for i in iss), "burn": sum(i["k"] == "소각" for i in iss)}

        def safe(fn):           # DART 가 끊기면 그 종목은 '확인 못 함'으로 두고 계속
            def run(x):
                try:
                    return fn(x)
                except DartBlocked as e:
                    return {"err": str(e), "pass": None}
            return run
        s2s = [safe(second)(x) for x in passed]
        for x, s2 in zip(passed, s2s):
            x["s2"] = s2
        s3s = [safe(third)(x) if x["s2"].get("pass") else None for x in passed]
        for x, s3 in zip(passed, s3s):
            if s3:
                x["s3"] = s3
        with ThreadPoolExecutor(8) as ex:
            bars = dict(zip([x["code"] for x in passed], ex.map(build.fetch_bars, [x["code"] for x in passed])))
        for x in passed:
            mc = mclose.get(x["market"]) or {}
            b = bars.get(x["code"])
            x["px"] = {"r3": rel(b, mc, 63), "r6": rel(b, mc, 120), "close": b[-1]["c"] if b else None}
            e = idx.get(x["code"])
            x["reports3"] = (e["up3"] + e["down3"]) if e else 0
            x["inlog"] = bool(e)
        out_q.append({"q": f"{y}.{q}Q", "live": live, "filed": len(rows), "reported": sum(x["src"] == "보고서" for x in rows),
                      "universe": len(corps), "items": sorted(passed, key=lambda x: -(x["op_yoy"] if isinstance(x["op_yoy"], float) else 9999))})
        miss = sum("err" in x["s2"] or "err" in (x.get("s3") or {}) for x in passed)
        out_q[-1]["unchecked"] = miss
        print(f"  2차 {sum(bool(x['s2'].get('pass')) for x in passed)} · 3차 필수 {sum(bool((x.get('s3') or {}).get('pass')) for x in passed)}"
              + (f" · 확인 못 함 {miss}" if miss else ""))
    rpath.write_text(json.dumps(reg, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    OUT.write_text(json.dumps({"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "last_annual": last_y,
                               "today": today.isoformat(), "since": reg["since"],
                               "rule": {"rev": MIN_REV, "op": MIN_OP, "turn_drop": TURN_DROP, "issue_years": ISSUE_YEARS},
                               "quarters": out_q}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if BLOCKED[0]:
        print(f"DART_BLOCKED {BLOCKED[0]} — 받은 데이터까지로 저장, 다음 실행이 캐시 뒤부터 이어 받음")
        sys.exit(2)


if __name__ == "__main__":
    main()
