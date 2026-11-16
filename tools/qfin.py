# -*- coding: utf-8 -*-
"""실적 캘린더 「상세 실적」 — 종목별 최근 11개 분기 손익·재무상태 (사용자 요청 2026-11-16, 샘플 '실적 퀵 분석' 1번 구성).

  자료 = DART 전체재무제표(fnlttSinglAcntAll) 분기마다 1번. 확정 보고서라 영구 캐시 raw/qfin/{corp}_{연}_{분기}.json.
    · 보고서 하나에 작년 같은 분기 3개월 값이 빠진 회사가 있어(오킨스전자 등 누적만) 분기마다 그 해 보고서를 받는다.
    · 분기 3개월 = 보고서의 당기 3개월 값(없으면 누적 차이), 4분기 = 사업보고서 연간 − 3분기 누적.
    · 재무상태(현금·재고·매출채권·매입채무·차입금) = 그 분기말 잔액, 영업현금흐름 = 그 해 누적.
    · 연결 우선, 연결이 없는 보고서만 별도(분기마다 fs 표시).
  대상 = data/earnings.json 종목(잠정 발표 + 분기보고서만 낸 종목), 최근 발표 먼저·같은 날은 시총 큰 순.
    이번 분기 보고서는 screen.py 가 확정 값을 잡은 뒤(raw/screen/confirmed.json)에만 부른다. 정정 보고서면 다시.
  DART 차단 경험(2026-11-09) → screen.dart(0.5초 간격 한 줄) 공용, 한 번 실행 최대 RUN_MAX, 하루 DAY_MAX.
  출력 data/qfin/{code}.json (억원). 비율·브리지·회전일수는 화면이 계산.

실행: Python39-32 python tools/qfin.py [--max N] [--code 080580]
"""
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import earnings  # noqa: E402
import screen  # noqa: E402

ROOT, DATA, KST = build.ROOT, build.DATA, build.KST
RAW = ROOT / "raw" / "qfin"
OUT = DATA / "qfin"
CALLS = RAW / "calls.json"
NQ = 11                 # 보여줄 분기 수 (샘플과 같게 1Q24~3Q26)
RUN_MAX, DAY_MAX = 2000, 6000
RETRY_DAYS = 30         # 지난 분기인데 자료 없음(013) — 상장 전 등. 이 일수 뒤 한 번 더

IS_ID = {"rev": ("ifrs-full_Revenue",), "cogs": ("ifrs-full_CostOfSales",), "gp": ("ifrs-full_GrossProfit",),
         "sga": ("dart_TotalSellingGeneralAdministrativeExpenses",), "op": ("dart_OperatingIncomeLoss",),
         "ni": ("ifrs-full_ProfitLoss",)}
IS_NM = {"rev": ("매출액", "수익(매출액)", "영업수익", "매출"), "cogs": ("매출원가",), "gp": ("매출총이익",),
         "sga": ("판매비와관리비", "판매비및관리비"), "op": ("영업이익", "영업이익(손실)"),
         "ni": ("당기순이익", "당기순이익(손실)", "분기순이익", "분기순이익(손실)", "반기순이익", "반기순이익(손실)")}
BS_ID = {"cash": ("ifrs-full_CashAndCashEquivalents",), "inv": ("ifrs-full_Inventories",),
         "ar": ("dart_ShortTermTradeReceivable", "ifrs-full_CurrentTradeReceivables", "ifrs-full_TradeAndOtherCurrentReceivables"),
         "ap": ("dart_ShortTermTradePayables", "ifrs-full_CurrentTradePayables", "ifrs-full_TradeAndOtherCurrentPayables")}
BS_NM = {"cash": ("현금및현금성자산",), "inv": ("재고자산",), "ar": ("매출채권", "매출채권및기타채권", "매출채권및기타유동채권"),
         "ap": ("매입채무", "매입채무및기타채무", "매입채무및기타유동채무")}
DEBT = re.compile(r"차입금|사채")            # 차입금 = 차입금·사채 계정 합(리스·충당 제외)
DEBT_NOT = re.compile(r"리스|충당|이자|할인|할증|총계|합계")


def calls_today():
    j = json.loads(CALLS.read_text(encoding="utf-8")) if CALLS.exists() else {}
    return j.get(date.today().isoformat(), 0)


def add_calls(n):
    j = json.loads(CALLS.read_text(encoding="utf-8")) if CALLS.exists() else {}
    t = date.today().isoformat()
    CALLS.write_text(json.dumps({t: j.get(t, 0) + n}), encoding="utf-8")


def nm(r):
    return re.sub(r"\s|\(.*?\)$", "", r.get("account_nm") or "")


def pick(rows, sj, ids, names):
    for want in (lambda r: r.get("account_id") in ids, lambda r: nm(r) in names or (r.get("account_nm") or "").replace(" ", "") in names):
        for s in sj:                        # 손익계산서(IS) 먼저, 없으면 포괄손익계산서(CIS)
            hit = [r for r in rows if r.get("sj_div") == s and want(r)]
            if hit:
                return hit[0]
    return None


def extract(rows, q):
    """보고서 한 개 → {fs, rc, ccy, is3, iscum, bs, ocf, aro, apo} (원)."""
    num = screen.num
    out = {"rc": rows[0].get("rcept_no"), "ccy": rows[0].get("currency") or "KRW", "is3": {}, "iscum": {}, "bs": {}}
    for k in IS_ID:
        r = pick(rows, ("IS", "CIS"), IS_ID[k], IS_NM[k])
        if not r:
            continue
        a, add = num(r.get("thstrm_amount")), num(r.get("thstrm_add_amount"))
        if q == 4:                          # 사업보고서 = 연간
            out["iscum"][k] = a
        else:
            out["is3"][k] = a if q > 1 or add is None else add
            out["iscum"][k] = add if add is not None else (a if q == 1 else None)
    for k in BS_ID:
        r = pick(rows, ("BS",), BS_ID[k], BS_NM[k])
        if r:
            out["bs"][k] = num(r.get("thstrm_amount"))
            if k in ("ar", "ap"):
                out[k + "o"] = "기타" in (r.get("account_nm") or "")
    debt = [num(r.get("thstrm_amount")) for r in rows if r.get("sj_div") == "BS" and DEBT.search(r.get("account_nm") or "")
            and not DEBT_NOT.search(r.get("account_nm") or "")]
    debt = [v for v in debt if v is not None]
    out["bs"]["debt"] = sum(debt) if debt else None
    r = pick(rows, ("CF",), ("ifrs-full_CashFlowsFromUsedInOperatingActivities",), ("영업활동현금흐름", "영업활동으로인한현금흐름"))
    out["ocf"] = num(r.get("thstrm_amount")) if r else None
    return out


class Budget(Exception):
    pass


def report(corp, y, q, want_rc=None, state=None):
    """(연, 분기) 보고서 추출본. 캐시 → 없으면 DART. 예산 넘으면 Budget."""
    p = RAW / f"{corp}_{y}_{q}.json"
    if p.exists():
        j = json.loads(p.read_text(encoding="utf-8"))
        v = j["v"]
        if v is not None and (not want_rc or v.get("rc") == want_rc):
            return v
        if v is None and not want_rc and (date.today() - date.fromisoformat(j["_at"])).days < RETRY_DAYS:
            return None
    if state["n"] >= state["max"]:
        raise Budget()
    v = None
    for fs in ("CFS", "OFS"):
        state["n"] += 1
        rows = screen.dart("fnlttSinglAcntAll", corp_code=corp, bsns_year=str(y), reprt_code=screen.REPRT[q], fs_div=fs).get("list", [])
        if rows:
            v = {"fs": fs, **extract(rows, q)}
            break
    p.write_text(json.dumps({"_at": date.today().isoformat(), "v": v}, ensure_ascii=False), encoding="utf-8")
    return v


def back(y, q, n):
    out = []
    for _ in range(n):
        out.append((y, q))
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    return out[::-1]


def series(reps, qs):
    """분기별 행 (억원). reps = {(y,q): 추출본|None}."""
    eok = lambda v: None if v is None else round(v / 1e8, 2)  # noqa: E731
    rows = []
    for y, q in qs:
        r = reps.get((y, q))
        if not r:
            rows.append({"p": f"{y}.{q}Q"})
            continue
        row = {"p": f"{y}.{q}Q", "fs": r["fs"], "rc": r["rc"], "d": f"{r['rc'][:4]}-{r['rc'][4:6]}-{r['rc'][6:8]}" if r.get("rc") else None}
        prev = reps.get((y, q - 1)) if q > 1 else None
        for k in IS_ID:
            if q == 4:
                a, b = r["iscum"].get(k), (prev or {}).get("iscum", {}).get(k)
                v = a - b if a is not None and b is not None and prev and prev["fs"] == r["fs"] else None
            else:
                v = r["is3"].get(k)
                if v is None and q > 1 and prev and prev["fs"] == r["fs"]:
                    a, b = r["iscum"].get(k), prev["iscum"].get(k)
                    v = a - b if a is not None and b is not None else None
            row[k] = eok(v)
        if row.get("gp") is None and row.get("rev") is not None and row.get("cogs") is not None:
            row["gp"] = round(row["rev"] - row["cogs"], 2)
        for k in ("cash", "inv", "ar", "ap", "debt"):
            row[k] = eok(r["bs"].get(k))
        row["ocf"] = eok(r.get("ocf"))           # 그 해 누적
        rows.append(row)
    return rows


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    run_max = int(args[args.index("--max") + 1]) if "--max" in args else RUN_MAX
    only = args[args.index("--code") + 1] if "--code" in args else None
    cc = json.loads(screen.CORPS.read_text(encoding="utf-8"))
    ej = json.loads(earnings.OUT.read_text(encoding="utf-8"))
    conf = json.loads(earnings.CONF.read_text(encoding="utf-8")) if earnings.CONF.exists() else {}
    caps = json.loads((DATA / "caps.json").read_text(encoding="utf-8"))["caps"] if (DATA / "caps.json").exists() else {}
    items = [x for x in ej["items"] if re.fullmatch(r"\d{4}\.\dQ", x["period"]) and x["code"] in cc and (not only or x["code"] == only)]
    items.sort(key=lambda x: caps.get(x["code"], 0), reverse=True)     # 시총 큰 순 (사용자 결정 11-16)
    state = {"n": 0, "max": min(run_max, max(0, DAY_MAX - calls_today()))}
    done, wrote, stop = set(), 0, None
    try:
        for x in items:
            if x["code"] in done:
                continue
            done.add(x["code"])
            corp = cc[x["code"]]["corp_code"]
            y, q = int(x["period"][:4]), int(x["period"][5])
            c = conf.get(f"{x['code']}|{x['period']}")
            qs = back(y, q, NQ)
            reps = {}
            for (yy, qq) in qs:
                if (yy, qq) == (y, q):
                    if not c:                    # 이번 분기 보고서 전(잠정만) — 지난 분기까지
                        continue
                    reps[(yy, qq)] = report(corp, yy, qq, c.get("rc"), state)
                else:
                    reps[(yy, qq)] = report(corp, yy, qq, None, state)
            rows = series(reps, qs)
            if not any(r.get("rev") is not None or r.get("op") is not None for r in rows):
                continue
            if any((reps.get(k) or {}).get("ccy", "KRW") != "KRW" for k in qs):
                continue                         # 외화 공시 — 원화 금액 비교 생략 (캘린더 카드와 같게)
            last = next((reps[k] for k in reversed(qs) if reps.get(k)), {})
            doc = {"code": x["code"], "name": x["name"], "period": x["period"], "fs": last.get("fs"),
                   "aro": bool(last.get("aro")), "apo": bool(last.get("apo")), "q": rows}
            p = OUT / f"{x['code']}.json"
            old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
            if not old or {k: v for k, v in old.items() if k != "updated"} != doc:
                doc["updated"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
                p.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                wrote += 1
    except Budget:
        stop = "호출 한도"
    except screen.DartBlocked as e:
        stop = f"DART_BLOCKED {e}"
    add_calls(state["n"])
    earnings.attach_conf()                       # 캘린더 카드의 「상세 실적」 표시(qf) 갱신
    have = sum((OUT / f"{c}.json").exists() for c in done | {x["code"] for x in items})
    print(f"상세 실적 {have}/{len({x['code'] for x in items})}종목 · 이번에 갱신 {wrote} · DART {state['n']}번 (오늘 {calls_today()})"
          + (f" · 멈춤: {stop} — 다음 실행에 이어서" if stop else ""))
    if stop and stop.startswith("DART_BLOCKED"):
        sys.exit(4)


if __name__ == "__main__":
    main()
