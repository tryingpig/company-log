# -*- coding: utf-8 -*-
"""종목별 리포트 로그 → 숫자 추출 + 증권사별 의견 정리 + 통합 정리 (Claude 구독 헤드리스).

Agent SDK 가 Python 3.10+ 라 Python312 로 실행한다. build.py 를 먼저 돌려 data/stocks 가 있어야 한다.

  data/stocks/{code}.json  → data/analysis/{code}.json

- AI 는 로그 요약문에 적힌 것만 옮긴다. 숫자는 원문 구절(raw)을 그대로 함께 받아
  그 구절이 해당 리포트 본문에 실제로 있는지 코드로 확인하고, 없으면 버린다.
- 증권사별 최신값 비교표·평균은 AI 가 아니라 이 스크립트가 계산한다.
- 리포트 구성이 바뀐 종목만 다시 돌린다(sig 비교). --force 면 무조건.

사용법:
    C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python312\\python.exe analyze.py 005380 006360 ...
    ... analyze.py --all [--min 3] [--force]
"""
import asyncio
import hashlib
import io
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

ROOT = Path(__file__).resolve().parent.parent
STOCKS = ROOT / "data" / "stocks"
OUT = ROOT / "data" / "analysis"
KST = timezone(timedelta(hours=9))

MODEL = "claude-sonnet-5"
ATTEMPT_TIMEOUT = 600
MAX_RETRY = 2

SYSTEM = """너는 증권사 리포트 요약 로그를 읽고 팩트만 구조화하는 정리기다.
입력은 한 종목에 대해 여러 증권사가 낸 '목표주가 상향/하향 리포트 요약' 목록이다.

절대 규칙:
- 입력 로그에 적힌 내용만 쓴다. 배경지식·추론으로 보충하지 않는다.
- 너의 투자의견·전망·평가는 넣지 않는다("~로 보인다", "주목할 만하다" 금지).
  증권사의 판단은 증권사의 것으로 옮긴다("~로 전망", "~를 반영해 하향").
- 숫자·단위·기간은 원문 그대로. 반올림·환산하지 않는다.

출력은 JSON 객체 하나만(코드펜스·설명 없이):
{
  "metrics": [
    {"id": 리포트번호, "item": "항목", "period": "기간", "value": 숫자, "unit": "단위", "raw": "원문 구절"}
  ],
  "brokers": [
    {"broker": "증권사명", "numbers": ["숫자 중심 한 줄", ...], "view": ["의견 한 줄", ...]}
  ],
  "flips": [
    {"id": 리포트번호, "note": "방향이 바뀐 이유 한 문장"}
  ],
  "overall": {"numbers": ["...", ...], "view": ["...", ...]}
}

metrics (리포트별 숫자 추출):
- 리포트 본문에 '값'으로 적힌 실적·추정치·밸류에이션 숫자를 뽑는다.
  item 은 가능하면 표준명: 매출액, 영업이익, 지배순이익, 순이익, EPS, BPS, ROE, OPM,
  적용 PER, 적용 PBR, 적용 EV/EBITDA, 컨센서스 영업이익, 가이던스.
  표준에 없으면 원문 용어 그대로(예: 드랍액, 홀드율, 수주잔고, 판매량).
  '적용 PER/PBR/EV/EBITDA'는 목표가 산정에 적용한 배수만이다. 현재 주가 기준 배수("현 12MF EV/EBITDA 17배")는
  '현재 EV/EBITDA'처럼 '현재'를 붙인다.
- period: 분기는 "3Q26", 연간은 "2026", 12개월 선행은 "12M Fwd". 추정 표시(E/F)는 떼고, 알 수 없으면 "".
- value 는 원문 숫자를 그대로 숫자로(1.6조원 → 1.6, unit "조원" / 2,406십억원 → 2406, unit "십억원" / 15배 → 15, unit "배").
- 증감률(+0.7%yoy 등)만 있고 절대값이 없으면 넣지 않는다. 절대값 옆의 증감률은 raw 에 같이 남긴다.
- raw 는 그 리포트 본문에서 해당 숫자가 나온 구절을 '글자 그대로' 복사한다(15~60자). 바꾸면 검증에서 버려진다.

brokers (증권사별, 그 증권사의 모든 리포트를 시간순으로 본 뒤 현재 입장):
- numbers: 그 증권사가 제시한 핵심 숫자를 먼저 쓴다(실적 추정, 적용 멀티플, 목표가 산정 근거). 2~4줄.
- view: 현재 의견과 근거를 1~3줄. 의견이 바뀌어 왔으면 흐름을 짧게("상향(8월) → 하향(10월): 환율 가정 변경").
- 한 줄은 60자 안팎.

flips: 입력에서 [전환] 표시된 리포트마다, 그 리포트 본문에 적힌 근거로 방향이 바뀐 이유를 한 문장.

overall (전체 통합):
- numbers: 여러 증권사가 공통으로 언급한 숫자를 먼저(예: "3Q26 영업이익 추정 2,406~2,602억원(3개사)"). 3~5줄.
- view: 증권사들이 공통으로 짚은 상향/하향 이유와 엇갈리는 지점. 2~4줄."""


def sig(reports):
    return hashlib.md5(",".join(str(r["id"]) for r in reports).encode()).hexdigest()[:12]


def build_prompt(d):
    lines = [f"종목: {d['name']} ({d['code']})", f"리포트 {len(d['reports'])}건 (오래된 순)", ""]
    for r in sorted(d["reports"], key=lambda r: r["id"]):
        tgt = f"{r['from']:,} → {r['to']:,}" if r["from"] else f"{r['to']:,} (이전값 없음)"
        flip = " [전환]" if r.get("flip") else ""
        lines.append(f"#{r['id']} | {r['date'][:10]} | {r['broker']} | "
                     f"{'상향' if r['dir'] == 'up' else '하향'}{flip} | 목표가 {tgt} | {r['opinion']}")
        lines.append(f"제목: {r['title']}")
        lines.append(r["summary"] or "(요약 없음)")
        lines.append("")
    return "\n".join(lines)


async def _ask(prompt):
    parts = []
    options = ClaudeAgentOptions(
        model=MODEL, system_prompt=SYSTEM, allowed_tools=[],
        # bypassPermissions 면 allowed_tools 가 무시된다 → 도구를 명시적으로 막는다
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"],
        permission_mode="bypassPermissions", max_turns=2,
        mcp_servers={}, setting_sources=[], extra_args={"strict-mcp-config": None},
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )
    async for m in query(prompt=prompt, options=options):
        if isinstance(m, AssistantMessage):
            parts.extend(b.text for b in m.content if isinstance(b, TextBlock))
    return "".join(parts)


def _extract_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e <= s:
        raise ValueError("JSON 없음")
    return json.loads(t[s:e + 1])


# ── 검증 + 코드 집계 ─────────────────────────────────────────────────────────

def _squash(s):
    return re.sub(r"\s+", "", s or "")


def verify_metrics(metrics, reports):
    """raw 구절이 해당 리포트 본문에 실제로 있고, value 숫자가 raw 안에 있어야 남긴다."""
    by_id = {r["id"]: r for r in reports}
    kept, dropped = [], 0
    for m in metrics or []:
        r = by_id.get(m.get("id"))
        raw = str(m.get("raw") or "")
        try:
            val = float(m.get("value"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        body = _squash(r["summary"] + r["title"]) if r else ""
        num_ok = any(abs(float(x.replace(",", "")) - val) < 1e-9
                     for x in re.findall(r"\d[\d,]*\.?\d*", raw) if x.replace(",", "").replace(".", "").isdigit())
        if not (r and raw and _squash(raw) in body and num_ok):
            dropped += 1
            continue
        kept.append({"id": r["id"], "broker": r["broker"], "date": r["date"],
                     "item": str(m.get("item") or "").strip(), "period": str(m.get("period") or "").strip(),
                     "value": val, "unit": str(m.get("unit") or "").strip(), "raw": raw})
    return kept, dropped


KRW = {"조원": 10000, "십억원": 10, "억원": 1, "백만원": 0.01}


def _norm_unit(m):
    """금액 단위는 비교를 위해 억원으로 맞춘다(단순 자릿수 환산). 원문은 raw 에 그대로 남는다."""
    k = KRW.get(m["unit"].replace(" ", ""))
    return (round(m["value"] * k, 2), "억원") if k else (m["value"], m["unit"])


def build_table(metrics):
    """(항목, 기간, 단위)별로 증권사마다 가장 최근 값만 모아 비교표를 만든다. 2개사 이상만."""
    groups = {}
    for m in metrics:
        if not m["period"]:
            continue
        val, unit = _norm_unit(m)
        key = (m["item"], m["period"], unit)
        cur = groups.setdefault(key, {}).get(m["broker"])
        if not cur or m["id"] > cur["id"]:
            groups[key][m["broker"]] = dict(m, value=val)
    table = []
    for (item, period, unit), per in groups.items():
        if len(per) < 2:
            continue
        # 원문 요약 자체에 단위 오타가 있다(예: 7,090억원을 "709.0억원"으로 적음). 값은 고치지 않고
        # 중앙값과 5배 이상 차이 나면 '원문 단위 확인' 표시 후 평균에서 뺀다. 2개사뿐이면 평균을 내지 않는다.
        vals = sorted(m["value"] for m in per.values())
        med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2

        def odd(v):
            if len(vals) == 2:
                return vals[0] > 0 and vals[1] / vals[0] >= 5
            return med > 0 and v > 0 and not (0.2 < v / med < 5)
        rows = [{"broker": b, "value": m["value"], "date": m["date"][:10], "id": m["id"], "odd": odd(m["value"])}
                for b, m in per.items()]
        good = [r["value"] for r in rows if not r["odd"]]
        table.append({"item": item, "period": period, "unit": unit, "n": len(rows),
                      "mean": round(sum(good) / len(good), 2) if good else None,
                      "min": min(good) if good else None, "max": max(good) if good else None,
                      "odd": len(rows) - len(good),
                      "brokers": sorted(rows, key=lambda x: x["date"], reverse=True)})
    order = ["매출액", "영업이익", "지배순이익", "순이익", "EPS", "BPS", "ROE", "OPM",
             "적용 PER", "적용 PBR", "적용 EV/EBITDA"]
    table.sort(key=lambda t: (order.index(t["item"]) if t["item"] in order else 99, t["item"], t["period"]))
    return table


def analyze(code, force=False):
    d = json.loads((STOCKS / f"{code}.json").read_text(encoding="utf-8"))
    out_path = OUT / f"{code}.json"
    s = sig(d["reports"])
    if not force and out_path.exists() and json.loads(out_path.read_text(encoding="utf-8")).get("sig") == s:
        print(f"{code} {d['name']}: 변경 없음, 건너뜀")
        return True

    prompt = build_prompt(d)
    t0, last = time.time(), None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            res = _extract_json(asyncio.run(asyncio.wait_for(_ask(prompt), ATTEMPT_TIMEOUT)))
            break
        except Exception as e:
            last = e
            print(f"  {code} 시도 {attempt} 실패: {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
    else:
        print(f"{code} {d['name']}: 실패 ({last})")
        return False

    metrics, dropped = verify_metrics(res.get("metrics"), d["reports"])
    flip_ids = {r["id"] for r in d["reports"] if r.get("flip")}
    out = {
        "code": code, "name": d["name"], "sig": s,
        "generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "overall": res.get("overall") or {},
        "brokers": {b["broker"]: {"numbers": b.get("numbers") or [], "view": b.get("view") or []}
                    for b in res.get("brokers") or [] if b.get("broker")},
        "flips": {str(f["id"]): f.get("note", "") for f in res.get("flips") or [] if f.get("id") in flip_ids},
        "table": build_table(metrics),
        "metrics": metrics,
        "dropped": dropped,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{code} {d['name']}: 완료 {time.time() - t0:.0f}초 · 숫자 {len(metrics)}개(버림 {dropped}) · "
          f"비교표 {len(out['table'])}행 · 증권사 {len(out['brokers'])} · 전환 {len(out['flips'])}/{len(flip_ids)}")
    return True


def main():
    args = sys.argv[1:]
    force = "--force" in args
    if "--retable" in args:          # AI 재호출 없이 저장된 숫자로 비교표만 다시 계산
        for p in sorted(OUT.glob("*.json")):
            a = json.loads(p.read_text(encoding="utf-8"))
            a["table"] = build_table(a["metrics"])
            p.write_text(json.dumps(a, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            print(p.stem, a["name"], "비교표", len(a["table"]), "행")
        return
    if "--all" in args:
        mn = int(args[args.index("--min") + 1]) if "--min" in args else 1
        codes = []
        for p in sorted(STOCKS.glob("*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            if len(d["reports"]) >= mn:
                codes.append(p.stem)
    else:
        codes = [a for a in args if re.fullmatch(r"[0-9A-Z]{6}", a)]
    ok = sum(analyze(c, force) for c in codes)
    print(f"끝: {ok}/{len(codes)}")


if __name__ == "__main__":
    main()
