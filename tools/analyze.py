# -*- coding: utf-8 -*-
"""종목별 리포트 로그 → 숫자·리비전·목표가 근거·일정 추출 + 증권사별/통합 의견 (Claude 구독 헤드리스).

Agent SDK 가 Python 3.10+ 라 Python312 로 실행한다. build.py 를 먼저 돌려 data/stocks 가 있어야 한다.

  data/stocks/{code}.json  → data/analysis/{code}.json

누락 방지 구조 (2026-10-14):
  1단계 추출 — 증권사별로 나눠, 리포트 요약을 '줄 번호'를 붙여 넘긴다. 모델은 모든 줄에 답해야 한다
             (항목이 없으면 skip 사유). 응답에 없는 줄, 숫자가 있는데 항목도 사유도 없는 줄은
             그 줄만 골라 다시 묻는다. 결과는 coverage 로 남긴다.
  2단계 정리 — 종목 전체 리포트로 증권사별 의견·전환 사유·통합 의견을 쓴다.
검증 — 모든 항목의 raw 는 해당 '줄'에 글자 그대로 있어야 하고, 숫자는 raw 안에 있어야 한다.
계산 — 비교표·리비전(직전 리포트 대비 포함)·현재가 기준 배수는 코드가 한다. 계산값은 calc 로 표시.

사용법:
    C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python312\\python.exe analyze.py 005380 006360 ...
    ... analyze.py --all [--min 3] [--force]
    ... analyze.py --rebuild      # AI 재호출 없이 저장된 추출값으로 표·리비전만 다시 계산
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
CALL_TIMEOUT = 420
MAX_RETRY = 2
PARALLEL = 1            # 동시 호출 수. 3이면 claude 프로세스가 여러 개 떠 PC 메모리 부족으로 강제 종료됐다(2026-10-14)
COVER_ROUNDS = 2        # 빠진 줄 재질문 횟수

COMMON_RULES = """절대 규칙:
- 입력 로그에 적힌 내용만 쓴다. 배경지식·추론으로 보충하지 않는다.
- 너의 투자의견·전망·평가는 넣지 않는다("~로 보인다", "주목할 만하다" 금지).
  증권사의 판단은 증권사의 것으로 옮긴다("~로 전망", "~를 반영해 하향").
- 숫자·단위·기간은 원문 그대로. 반올림·환산하지 않는다.
- 출력은 JSON 객체 하나만(코드펜스·설명 없이)."""

EXTRACT_SYSTEM = """너는 증권사 리포트 요약 로그를 '줄 단위로 빠짐없이' 구조화하는 추출기다.
입력은 한 종목에 대한 한 증권사의 리포트들이고, 요약의 각 줄 앞에 [키]가 붙어 있다.

""" + COMMON_RULES + """

출력:
{"lines": [{"key": "키", "items": [항목, ...], "skip": "항목이 없을 때 사유"}, ...]}

- 입력의 '모든 키'에 대해 하나씩 답한다. 하나라도 빠지면 안 된다.
- 한 줄에서 여러 항목이 나오면 모두 넣는다(매출액·영업이익·OPM이 한 줄에 있으면 3개).
- 항목이 없으면 items 는 [] 이고 skip 에 사유를 짧게("숫자 없음", "증감률만", "과거 실적 서술" 등).

항목 종류 4가지 — 모든 항목에 raw(그 줄에서 근거가 된 구절을 글자 그대로, 10~80자)를 넣는다:

1) metric — 값으로 적힌 실적·추정치·밸류에이션 숫자
 {"type":"metric","item":"항목","period":"기간","value":숫자,"prev":숫자|null,"unit":"단위","raw":"..."}
 - item 표준명: 매출액, 영업이익, 지배순이익, 순이익, 세전이익, EPS, BPS, ROE, OPM, EBITDA,
   적용 PER, 적용 PBR, 적용 EV/EBITDA, 컨센서스 영업이익, 컨센서스 매출액. 표준에 없으면 원문 용어.
   '순익'은 원문에 '지배'가 있으면 지배순이익, 없으면 순이익.
   '적용 ~'은 목표가 산정에 적용한 배수만. 현재 주가 기준 배수는 '현재 PER'처럼.
 - period: "3Q26", "2026", "12M Fwd" 형식(E/F 떼기). 줄에 없으면 같은 리포트의 문맥상 분명할 때만 채우고, 아니면 "".
 - value: 원문 숫자 그대로(1.6조원 → 1.6 "조원" / 2,406십억원 → 2406 "십억원" / 15배 → 15 "배").
 - prev: 같은 줄에 기존/직전/이전 값이 함께 있으면 그 값(같은 단위), 없으면 null.
   "37,656원으로 기존 42,964원 대비" → value 37656, prev 42964. "14,697에서 11,209로" → value 11209, prev 14697.
   prev 는 '추정치를 바꾸기 전 값'만이다. 전년·전분기·과거 실적 값(예: "전년 동기 53%에서 65%로")은 prev 가 아니다.
 - 증감률(+0.7%yoy)만 있는 숫자는 metric 이 아니다.

2) revision — 추정치를 바꿨다고 적혔지만 '바뀐 뒤 절대값'이 없는 경우
 {"type":"revision","item":"항목","period":"기간","pct":숫자|null,"dir":"up"|"down","raw":"..."}
 - "2026년과 2027년 순익추정치를 각각 5%, 8% 하향" → 순이익 2026 pct -5, 순이익 2027 pct -8 (2개).
 - "실적 추정치 하향" 처럼 숫자가 없으면 pct null, item 은 원문대로("실적 추정치"), period 는 알 수 있을 때만.
 - pct 는 하향이면 음수. 절대값과 기존값이 다 있으면 revision 이 아니라 metric(prev 포함)으로.

3) basis — 목표가 산정 근거
 {"type":"basis","year":"2027"|"12M Fwd"|...|"","year_src":"명시"|"문맥"|"","year_ctx":"문맥 근거 구절"|"",
  "method":"PER"|"PBR"|"EV/EBITDA"|"SOTP"|"DCF"|...,"multiple":숫자|null,
  "base_item":"EPS"|...|"","base_value":숫자|null,"base_unit":"원"|...|"","raw":"..."}
 - year 가 그 줄에 있으면 year_src "명시".
 - 줄에 없지만 '같은 리포트'의 다른 줄에 근거가 있으면 year_src "문맥"이고 (다른 날짜 리포트는 근거로 쓰지 않는다),
   year_ctx 에 그 근거 구절을 글자 그대로. 근거가 없으면 year "" year_src "".

4) event — 리포트 날짜 이후에 일어날 것으로 적힌 일(출시·착공·양산·수주·인도·준공·임상 발표 등)
 {"type":"event","when":"4Q26"|"2H26"|"1H27"|"2027"|"2028~2030"|"","event":"40자 안팎","raw":"..."}
 - 이미 지난 실적은 event 가 아니다. "연내"는 리포트 연도로."""

SUMMARY_SYSTEM = """너는 증권사 리포트 요약 로그를 읽고 증권사별·통합 의견을 팩트로 정리하는 정리기다.
입력은 한 종목에 대해 여러 증권사가 낸 '목표주가 상향/하향 리포트 요약' 목록이다.

""" + COMMON_RULES + """

출력:
{
  "brokers": [{"broker": "증권사명", "numbers": ["...", ...], "view": ["...", ...]}],
  "flips": [{"id": 리포트번호, "note": "방향이 바뀐 이유 한 문장"}],
  "overall": {"numbers": ["...", ...], "view": ["...", ...]}
}

brokers — 입력에 나온 모든 증권사를 빠짐없이:
- numbers: 그 증권사의 핵심 숫자 먼저(27E·28E 같은 먼 기간 추정을 우선, 적용 배수, 목표가 산정 근거). 2~4줄.
- view: 현재 의견과 근거 1~3줄. 의견이 바뀌어 왔으면 흐름을 짧게("상향(8월) → 하향(10월): 환율 가정 변경").
- 한 줄 60자 안팎.
flips — 입력에서 [전환] 표시된 리포트 전부, 그 리포트 본문 근거로 방향이 바뀐 이유 한 문장.
overall:
- numbers: 여러 증권사가 공통으로 언급한 숫자를 먼 기간(28E·27E)부터. 3~5줄.
- view: 공통으로 짚은 상향/하향 이유와 엇갈리는 지점. 2~4줄."""


# ── 공통 ────────────────────────────────────────────────────────────────────

def sig(reports):
    return hashlib.md5(",".join(str(r["id"]) for r in reports).encode()).hexdigest()[:12]


async def _ask(system, prompt):
    parts = []
    options = ClaudeAgentOptions(
        model=MODEL, system_prompt=system, allowed_tools=[],
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


async def ask_json(system, prompt, label):
    last = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            return _extract_json(await asyncio.wait_for(_ask(system, prompt), CALL_TIMEOUT))
        except Exception as e:
            last = e
            print(f"  {label} 시도 {attempt} 실패: {type(e).__name__}: {str(e)[:150]}", file=sys.stderr)
    raise RuntimeError(f"{label}: {last}")


def _squash(s):
    return re.sub(r"\s+", "", s or "")


NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


def _num_in(v, raw):
    return any(abs(float(x.replace(",", "")) - v) < 1e-9
               for x in NUM_RE.findall(raw) if x.replace(",", "").replace(".", "").isdigit())


def _float(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def norm_period(p):
    p = str(p or "").strip().upper().replace(" ", "")
    if not p:
        return ""
    if re.search(r"12M|12개월|FWD|선행", p):
        return "12M Fwd"
    m = re.match(r"([1-4])Q(\d{2}|\d{4})", p)
    if m:
        return f"{m.group(1)}Q{m.group(2)[-2:]}"
    m = re.match(r"([12])H(\d{2}|\d{4})", p)
    if m:
        return f"{m.group(1)}H{m.group(2)[-2:]}"
    m = re.match(r"(?:FY)?'?(\d{4}|\d{2})(?:[EF년]|$)", p)
    if m:
        y = m.group(1)
        return y if len(y) == 4 else "20" + y
    return p


# ── 1단계: 줄 단위 추출 ───────────────────────────────────────────────────────

def report_lines(r):
    """요약을 줄로 쪼갠다. 리스크 소제목 같은 머리줄도 포함(모델이 skip 하면 된다)."""
    out = []
    for i, ln in enumerate(l for l in (r["summary"] or "").splitlines() if l.strip()):
        out.append((f"{r['id']}:{i}", ln.strip()))
    return out


def _has_number(text):
    return bool(re.search(r"\d", text))


def extract_prompt(name, broker, reps, only_keys=None):
    lines = [f"종목: {name} / 증권사: {broker} / 리포트 {len(reps)}건 (오래된 순)", ""]
    for r in sorted(reps, key=lambda r: r["id"]):
        tgt = f"{r['from']:,} → {r['to']:,}" if r["from"] else f"{r['to']:,}"
        lines.append(f"#{r['id']} | {r['date'][:10]} | {'상향' if r['dir'] == 'up' else '하향'} | "
                     f"목표가 {tgt} | {r['opinion']} | 제목: {r['title']}")
        for key, text in report_lines(r):
            if only_keys is None or key in only_keys:
                lines.append(f"[{key}] {text}")
            else:
                lines.append(f"(참고) {text}")
        lines.append("")
    if only_keys:
        lines.append(f"위에서 [키]가 붙은 {len(only_keys)}줄에 대해서만 답한다. (참고) 줄은 문맥용이다.")
    return "\n".join(lines)


async def extract_broker(name, broker, reps, sem):
    """한 증권사 리포트 전체를 줄 단위로 추출. 빠진 줄은 그 줄만 다시 묻는다."""
    want = {k: t for r in reps for k, t in report_lines(r)}
    got, retried = {}, 0
    pending = set(want)
    for rnd in range(COVER_ROUNDS + 1):
        async with sem:
            res = await ask_json(EXTRACT_SYSTEM,
                                 extract_prompt(name, broker, reps, None if rnd == 0 else pending),
                                 f"{broker} 추출{'' if rnd == 0 else f' 재질문{rnd}'}")
        for ln in res.get("lines") or []:
            k = str(ln.get("key") or "")
            if k in pending:
                items = ln.get("items") or []
                skip = str(ln.get("skip") or "").strip()
                # 숫자가 있는 줄인데 항목도 사유도 없으면 답하지 않은 것으로 본다
                if items or skip or not _has_number(want[k]):
                    got[k] = {"items": items, "skip": skip}
        pending = set(want) - set(got)
        if not pending:
            break
        retried += len(pending)
    return got, sorted(pending), retried


def verify_items(got, reports_by_id, broker_text):
    """줄별 항목을 검증해 종류별 목록으로. raw 는 그 줄에 글자 그대로 있어야 한다."""
    metrics, revisions, basis, events, dropped = [], [], [], [], 0
    for key, v in got.items():
        rid, li = key.split(":")
        r = reports_by_id[int(rid)]
        line = dict(report_lines(r))[key]
        for it in v["items"]:
            raw = str(it.get("raw") or "")
            if not raw or _squash(raw) not in _squash(line):
                dropped += 1
                continue
            base = {"id": r["id"], "broker": r["broker"], "date": r["date"], "key": key, "raw": raw}
            t = it.get("type")
            if t == "metric":
                val = _float(it.get("value"))
                if val is None or not _num_in(val, raw):
                    dropped += 1
                    continue
                prev = _float(it.get("prev"))
                if prev is not None and not _num_in(prev, raw):
                    prev = None
                metrics.append(dict(base, item=str(it.get("item") or "").strip(),
                                    period=norm_period(it.get("period")), value=val, prev=prev,
                                    unit=str(it.get("unit") or "").strip()))
            elif t == "revision":
                pct = _float(it.get("pct"))
                if pct is not None and not _num_in(abs(pct), raw):
                    pct = None
                d = it.get("dir") if it.get("dir") in ("up", "down") else ("down" if (pct or 0) < 0 else "up")
                if pct is not None:
                    pct = -abs(pct) if d == "down" else abs(pct)
                revisions.append(dict(base, item=str(it.get("item") or "").strip(),
                                      period=norm_period(it.get("period")), pct=pct, dir=d))
            elif t == "basis":
                src = it.get("year_src") if it.get("year_src") in ("명시", "문맥") else ""
                ctx = str(it.get("year_ctx") or "")
                year = norm_period(it.get("year"))
                if src == "문맥" and (not ctx or _squash(ctx) not in _squash(r["summary"] + r["title"])):
                    year, src, ctx = "", "", ""          # 문맥 근거를 확인 못 하면 미기재로
                mult, bval = _float(it.get("multiple")), _float(it.get("base_value"))
                basis.append(dict(base, year=year, year_src=src if year else "", year_ctx=ctx if src == "문맥" else "",
                                  method=str(it.get("method") or "").strip(),
                                  multiple=mult if (mult is not None and _num_in(mult, raw)) else None,
                                  base_item=str(it.get("base_item") or "").strip(),
                                  base_value=bval if (bval is not None and _num_in(bval, raw)) else None,
                                  base_unit=str(it.get("base_unit") or "").strip()))
            elif t == "event" and str(it.get("event") or "").strip():
                w = str(it.get("when") or "").strip()
                events.append(dict(base, date=r["date"][:10], when=w, sortkey=_when_key(w),
                                   event=str(it["event"]).strip()))
            else:
                dropped += 1
    return metrics, revisions, basis, events, dropped


# ── 계산 ────────────────────────────────────────────────────────────────────

KRW = {"조원": 10000, "십억원": 10, "억원": 1, "백만원": 0.01}


def _norm_unit(unit, value):
    """금액은 비교를 위해 억원으로 맞춘다(단순 자릿수 환산). 원문은 raw 에 그대로 남는다."""
    k = KRW.get((unit or "").replace(" ", ""))
    return (round(value * k, 4), "억원") if k else (value, unit)


def _when_key(w):
    """'4Q26'·'2H27'·'2027'·'2028~2030' → 정렬용 숫자. 모르면 맨 뒤."""
    w = w.replace(" ", "")
    m = re.match(r"([1-4])Q(\d{2})", w)
    if m:
        return 2000 + int(m.group(2)) + (int(m.group(1)) - 1) * 0.25
    m = re.match(r"([12])H(\d{2})", w)
    if m:
        return 2000 + int(m.group(2)) + (int(m.group(1)) - 1) * 0.5
    m = re.search(r"(20\d{2})", w)
    if m:
        return int(m.group(1)) + 0.9
    return 9999


def _period_key(p):
    """연간·선행만 숫자(28E → 27E → 12M Fwd → 26E 정렬용), 분기·반기·미상은 None."""
    if not p or "Q" in p or "H" in p:
        return None
    if "Fwd" in p:
        return 2026.5
    return int(p) if re.fullmatch(r"20\d{2}", p) else None


def build_table(metrics):
    """(항목, 기간, 단위)별로 증권사마다 가장 최근 값 비교표. 2개사 이상만."""
    groups = {}
    for m in metrics:
        if not m["period"]:
            continue
        val, unit = _norm_unit(m["unit"], m["value"])
        key = (m["item"], m["period"], unit)
        cur = groups.setdefault(key, {}).get(m["broker"])
        if not cur or m["id"] > cur["id"]:
            groups[key][m["broker"]] = dict(m, value=val)
    table = []
    for (item, period, unit), per in groups.items():
        if len(per) < 2:
            continue
        # 원문 요약 자체에 단위 오타가 있다(7,090억원을 "709.0억원"으로 적는 식). 값은 고치지 않고
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


def build_revisions(metrics, revs):
    """리비전 행 모으기. src: 원문(기존→변경) / 원문(변화율) / 방향만 / 계산(직전 리포트 대비).
    직전 리포트 대비는 같은 증권사·항목·기간·단위의 이전 리포트 값이 있고, 이번 리포트에 그 항목의
    원문 리비전이 없을 때만 계산한다. 변화율만 있고 직전 값이 있으면 바뀐 값을 계산(calc)해 붙인다."""
    rows = []
    stated = set()                                # (리포트, 항목, 기간) 원문 리비전이 있는 것
    for m in metrics:
        if m["prev"] not in (None, 0) and m["period"]:
            pct = round((m["value"] - m["prev"]) / abs(m["prev"]) * 100, 1)
            if abs(pct) > 80:                     # 단위 오타·항목 혼동으로 보이는 값
                continue
            stated.add((m["id"], m["item"], m["period"]))
            rows.append({"item": m["item"], "period": m["period"], "broker": m["broker"], "id": m["id"],
                         "date": m["date"][:10], "prev": m["prev"], "value": m["value"], "unit": m["unit"],
                         "pct": pct, "src": "stated", "raw": m["raw"]})
    hist = {}                                     # (증권사, 항목, 기간, 단위) → [(id, 억원환산값, 원값, 단위)]
    for m in sorted(metrics, key=lambda m: m["id"]):
        if m["period"]:
            v, u = _norm_unit(m["unit"], m["value"])
            hist.setdefault((m["broker"], m["item"], m["period"], u), []).append((m["id"], v, m))
    for r in revs:
        if not r["period"]:
            rows.append({"item": r["item"], "period": "", "broker": r["broker"], "id": r["id"],
                         "date": r["date"][:10], "pct": r["pct"], "dir": r["dir"],
                         "src": "pct" if r["pct"] is not None else "dir", "raw": r["raw"]})
            continue
        stated.add((r["id"], r["item"], r["period"]))
        row = {"item": r["item"], "period": r["period"], "broker": r["broker"], "id": r["id"],
               "date": r["date"][:10], "pct": r["pct"], "dir": r["dir"],
               "src": "pct" if r["pct"] is not None else "dir", "raw": r["raw"]}
        if r["pct"] is not None:                  # 직전 리포트 값이 있으면 바뀐 값을 계산해 붙인다
            for (b, item, period, u), seq in hist.items():
                if b == r["broker"] and item == r["item"] and period == r["period"]:
                    before = [x for x in seq if x[0] < r["id"]]
                    if before:
                        _, _, pm = before[-1]
                        row.update(prev=pm["value"], value=round(pm["value"] * (1 + r["pct"] / 100), 2),
                                   unit=pm["unit"], calc="value", prev_id=pm["id"])
                    break
        rows.append(row)
    for (b, item, period, u), seq in hist.items():  # 직전 리포트 대비(원문 리비전이 없는 경우)
        for (pid, pv, pm), (cid, cv, cm) in zip(seq, seq[1:]):
            if pid == cid or (cid, item, period) in stated or pv == 0:
                continue
            pct = round((cv - pv) / abs(pv) * 100, 1)
            if abs(pct) > 80 or pct == 0:
                continue
            rows.append({"item": item, "period": period, "broker": b, "id": cid, "date": cm["date"][:10],
                         "prev": pm["value"], "value": cm["value"], "unit": cm["unit"], "pct": pct,
                         "src": "calc", "prev_id": pid, "raw": cm["raw"]})

    groups = {}
    for r in rows:
        groups.setdefault((r["item"], r["period"]), []).append(r)
    out = []
    for (item, period), rs in groups.items():
        rs.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
        pcts = [x["pct"] for x in rs if x["pct"] is not None]
        out.append({"item": item, "period": period, "n": len(rs),
                    "avg": round(sum(pcts) / len(pcts), 1) if pcts else None,
                    "up": sum(1 for x in rs if (x["pct"] or 0) > 0 or (x["pct"] is None and x.get("dir") == "up")),
                    "down": sum(1 for x in rs if (x["pct"] or 0) < 0 or (x["pct"] is None and x.get("dir") == "down")),
                    "kind": "annual" if _period_key(period) is not None else ("quarter" if period else "unknown"),
                    "rows": rs})
    order = ["매출액", "영업이익", "지배순이익", "순이익", "세전이익", "EPS", "EBITDA", "BPS"]
    out.sort(key=lambda g: (-(_period_key(g["period"]) or 0), order.index(g["item"]) if g["item"] in order else 99,
                            -g["n"]))
    return out


def current_multiples(metrics, close):
    """현재가 ÷ 연도별 EPS·BPS(증권사별 최신값 평균, 원 단위) → 현재가 기준 PER·PBR."""
    if not close:
        return []
    latest = {}
    for m in metrics:
        if m["item"] not in ("EPS", "BPS") or m["unit"] not in ("원", "") or _period_key(m["period"]) is None:
            continue
        k = (m["item"], m["period"], m["broker"])
        if k not in latest or m["id"] > latest[k]["id"]:
            latest[k] = m
    agg = {}
    for (item, period, _), m in latest.items():
        if m["value"] > 0:
            agg.setdefault((item, period), []).append(m["value"])
    out = []
    for (item, period), vals in agg.items():
        med = sorted(vals)[len(vals) // 2]
        vals = [v for v in vals if 0.2 < v / med < 5]
        avg = sum(vals) / len(vals)
        out.append({"item": item, "period": period, "n": len(vals), "base": round(avg),
                    "label": "PER" if item == "EPS" else "PBR", "multiple": round(close / avg, 2)})
    out.sort(key=lambda x: (x["item"], -_period_key(x["period"])))
    return out


def derive(a, close):
    """추출값에서 화면용 집계를 만든다(AI 재호출 없이 다시 돌릴 수 있게 분리)."""
    a["table"] = build_table(a["metrics"])
    a["revisions"] = build_revisions(a["metrics"], a["revision_items"])
    a["current"] = current_multiples(a["metrics"], close)
    a["timeline"] = sorted(a["events"], key=lambda x: (x["sortkey"], x["date"]))
    a["basis_latest"] = basis_latest(a["basis"])
    return a


def basis_latest(basis):
    """증권사별 가장 최근 리포트의 산정 근거. 한 리포트에서 여러 줄로 나뉜 항목(연도·배수·기준값)을 합친다."""
    last = {}
    for b in basis:
        if b["broker"] not in last or b["id"] > last[b["broker"]]:
            last[b["broker"]] = b["id"]
    out = []
    for broker, rid in last.items():
        parts = [b for b in basis if b["broker"] == broker and b["id"] == rid]
        # 명시 연도 > 문맥 연도 > 미기재, 그다음 채워진 칸이 많은 줄을 기준으로
        parts.sort(key=lambda b: ({"명시": 0, "문맥": 1}.get(b["year_src"], 2),
                                  -sum(b[k] is not None for k in ("multiple", "base_value"))))
        m = dict(parts[0])
        for b in parts[1:]:
            for k in ("method", "multiple", "base_item", "base_value", "base_unit"):
                if m.get(k) in (None, "") and b.get(k) not in (None, ""):
                    m[k] = b[k]
        m["raws"] = [b["raw"] for b in parts]
        out.append(m)
    out.sort(key=lambda b: b["id"], reverse=True)
    return out


# ── 2단계: 의견 정리 ─────────────────────────────────────────────────────────

def summary_prompt(d):
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


# ── 실행 ────────────────────────────────────────────────────────────────────

async def analyze_async(d):
    reps_by_broker = {}
    for r in d["reports"]:
        reps_by_broker.setdefault(r["broker"], []).append(r)
    sem = asyncio.Semaphore(PARALLEL)

    async def one(broker, reps):
        got, missing, retried = await extract_broker(d["name"], broker, reps, sem)
        return broker, reps, got, missing, retried

    async def summary():
        async with sem:
            return await ask_json(SUMMARY_SYSTEM, summary_prompt(d), "의견 정리")

    results, summ = await asyncio.gather(
        asyncio.gather(*(one(b, rs) for b, rs in reps_by_broker.items())), summary())
    return results, summ


def analyze(code, force=False):
    d = json.loads((STOCKS / f"{code}.json").read_text(encoding="utf-8"))
    out_path = OUT / f"{code}.json"
    s = sig(d["reports"])
    if not force and out_path.exists() and json.loads(out_path.read_text(encoding="utf-8")).get("sig") == s:
        print(f"{code} {d['name']}: 변경 없음, 건너뜀")
        return True
    t0 = time.time()
    try:
        results, res = asyncio.run(analyze_async(d))
    except Exception as e:
        print(f"{code} {d['name']}: 실패 ({e})")
        return False

    by_id = {r["id"]: r for r in d["reports"]}
    metrics, revs, basis, events, dropped = [], [], [], [], 0
    cov = {"lines": 0, "numeric": 0, "with_items": 0, "skipped": 0, "retried": 0, "missing": []}
    for broker, reps, got, missing, retried in results:
        broker_text = _squash("".join(r["summary"] + r["title"] for r in reps))
        m, rv, b, ev, dr = verify_items(got, by_id, broker_text)
        metrics += m; revs += rv; basis += b; events += ev; dropped += dr
        all_lines = [(k, t) for r in reps for k, t in report_lines(r)]
        cov["lines"] += len(all_lines)
        cov["numeric"] += sum(1 for _, t in all_lines if _has_number(t))
        cov["with_items"] += sum(1 for v in got.values() if v["items"])
        cov["skipped"] += sum(1 for v in got.values() if not v["items"])
        cov["retried"] += retried
        cov["missing"] += missing

    flip_ids = {r["id"] for r in d["reports"] if r.get("flip")}
    out = {
        "code": code, "name": d["name"], "sig": s,
        "generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "overall": res.get("overall") or {},
        "brokers": {b["broker"]: {"numbers": b.get("numbers") or [], "view": b.get("view") or []}
                    for b in res.get("brokers") or [] if b.get("broker")},
        "flips": {str(f["id"]): f.get("note", "") for f in res.get("flips") or [] if f.get("id") in flip_ids},
        "metrics": metrics, "revision_items": revs, "basis": basis, "events": events,
        "dropped": dropped, "coverage": cov,
    }
    derive(out, d.get("close"))
    OUT.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    nb_missing = len(set(r["broker"] for r in d["reports"]) - set(out["brokers"]))
    print(f"{code} {d['name']}: 완료 {time.time() - t0:.0f}초 · 줄 {cov['lines']}(숫자 {cov['numeric']}) "
          f"처리 {cov['with_items']}+사유 {cov['skipped']} · 재질문 {cov['retried']} · 미처리 {len(cov['missing'])} · "
          f"숫자 {len(metrics)} 리비전행 {sum(g['n'] for g in out['revisions'])} 근거 {len(basis)} 일정 {len(events)} "
          f"· 검증탈락 {dropped} · 증권사 의견 누락 {nb_missing} · 전환 {len(out['flips'])}/{len(flip_ids)}")
    return True


def main():
    args = sys.argv[1:]
    force = "--force" in args
    if "--rebuild" in args:
        for p in sorted(OUT.glob("*.json")):
            a = json.loads(p.read_text(encoding="utf-8"))
            if "revision_items" not in a:
                continue
            d = json.loads((STOCKS / p.name).read_text(encoding="utf-8"))
            by_id = {r["id"]: r for r in d["reports"]}
            for b in a.get("basis", []):          # 문맥 연도는 같은 리포트 안 근거만 인정
                r = by_id.get(b["id"])
                if b.get("year_src") == "문맥" and (not r or _squash(b.get("year_ctx") or "") not in _squash(r["summary"] + r["title"])):
                    b.update(year="", year_src="", year_ctx="")
            derive(a, d.get("close"))
            p.write_text(json.dumps(a, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            print(p.stem, a["name"], "재계산")
        return
    if "--all" in args:
        mn = int(args[args.index("--min") + 1]) if "--min" in args else 1
        codes = [p.stem for p in sorted(STOCKS.glob("*.json"))
                 if len(json.loads(p.read_text(encoding="utf-8"))["reports"]) >= mn]
    else:
        codes = [a for a in args if re.fullmatch(r"[0-9A-Z]{6}", a)]
    ok = sum(analyze(c, force) for c in codes)
    print(f"끝: {ok}/{len(codes)}")


if __name__ == "__main__":
    main()
