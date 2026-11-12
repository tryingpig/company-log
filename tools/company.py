"""기업 분석 자료 — 종목 페이지 「기업 분석」 칸 (사용자 요청 2026-11-12).

  ③ 이익 성장률: 스탁이지 연간 실적(최근 5년)·추정(3년) → data/fin/{code}.json
     · 매일 스탁이지 전 종목 PER(/valuation/data, 요청 1번)을 받아 주가÷PER 로 추정 EPS 를 되짚고,
       어제와 달라진 종목(=추정치가 바뀐 종목)·실적 발표가 지난 종목·아직 없는 종목만 종목별로 다시 받는다.
       (사용자: 일일이 다 받으면 그쪽 부담 → 바뀐 종목만)
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
STATE = RAW / "state.json"
SE_GAP = 1.5                 # 스탁이지 종목별 요청 간격(초)
FIN_STALE = 7                # 세션이 없을 때: 이 날수보다 오래된 것만 다시
BIZ_RECHECK = 30             # 사업보고서 새로 나왔나 다시 볼 간격(일) — 3~4월은 매 실행
BIZ_CHARS = 16000            # AI 에 넘길 「사업의 내용」 길이 (하위 장을 못 나눈 보고서는 앞부분)
BIZ_PARTS = [(r"개요", 5000), (r"주요\s*제품|영업의\s*(현황|종류)", 5000), (r"매출", 6000)]   # 하위 장별 최대 글자


def arg(name, default):
    return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def targets():
    codes = {p.stem for p in (DATA / "stocks").glob("*.json")} | {p.stem for p in (DATA / "charts").glob("*.json")}
    return sorted(codes)


# ── ③ 실적·추정 (스탁이지) ──────────────────────────────────────────────────

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


def parse_fin(fin):
    """info-tab financials → 연결(없으면 별도) 연간 실적 5년 + 추정 3년. 금액 억원."""
    for cs, a_key, e_key in (("C", "consolidatedYearly", "consolidatedYearlyEstimate"),
                             ("S", "separateYearly", "separateYearlyEstimate")):
        act = sorted((x for x in fin.get(a_key) or [] if x.get("revenue") is not None), key=lambda x: x["year"])
        if act:
            break
    else:
        return None
    last = act[-1]["year"]
    est = sorted((x for x in fin.get(e_key) or [] if x["year"] > last and x.get("revenue") is not None),
                 key=lambda x: x["year"])[:3]
    eok = lambda v: None if v is None else round(v / 1e8, 1)   # noqa: E731
    rows = []
    for x, e in [(x, False) for x in act[-5:]] + [(x, True) for x in est]:
        rev, gp, op = eok(x.get("revenue")), eok(x.get("gross_profit")), eok(x.get("operating_income"))
        gpm = x.get("gross_margin")
        if gpm is None and gp is not None and rev:
            gpm = gp / rev * 100
        opm = x.get("operating_margin")
        if opm is None and op is not None and rev:
            opm = op / rev * 100
        rows.append({"y": str(x["year"]), "e": e, "rev": rev, "op": op, "gp": gp,
                     "gpm": None if gpm is None else round(gpm, 1), "opm": None if opm is None else round(opm, 1)})
    return {"basis": cs, "years": rows}


def fetch_fin(s, code):
    j = earnings.get(s, f"/stockdata/api/v1/stock-info/info-tab/{code}")
    return parse_fin((j or {}).get("financials") or {})


def run_fin(state, codes, today, max_n):
    s = earnings.session()
    try:
        val = ((earnings.get(s, "/stockdata/api/v1/valuation/data") or {}).get("data") or {}).get("data") or {}
    except PermissionError:
        val = None
        print("스탁이지 세션 만료 — PER 비교 없이 오래된 것만 다시 받음")
    except requests.ConnectionError:
        print("스탁이지 접속 안 됨 — 실적 수집은 다음 실행에", flush=True)   # 사업 내용(DART)은 그대로 진행
        return 1
    # 실적 발표가 지난 종목: 발표일이 마지막 수집일 뒤
    rd = {}
    try:
        for it in json.loads(earnings.OUT.read_text(encoding="utf-8")).get("items", []):
            if it.get("record_date", "9999") <= today:
                rd[it["symbol"]] = max(rd.get(it["symbol"], ""), it["record_date"])
    except Exception:
        pass
    stale = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=FIN_STALE)).strftime("%Y-%m-%d")
    todo, why = [], {}
    for c in codes:
        st = state.setdefault(c, {})
        got = st.get("fin")
        if not (FIN / f"{c}.json").exists() or not got:
            why[c] = "new"
        elif rd.get(c, "") > got:
            why[c] = "실적 발표"
        elif val is not None and c in val and eps_changed(st.get("eps"), implied_eps(val[c])):
            why[c] = "추정치 변경"
        elif val is None and got < stale:
            why[c] = "오래됨"
        if c in why:
            todo.append(c)
    order = {"실적 발표": 0, "추정치 변경": 1, "오래됨": 2, "new": 3}
    todo.sort(key=lambda c: order[why[c]])
    FIN.mkdir(parents=True, exist_ok=True)
    done = fail = streak = 0
    for c in todo[:max_n]:
        try:
            f = fetch_fin(s, c)
            streak = 0
        except PermissionError:
            f = None                                 # info-tab 은 로그인 없이도 열림 — 만료로 막히면 다음에
        except Exception as e:
            print(f"  {c} 실적 수집 실패: {type(e).__name__}", flush=True)
            f, fail = None, fail + 1
            streak += isinstance(e, requests.ConnectionError)
            if streak >= 3:                          # 서버 무응답 — 종목마다 90초씩 기다리지 말고 다음 실행에
                print("스탁이지 접속 안 됨 — 실적 수집 중단, 다음 실행이 이어 받음", flush=True)
                break
        if f:
            (FIN / f"{c}.json").write_text(json.dumps({"code": c, "fetched": today, "src": "스탁이지", **f},
                                                     ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            done += 1
            st = state[c]
            st["fin"] = today                        # 받은 종목만 — 실패한 종목은 다음 실행이 다시
            if val and c in val:
                st["eps"] = implied_eps(val[c])
        time.sleep(SE_GAP)
    if val:                                          # 안 받은 종목도 오늘 EPS 를 기준으로 (다음 비교용) — 받은 적 있는 것만
        for c in codes:
            if c in val and state[c].get("fin") and c not in todo:
                state[c]["eps"] = implied_eps(val[c])
    cnt = {k: sum(1 for c in todo if why[c] == k) for k in order}
    print(f"실적·추정: 대상 {len(codes)} · 다시 받을 {len(todo)} ({', '.join(f'{k} {v}' for k, v in cnt.items() if v)})"
          f" · 이번에 {done} 받음" + (f" · 실패 {fail}" if fail else "") + (f" · 남은 {len(todo) - max_n}" if len(todo) > max_n else ""))
    return fail


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
            rc |= 2 if run_fin(state, codes, today, arg("--max-fin", 250)) else 0
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
