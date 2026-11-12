"""기업 분석 자료 — 종목 페이지 「기업 분석」 칸 (사용자 요청 2026-11-12).

  ③ 이익 성장률: 실적 5년 = DART 사업보고서 재무제표(screen 캐시 공용, 매출총이익·이익률은 계산)
                 + 추정 3년 = 스탁이지 → data/fin/{code}.json (사용자 11-12: 실적은 원천, 스탁이지는 추정만)
     · 매일 스탁이지 전 종목 PER(/valuation/data, 요청 1번)을 받아 주가÷PER 로 추정 EPS 를 되짚고,
       추정이 있는 종목 중 처음이거나 추정 EPS 가 바뀐 종목만 종목별로 받는다(사용자: 그쪽 부담 줄이기).
       추정이 없는 종목은 스탁이지를 아예 안 부른다.
     · 로그인 세션이 끊기면 PER 비교를 못 하므로 7일 지난 것만 다시 받는다(종목별 주소는 로그인 없이 열림).
  ① 사업 내용: DART 최근 사업보고서 「II. 사업의 내용」 원문을 raw/company/biz/{code}.txt 로 받아 둔다.
     AI 정리는 daily.py(Python312)가 bizsum 으로 한다. 사업보고서가 새로 나왔을 때만 다시.

대상: 리포트 로그 종목(data/stocks) + 차트만 보는 매수 에지 종목(data/charts).

실행: Python39-32 python tools/company.py [--max-fin N] [--max-biz N]
"""
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import earnings  # noqa: E402
import screen  # noqa: E402

ROOT, DATA, KST = build.ROOT, build.DATA, build.KST
FIN = DATA / "fin"
RAW = ROOT / "raw" / "company"
BIZ_RAW = RAW / "biz"
SE_RAW = RAW / "se"           # 스탁이지 info-tab 정리본(실적·추정) — 추정치만 화면에 씀
STATE = RAW / "state.json"
SE_GAP = 8                   # 스탁이지 종목별 요청 간격(초) — 11-12 1.5초 연속 조회 후 IP 6시간 차단
FIN_STALE = 7                # 세션이 없을 때: 이 날수보다 오래된 것만 다시
BIZ_RECHECK = 30             # 사업보고서 새로 나왔나 다시 볼 간격(일) — 3~4월은 매 실행
BIZ_CHARS = 16000            # AI 에 넘길 「사업의 내용」 길이 (하위 장을 못 나눈 보고서는 앞부분)
BIZ_PARTS = [(r"개요", 5000), (r"주요\s*제품|영업의\s*(현황|종류)", 5000), (r"매출", 6000)]   # 하위 장별 최대 글자


def arg(name, default):
    return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def targets():
    codes = {p.stem for p in (DATA / "stocks").glob("*.json")} | {p.stem for p in (DATA / "charts").glob("*.json")}
    return sorted(codes)


# ── ③ 실적(DART)·추정(스탁이지) ─────────────────────────────────────────────
# 사용자 원칙(11-12): 실적은 원천 DART + 계산(매출총이익 = 매출 − 매출원가, 이익률), 스탁이지는 추정치에만.

def implied_eps(v):
    """/valuation/data 한 종목 → {연도: 주가÷PER}. PER 이 비거나 0 이면 뺀다."""
    out, px = {}, v.get("price")
    for k, x in v.items():
        m = re.fullmatch(r"(\d{4})\(E\)_PER", k)
        if m and px and isinstance(x, (int, float)) and x:
            out[m.group(1)] = round(px / x, 2)
    return out


def eps_changed(old, new):
    if not old:
        return True
    for y, e in new.items():
        o = old.get(y)
        if o is None or abs(e - o) > max(abs(o) * 0.003, 0.5):      # 주가·PER 반올림 흔들림은 무시
            return True
    return False


def row(y, e, rev, op, gp):
    """금액 억원 → 화면 한 해. 이익률은 직접 계산."""
    pct = lambda a: None if a is None or not rev else round(a / rev * 100, 1)   # noqa: E731
    return {"y": str(y), "e": e, "rev": rev, "op": op, "gp": gp, "gpm": pct(gp), "opm": pct(op)}


def parse_se(fin):
    """스탁이지 info-tab financials → 연결(없으면 별도) {basis, act: 실적 행, est: 추정 3년 행}. 억원."""
    for cs, a_key, e_key in (("C", "consolidatedYearly", "consolidatedYearlyEstimate"),
                             ("S", "separateYearly", "separateYearlyEstimate")):
        act = sorted((x for x in fin.get(a_key) or [] if x.get("revenue") is not None), key=lambda x: x["year"])
        if act:
            break
    else:
        return None
    est = sorted((x for x in fin.get(e_key) or [] if x["year"] > act[-1]["year"] and x.get("revenue") is not None),
                 key=lambda x: x["year"])[:3]
    eok = lambda v: None if v is None else round(v / 1e8, 1)   # noqa: E731
    conv = lambda x, e: row(x["year"], e, eok(x.get("revenue")), eok(x.get("operating_income")),   # noqa: E731
                            eok(x.get("gross_profit")))
    return {"basis": cs, "act": [conv(x, False) for x in act[-5:]], "est": [conv(x, True) for x in est]}


def annual_year(st, today):
    """실적 기준 사업연도. 사업보고서는 3월 말까지 나오므로 4월부터 작년, 그 전엔 재작년.
    사업 내용 수집이 이미 새 사업보고서를 봤으면(3월 제출분) 그 연도."""
    y = int(today[:4]) - (1 if today[5:7] >= "04" else 2)
    m = re.search(r"\((\d{4})\.\d{2}\)", st.get("biz_nm", ""))
    return max(y, int(m.group(1))) if m else y


def dart_act(corp, y0):
    """DART 사업보고서 전체 재무제표(스크리닝과 캐시 공용) y0·y0-2 두 권 → 최근 5년 실적 행. 없으면 None."""
    def rep(y, retry=False):
        a = screen.full(corp, y, "11011", "CFS")
        if not (a.get("rev") or [None])[0]:
            if retry:                                   # 최근 보고서가 아직 안 나왔으면 빈 캐시는 지워 다음에 다시
                (screen.CACHE / f"full_{corp}_{y}_11011.json").unlink(missing_ok=True)
            return None
        return a
    a = rep(y0, retry=True)
    if not a:
        return None
    v = {}
    for r, base in ((rep(y0 - 2), y0 - 2), (a, y0)):    # 뒤 보고서(재작성된 전기 값)가 우선
        for k in ("rev", "cogs", "op"):
            for i, x in enumerate((r or {}).get(k) or []):
                if x is not None:
                    v.setdefault(base - i, {})[k] = x
    eok = lambda x: None if x is None else round(x / 1e8, 1)   # noqa: E731
    out = []
    for y in range(y0 - 4, y0 + 1):
        d = v.get(y) or {}
        if d.get("rev"):
            gp = d["rev"] - d["cogs"] if d.get("cogs") is not None else None
            out.append(row(y, False, eok(d["rev"]), eok(d.get("op")), eok(gp)))
    return out or None


def build_fin(c, corp, st, today):
    """data/fin/{c}.json = DART 실적 + 스탁이지 추정(받아 둔 것). 둘 다 없으면 파일 없음."""
    se = json.loads((SE_RAW / f"{c}.json").read_text(encoding="utf-8")) if (SE_RAW / f"{c}.json").exists() else None
    act = dart_act(corp, annual_year(st, today)) if corp else None
    src = "DART"
    if not act and se and se["act"]:
        act, src = se["act"], "스탁이지"                 # DART 재무제표가 없는 종목(금융사 일부 등)만
    if not act:
        (FIN / f"{c}.json").unlink(missing_ok=True)
        return False
    last = int(act[-1]["y"])
    est = [x for x in (se or {}).get("est") or [] if int(x["y"]) > last]
    FIN.mkdir(parents=True, exist_ok=True)
    (FIN / f"{c}.json").write_text(json.dumps({
        "code": c, "act_src": src, "est_src": "스탁이지" if est else None,
        "fetched": (se or {}).get("fetched") if est else None, "basis": (se or {}).get("basis", "C"),
        **({"est_wait": True} if not est and st.get("has_est") else {}), "years": act + est}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return True


def run_se(state, codes, today, max_n):
    """스탁이지 추정치: 추정 PER 이 있는 종목 중 처음이거나 추정 EPS 가 바뀐 것만 종목별로."""
    s = earnings.session()
    try:
        val = ((earnings.get(s, "/stockdata/api/v1/valuation/data") or {}).get("data") or {}).get("data") or {}
    except PermissionError:
        val = None
        print("스탁이지 세션 만료 — 추정 EPS 비교 없이 받아 둔 지 오래된 것만 다시 받음")
    except requests.ConnectionError:
        print("스탁이지 접속 안 됨 — 추정치는 다음 실행에", flush=True)
        return 1
    stale = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=FIN_STALE)).strftime("%Y-%m-%d")
    todo, why = [], {}
    for c in codes:
        st = state.setdefault(c, {})
        got = st.get("se")
        if val is not None:
            eps = implied_eps(val.get(c) or {})
            st["has_est"] = bool(eps)                    # 화면: '추정 없음'과 '아직 못 받음' 구분
            if not eps:
                continue                                 # 증권사 추정이 없는 종목 — 실적은 DART 만으로
            if not got:
                why[c] = "new"
            elif eps_changed(st.get("eps"), eps):
                why[c] = "추정치 변경"
        elif got and got < stale:
            why[c] = "오래됨"
        if c in why:
            todo.append(c)
    order = {"추정치 변경": 0, "오래됨": 1, "new": 2}
    todo.sort(key=lambda c: order[why[c]])
    SE_RAW.mkdir(parents=True, exist_ok=True)
    done = fail = streak = 0
    for c in todo[:max_n]:
        try:
            j = earnings.get(s, f"/stockdata/api/v1/stock-info/info-tab/{c}")
            f = parse_se((j or {}).get("financials") or {})
            streak = 0
        except PermissionError:
            f = None                                     # info-tab 은 로그인 없이도 열림 — 만료로 막히면 다음에
        except Exception as e:
            print(f"  {c} 추정치 수집 실패: {type(e).__name__}", flush=True)
            f, fail = None, fail + 1
            streak += isinstance(e, requests.ConnectionError)
            if streak >= 3:                              # 서버 무응답 — 종목마다 90초씩 기다리지 말고 다음 실행에
                print("스탁이지 접속 안 됨 — 추정치 수집 중단, 다음 실행이 이어 받음", flush=True)
                break
        if f:
            (SE_RAW / f"{c}.json").write_text(json.dumps({"fetched": today, **f}, ensure_ascii=False), encoding="utf-8")
            done += 1
            st = state[c]
            st["se"] = today                             # 받은 종목만 — 실패한 종목은 다음 실행이 다시
            if val and c in val:
                st["eps"] = implied_eps(val[c])
        time.sleep(SE_GAP)
    if val:                                              # 안 받은 종목도 오늘 EPS 를 기준으로 (다음 비교용) — 받은 적 있는 것만
        for c in codes:
            if c in val and state[c].get("se") and c not in todo:
                state[c]["eps"] = implied_eps(val[c])
    cnt = {k: sum(1 for c in todo if why[c] == k) for k in order}
    print(f"추정치(스탁이지): 다시 받을 {len(todo)} ({', '.join(f'{k} {v}' for k, v in cnt.items() if v)})"
          f" · 이번에 {done} 받음" + (f" · 실패 {fail}" if fail else "") + (f" · 남은 {len(todo) - max_n}" if len(todo) > max_n else ""))
    return fail


def run_fin(state, codes, today, max_n):
    """추정치(스탁이지, 바뀐 것만) → 전 종목 data/fin 다시 만들기(DART 실적은 캐시, 없는 것만 새로)."""
    rc = run_se(state, codes, today, max_n)
    corps = json.loads(build.CORPS.read_text(encoding="utf-8"))
    n = 0
    for c in codes:
        try:
            n += build_fin(c, (corps.get(c) or {}).get("corp_code"), state.get(c, {}), today)
        except screen.DartBlocked as e:
            print(f"DART 끊김 — 나머지 실적은 다음 실행에 ({e})")
            rc = 1
            break
        except Exception as e:
            print(f"  {c} 실적 정리 실패: {type(e).__name__}: {str(e)[:100]}")
    print(f"실적·추정 화면 자료 {n}/{len(codes)}종목")
    return rc


# ── ① 사업 내용 원문 (DART) ─────────────────────────────────────────────────

TAG, WS = re.compile(r"<[^>]+>"), re.compile(r"[ \t\r\f\v]+")


def biz_section(doc):
    """사업보고서 본문에서 「II. 사업의 내용」 ~ 「III.」 앞까지 글자만."""
    m = None
    for m in re.finditer(r"<TITLE[^>]*>\s*II\.\s*사업의\s*내용", doc):
        pass                                          # 목차가 아닌 본문(마지막) 제목
    if not m:
        return ""
    end = re.search(r"<TITLE[^>]*>\s*III\.", doc[m.end():])
    body = doc[m.start(): m.end() + end.start()] if end else doc[m.start(): m.start() + 400000]
    # 하위 장(1. 사업의 개요 ~ 7. 기타) 중 개요·주요 제품·매출만 — 앞에서 자르면 매출 비중 표가 잘려 나감
    parts = [p for p in re.split(r"(?=<TITLE[^>]*>\s*\d+\.\s)", body) if re.match(r"<TITLE[^>]*>\s*\d+\.", p)]
    keep, etc = [], ""
    for p in parts:
        t = re.match(r"<TITLE[^>]*>([^<]*)", p).group(1)
        cap = next((n for k, n in BIZ_PARTS if re.search(k, t)), 0)
        if cap:
            keep.append(clean(p)[:cap])
        elif re.search(r"기타\s*참고", t):
            etc = clean(p)                            # 산업 특성·시장 점유율이 여기 있는 회사가 많음 — 남는 분량만큼
    if len(keep) < 2:
        return clean(body)[:BIZ_CHARS]
    left = BIZ_CHARS - sum(len(k) + 1 for k in keep)
    return "\n".join(keep + ([etc[:left]] if etc and left > 500 else []))


def clean(body):
    def table(t):                                     # 표는 한 행 = 한 줄 ("칸 | 칸 | 칸")
        rows = []
        for tr in re.split(r"</TR>", t.group(0), flags=re.I):
            cells = [WS.sub(" ", TAG.sub(" ", c)).strip() for c in re.split(r"</T[DH]>", tr, flags=re.I)]
            cells = [c for c in cells if c]
            if cells:
                rows.append(" | ".join(cells))
        return "\n" + "\n".join(rows) + "\n"
    body = re.sub(r"<TABLE.*?</TABLE>", table, body, flags=re.I | re.S)
    body = re.sub(r"</(P|TITLE)>", "\n", body, flags=re.I)
    text = WS.sub(" ", TAG.sub("", body))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")


def latest_annual(corp, today):
    bgn = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=500)).strftime("%Y%m%d")
    j = screen.dart("list", corp_code=corp, bgn_de=bgn, end_de=today.replace("-", ""),
                    pblntf_detail_ty="A001", page_count=10)
    rows = [x for x in j.get("list", []) if "사업보고서" in x.get("report_nm", "")
            and "첨부" not in x["report_nm"]]          # [첨부정정]·[첨부추가] 는 감사보고서 등 첨부만 — 본문 없음
    rows.sort(key=lambda x: x["rcept_dt"], reverse=True)
    return rows[0] if rows else None


def fetch_doc(rcept):
    r = requests.get(f"{screen.DART}/document.xml", params={"crtfc_key": screen.key(), "rcept_no": rcept}, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    pick = f"{rcept}.xml" if f"{rcept}.xml" in names else max(names, key=lambda n: z.getinfo(n).file_size)
    return z.read(pick).decode("utf-8", "replace")


def run_biz(state, codes, today, max_n):
    corps = json.loads(build.CORPS.read_text(encoding="utf-8"))
    season = today[5:7] in ("03", "04")               # 사업보고서 제출 철엔 매 실행 확인
    recheck = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=BIZ_RECHECK)).strftime("%Y-%m-%d")
    BIZ_RAW.mkdir(parents=True, exist_ok=True)
    got = checked = 0
    for c in codes:
        if got >= max_n:
            break
        st = state.setdefault(c, {})
        corp = (corps.get(c) or {}).get("corp_code")
        if not corp or (st.get("biz_checked", "") > recheck and not season and st.get("biz_rcept")):
            continue
        if st.get("biz_checked") == today:
            continue
        try:
            rep = latest_annual(corp, today)
            checked += 1
            st["biz_checked"] = today
            if rep:
                st["biz_nm"] = rep["report_nm"].strip()
            if not rep or rep["rcept_no"] == st.get("biz_rcept"):
                continue
            text = biz_section(fetch_doc(rep["rcept_no"]))
            got += 1
            if not text:
                print(f"  {c} 사업의 내용 못 찾음 ({rep['rcept_no']})")
                st["biz_rcept"] = rep["rcept_no"]
                continue
            (BIZ_RAW / f"{c}.txt").write_text(json.dumps(
                {"code": c, "rcept": rep["rcept_no"], "rcept_dt": rep["rcept_dt"], "report_nm": rep["report_nm"].strip(),
                 "text": text[:BIZ_CHARS]}, ensure_ascii=False), encoding="utf-8")
            st["biz_rcept"] = rep["rcept_no"]
        except screen.DartBlocked as e:
            print(f"DART 끊김 — 사업 내용은 다음 실행이 이어 받음 ({e})")
            return 1
        except Exception as e:
            print(f"  {c} 사업 내용 실패: {type(e).__name__}: {str(e)[:120]}")
    print(f"사업 내용 원문: 사업보고서 확인 {checked} · 새로 받음 {got}")
    return 0


def main():
    sys.stdout.reconfigure(line_buffering=True)       # 파일로 돌려도 진행 상황이 바로 보이게
    today = datetime.now(KST).strftime("%Y-%m-%d")
    RAW.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    codes = targets()
    rc = 0
    try:
        if "--no-fin" not in sys.argv:
            rc |= 2 if run_fin(state, codes, today, arg("--max-fin", 10)) else 0
        if "--no-biz" not in sys.argv:
            rc |= 4 if run_biz(state, codes, today, arg("--max-biz", 80)) else 0
    finally:
        STATE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for p in FIN.glob("*.json"):                      # 대상에서 빠진 종목(매수 에지에서 빠진 차트 종목) 정리
        if p.stem not in codes:
            p.unlink()
    sys.exit(rc)


if __name__ == "__main__":
    main()
