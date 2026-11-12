"""기업 분석 자료 — 종목 페이지 「기업 분석」 칸 (사용자 요청 2026-11-12).

  ③ 이익 성장률: 실적 5년 = DART 사업보고서 재무제표(screen 캐시 공용, 매출총이익·이익률은 계산)
                 + 추정 3년 = 와이즈리포트(네이버증권 종목분석 표) → data/fin/{code}.json
     · 사용자 11-12: 실적은 원천, 추정만 외부. 스탁이지는 10여 건 연속 조회에 끊겨 와이즈리포트로 바꿈
       (robots 는 Disallow 지만 네이버 화면 그대로의 표라 사용자가 허용). 처음·실적 발표 뒤·7일 지난 종목만 다시.
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
WR_RAW = RAW / "wr"           # 와이즈리포트 연간 요약표 정리본(실적·추정) — 추정치만 화면에 씀
WR = "https://navercomp.wisereport.co.kr/v3/company/"   # 네이버증권 종목분석 화면에 끼워진 표
WR_ROWS = ("매출액", "영업수익", "영업이익", "영업이익(발표기준)", "당기순이익", "EPS(원)", "PER(배)", "ROE(%)")
WR_EXTRA = {"ni": "당기순이익", "eps": "EPS(원)", "per": "PER(배)", "roe": "ROE(%)"}   # 표에만 쓰는 칸
STATE = RAW / "state.json"
WR_GAP = 3                   # 와이즈리포트 종목 간격(초, 종목당 요청 2번)
WR_STALE = 7                 # 추정치 다시 받는 주기(일) — 실적 발표가 지나면 바로
BIZ_RECHECK = 30             # 사업보고서 새로 나왔나 다시 볼 간격(일) — 3~4월은 매 실행
BIZ_CHARS = 16000            # AI 에 넘길 「사업의 내용」 길이 (하위 장을 못 나눈 보고서는 앞부분)
BIZ_PARTS = [(r"개요", 5000), (r"주요\s*제품|영업의\s*(현황|종류)", 5000), (r"매출", 6000)]   # 하위 장별 최대 글자


def arg(name, default):
    return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def targets():
    codes = {p.stem for p in (DATA / "stocks").glob("*.json")} | {p.stem for p in (DATA / "charts").glob("*.json")}
    return sorted(codes)


# ── ③ 실적(DART)·추정(와이즈리포트) ─────────────────────────────────────────
# 사용자 원칙(11-12): 실적은 원천 DART + 계산(매출총이익 = 매출 − 매출원가, 이익률), 추정치만 와이즈리포트.

def row(y, e, rev, op, gp):
    """금액 억원 → 화면 한 해. 이익률은 직접 계산."""
    pct = lambda a: None if a is None or not rev else round(a / rev * 100, 1)   # noqa: E731
    return {"y": str(y), "e": e, "rev": rev, "op": op, "gp": gp, "gpm": pct(gp), "opm": pct(op)}


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
    """data/fin/{c}.json = DART 실적 + 와이즈리포트 추정(받아 둔 것). 둘 다 없으면 파일 없음."""
    se = json.loads((WR_RAW / f"{c}.json").read_text(encoding="utf-8")) if (WR_RAW / f"{c}.json").exists() else None
    act = dart_act(corp, annual_year(st, today)) if corp else None
    src = "DART"
    if not act and se and se["act"]:
        act, src = se["act"], "와이즈리포트"             # DART 재무제표에서 매출을 못 읽는 종목(금융사 등)만
    if not act:
        (FIN / f"{c}.json").unlink(missing_ok=True)
        return False
    if src == "DART":                                    # 순이익·EPS·PER·ROE 는 실적 연도도 와이즈리포트 값
        wa = {x["y"]: x for x in (se or {}).get("act") or []}
        act = [{**x, **{k: (wa.get(x["y"]) or {}).get(k) for k in WR_EXTRA}} for x in act]
    last = int(act[-1]["y"])
    est = [x for x in (se or {}).get("est") or [] if int(x["y"]) > last]
    FIN.mkdir(parents=True, exist_ok=True)
    (FIN / f"{c}.json").write_text(json.dumps({
        "code": c, "act_src": src, "est_src": "와이즈리포트" if est else None,
        "fetched": (se or {}).get("fetched") if est else None, "basis": (se or {}).get("basis", "C"),
        **({"est_wait": True} if se is None else {}), "years": act + est}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return True


def parse_wr(html):
    """와이즈리포트 Financial Summary(연간) 표 → {basis, act: 실적 행, est: 추정 행}. 금액 억원 그대로."""
    cell = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s).replace("&nbsp;", " ")).strip()   # noqa: E731
    ys, basis, data = None, "C", {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cs = [cell(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)]
        if ys is None:
            m = [re.match(r"(\d{4})/\d{2}\s*(\(E\))?", c) for c in cs]
            if sum(1 for x in m if x) >= 3:
                ys = [(x.group(1), bool(x.group(2))) for x in m if x]
                basis = "S" if any("별도" in c for c in cs) else "C"
        elif cs and cs[0] in WR_ROWS:
            data[cs[0]] = cs[-len(ys):]
    if not ys:
        return None
    val = lambda k, i: num_wr((data.get(k) or [""] * len(ys))[i])   # noqa: E731
    act, est = [], []
    for i, (y, e) in enumerate(ys):
        rev = val("매출액", i) if val("매출액", i) is not None else val("영업수익", i)
        op = val("영업이익", i) if val("영업이익", i) is not None else val("영업이익(발표기준)", i)
        if rev is not None:
            r = row(y, e, rev, op, None)                 # 와이즈리포트엔 매출총이익 없음
            r.update({k: val(n, i) for k, n in WR_EXTRA.items()})
            (est if e else act).append(r)
    return {"basis": basis, "act": act, "est": est[:3]}


def num_wr(s):
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def fetch_wr(s, code):
    page = WR + f"c1010001.aspx?cmp_cd={code}"
    h = s.get(page, timeout=20)
    h.raise_for_status()
    enc, idv = re.search(r"encparam: '([^']+)'", h.text), re.search(r"id: '([^']*)'", h.text)
    if not enc:
        return None                                      # 와이즈리포트에 없는 종목(스팩·리츠 일부 등)
    time.sleep(1)
    r = s.get(WR + "ajax/cF1001.aspx", timeout=20, headers={"Referer": page, "X-Requested-With": "XMLHttpRequest"},
              params={"cmp_cd": code, "fin_typ": 0, "freq_typ": "Y", "extY": 0, "extQ": 0,
                      "encparam": enc.group(1), "id": idv.group(1) if idv else ""})
    r.raise_for_status()
    return parse_wr(r.text)


def run_wr(state, codes, today, max_n):
    """추정치(와이즈리포트): 처음·실적 발표 뒤·7일 지난 종목만. 실패·무응답이면 다음 실행에."""
    rd = {}                                              # 실적 발표가 지난 종목: 발표일이 마지막 수집일 뒤
    try:
        for it in json.loads(earnings.OUT.read_text(encoding="utf-8")).get("items", []):
            if it.get("record_date", "9999") <= today:
                rd[it["symbol"]] = max(rd.get(it["symbol"], ""), it["record_date"])
    except Exception:
        pass
    stale = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=WR_STALE)).strftime("%Y-%m-%d")
    todo, why = [], {}
    for c in codes:
        got = state.setdefault(c, {}).get("wr")
        if not got:
            why[c] = "new"
        elif rd.get(c, "") > got:
            why[c] = "실적 발표"
        elif got < stale:
            why[c] = "오래됨"
        if c in why:
            todo.append(c)
    order = {"실적 발표": 0, "new": 1, "오래됨": 2}
    todo.sort(key=lambda c: (order[why[c]], state[c].get("wr", "")))
    WR_RAW.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/150.0", "Referer": "https://stock.naver.com/"})
    done = fail = streak = 0
    for c in todo[:max_n]:
        try:
            f = fetch_wr(s, c)
            streak = 0
        except Exception as e:
            print(f"  {c} 추정치 수집 실패: {type(e).__name__}", flush=True)
            f, fail = False, fail + 1
            streak += isinstance(e, requests.ConnectionError)
            if streak >= 3:                              # 서버 무응답 — 다음 실행에
                print("와이즈리포트 접속 안 됨 — 추정치 수집 중단, 다음 실행이 이어 받음", flush=True)
                break
        if f is not False:
            (WR_RAW / f"{c}.json").write_text(json.dumps({"fetched": today, **(f or {"act": [], "est": []})},
                                                         ensure_ascii=False), encoding="utf-8")
            state[c]["wr"] = today                       # 받은 종목만 — 실패한 종목은 다음 실행이 다시
            done += 1
        time.sleep(WR_GAP)
    cnt = {k: sum(1 for c in todo if why[c] == k) for k in order}
    print(f"추정치(와이즈리포트): 다시 받을 {len(todo)} ({', '.join(f'{k} {v}' for k, v in cnt.items() if v)})"
          f" · 이번에 {done} 받음" + (f" · 실패 {fail}" if fail else "") + (f" · 남은 {len(todo) - max_n}" if len(todo) > max_n else ""))
    return fail


def run_fin(state, codes, today, max_n):
    """추정치(와이즈리포트) → 전 종목 data/fin 다시 만들기(DART 실적은 캐시, 없는 것만 새로)."""
    rc = run_wr(state, codes, today, max_n)
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
            rc |= 2 if run_fin(state, codes, today, arg("--max-fin", 120)) else 0
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
