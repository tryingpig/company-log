"""① 사업 내용 AI 정리 — company.py 가 받아 둔 DART 사업보고서 「II. 사업의 내용」 원문 → data/biz/{code}.json.

사업보고서가 바뀐 종목(원문 접수번호 ≠ 정리한 접수번호)만 정리한다. daily.py 가 정규 실행 끝에 부른다(Python312).
원문에 있는 사실만 옮긴다(추측·평가 금지, 사용자 원칙: 팩트 기반).

    Python312 python tools/bizsum.py [--max N]
"""
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as an  # noqa: E402

ROOT = an.ROOT
RAW = ROOT / "raw" / "company" / "biz"
OUT = ROOT / "data" / "biz"
CONC = 3

SYSTEM = """너는 DART 사업보고서 「II. 사업의 내용」 원문을 종목 페이지용으로 짧게 정리한다.
원문에 적힌 사실만 쓴다. 추측·평가·전망·투자 의견은 쓰지 않는다. 원문에 없는 항목은 빈 문자열로 둔다.
JSON 하나만 출력한다:
{"line": "회사가 무엇을 하는지 한 문장. 45자 안팎, '~회사'/'~기업'으로 끝맺음",
 "rows": [{"biz": "사업 부문(짧게)", "products": "주요 제품·역할(원문 제품명 위주, 40자 안팎)",
           "customers": "주요 수요처·고객(원문에 적힌 것만, 없으면 \\"\\")", "share": "매출 비중(원문 숫자 그대로 예: 53.4%, 없으면 \\"\\")",
           "amt": "이 행의 가장 최근 연도 매출 금액(원문 숫자 그대로, 예: 257,893,193). 원문에 없으면 \\"\\""}],
 "notes": ["원문에 있는 핵심 숫자 0~2개, 예: '해외 수출 비중 42.7%'"]}
규칙:
- rows 는 원문의 사업 부문 구분을 따른다. 2~5행. 부문이 하나뿐이면 주요 제품군으로 나눈다.
- 매출 비중은 원문에서 가장 최근 연도 값. 부문 합계·내부거래 조정 행은 넣지 않는다.
- 수요처는 '국내 대형 IDM·OSAT, 미국 IDM' 처럼 원문 표현을 줄여 쓴다. 회사 실명이 원문에 있으면 써도 된다.
- 금액 단위 변환·계산은 하지 않는다. 비중 % 가 원문에 없으면 share 는 비우고 amt 에 부문별 금액을 옮긴다(연결 기준 우선).
- rows 를 매출 금액 표의 부문과 맞춘다. 여러 부문을 한 행으로 묶었으면 amt 는 그 부문 금액들을 각각 '+' 로 이어 쓴다."""


def num(s):
    """'257,893,193' / '1,234+5,678' → 합계. 못 읽으면 None."""
    try:
        return sum(float(x.replace(",", "").strip()) for x in str(s).split("+"))
    except ValueError:
        return None


def calc_share(rows):
    """비중 % 가 비어 있는 행이 있을 때 부문 금액 ÷ 합계. 모든 행에 금액이 있어야 계산(아니면 그대로)."""
    if all(r["share"] for r in rows):
        return False
    v = [num(r.get("amt", "")) for r in rows]
    if len(v) < 2 or any(x is None or x < 0 for x in v) or not sum(v):
        return False
    for r, x in zip(rows, v):
        r["share"] = f"{x / sum(v) * 100:.1f}%"
    return True


def check_share(rows, text, calc):
    """비중 검증 → (계산값 여부, 합 100% 초과 여부).
    한 행이 100% 초과·합 130% 초과면 잘못 뽑은 값이라 비움. 105~130% 는 부문 간 내부거래 제거 전 비중으로 두고 표시.
    비중 숫자 절반 이상이 원문에 없으면 모델이 금액으로 계산한 값으로 표시."""
    def pct(s):                                       # '43.67%' 또는 '봉형강 43.67%, 후판 17.37%' (행 안의 % 합)
        p = re.findall(r"(\d+(?:\.\d+)?)\s*%", s)
        return sum(map(float, p)) if p else num(s)
    v = [pct(r["share"]) for r in rows if r["share"]]
    if not v:
        return calc, False
    if any(x is None or x > 100 for x in v) or sum(v) > 130:
        for r in rows:
            r["share"] = ""
        return False, False
    src = text.replace(" ", "")
    miss = sum(1 for r in rows if r["share"]
               and any(p not in src for p in (re.findall(r"\d+(?:\.\d+)?(?=\s*%)", r["share"]) or [r["share"]])))
    return calc or miss * 2 >= len(v), sum(v) > 105


async def one(code, sem, today):
    raw = json.loads((RAW / f"{code}.txt").read_text(encoding="utf-8"))
    async with sem:
        j = await an.ask_json(SYSTEM, f"[{raw['report_nm']} · 접수 {raw['rcept_dt']}]\n\n{raw['text']}", f"사업내용 {code}")
    rows = [{k: str(r.get(k) or "").strip() for k in ("biz", "products", "customers", "share", "amt")}
            for r in j.get("rows") or [] if r.get("biz")]
    if not j.get("line") or not rows:
        raise ValueError("빈 결과")
    rows = rows[:5]
    calc = calc_share(rows)
    for r in rows:
        r.pop("amt", None)
    calc, gross = check_share(rows, raw["text"], calc)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{code}.json").write_text(json.dumps({
        "code": code, "rcept": raw["rcept"], "rcept_dt": raw["rcept_dt"], "report_nm": raw["report_nm"],
        "line": str(j["line"]).strip(), "rows": rows, "notes": [str(x) for x in (j.get("notes") or [])][:2],
        **({"share_calc": True} if calc else {}), **({"share_gross": True} if gross else {}), "generated": today}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")


def todo():
    out = []
    for p in sorted(RAW.glob("*.txt")):
        rc = json.loads(p.read_text(encoding="utf-8"))["rcept"]
        q = OUT / f"{p.stem}.json"
        if not q.exists() or json.loads(q.read_text(encoding="utf-8")).get("rcept") != rc:
            out.append(p.stem)
    return out


async def run(max_n=60, log=print):
    codes = todo()
    today = datetime.now(an.KST).strftime("%Y-%m-%d")
    sem = asyncio.Semaphore(CONC)
    res = await asyncio.gather(*(one(c, sem, today) for c in codes[:max_n]), return_exceptions=True)
    fail = [c for c, r in zip(codes, res) if isinstance(r, Exception)]
    log(f"사업 내용 정리 {len(res) - len(fail)}종목" + (f" · 실패 {len(fail)}" if fail else "")
        + (f" · 남은 {len(codes) - max_n}" if len(codes) > max_n else ""))
    return fail


if __name__ == "__main__":
    n = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 60
    asyncio.run(run(n))
