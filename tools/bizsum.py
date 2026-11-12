"""① 사업 내용 AI 정리 — company.py 가 받아 둔 DART 사업보고서 「II. 사업의 내용」 원문 → data/biz/{code}.json.

사업보고서가 바뀐 종목(원문 접수번호 ≠ 정리한 접수번호)만 정리한다. daily.py 가 정규 실행 끝에 부른다(Python312).
원문에 있는 사실만 옮긴다(추측·평가 금지, 사용자 원칙: 팩트 기반).

    Python312 python tools/bizsum.py [--max N]
"""
import asyncio
import json
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
           "customers": "주요 수요처·고객(원문에 적힌 것만, 없으면 \\"\\")", "share": "매출 비중(원문 숫자 그대로 예: 53.4%, 없으면 \\"\\")"}],
 "notes": ["원문에 있는 핵심 숫자 0~2개, 예: '해외 수출 비중 42.7%'"]}
규칙:
- rows 는 원문의 사업 부문 구분을 따른다. 2~5행. 부문이 하나뿐이면 주요 제품군으로 나눈다.
- 매출 비중은 원문에서 가장 최근 연도 값. 부문 합계·내부거래 조정 행은 넣지 않는다.
- 수요처는 '국내 대형 IDM·OSAT, 미국 IDM' 처럼 원문 표현을 줄여 쓴다. 회사 실명이 원문에 있으면 써도 된다.
- 금액 단위 변환·계산은 하지 않는다."""


async def one(code, sem, today):
    raw = json.loads((RAW / f"{code}.txt").read_text(encoding="utf-8"))
    async with sem:
        j = await an.ask_json(SYSTEM, f"[{raw['report_nm']} · 접수 {raw['rcept_dt']}]\n\n{raw['text']}", f"사업내용 {code}")
    rows = [{k: str(r.get(k) or "").strip() for k in ("biz", "products", "customers", "share")}
            for r in j.get("rows") or [] if r.get("biz")]
    if not j.get("line") or not rows:
        raise ValueError("빈 결과")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{code}.json").write_text(json.dumps({
        "code": code, "rcept": raw["rcept"], "rcept_dt": raw["rcept_dt"], "report_nm": raw["report_nm"],
        "line": str(j["line"]).strip(), "rows": rows[:5], "notes": [str(x) for x in (j.get("notes") or [])][:2],
        "generated": today}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


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
