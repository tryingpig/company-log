# -*- coding: utf-8 -*-
"""처리 속도·정확도 시험 (2026-10-14). 하루치 새 리포트만 추출해 세 방식을 비교한다.

  A: 지금 방식 — Sonnet, (종목·증권사)마다 1번 호출, 동시 1개
  B: Sonnet, 여러 (종목·증권사)를 줄 수 기준으로 묶어 호출, 동시 2개
  C: B 와 같고 모델만 Haiku

정확도는 A 를 기준으로 B·C 가 같은 줄에서 같은 숫자를 뽑았는지(검증 통과 항목만) 비교한다.
결과: bench/{day}_{mode}.json, bench/{day}_report.txt

    python312 tools/bench.py 2026-10-08 [A B C]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as an  # noqa: E402

BENCH = an.ROOT / "bench"
MODELS = {"A": "claude-sonnet-5", "B": "claude-sonnet-5", "C": "claude-haiku-5-5"}
PARALLEL = {"A": 1, "B": 2, "C": 2}
BATCH_LINES = 40

BATCH_NOTE = """
(묶음 입력) 아래에는 여러 종목·증권사 블록이 '=== 종목 / 증권사 ===' 로 나뉘어 있다.
각 [키]는 자기 블록의 리포트 문맥으로만 판단한다. 블록 사이의 내용을 섞지 않는다."""


def day_groups(day):
    """그날 나온 리포트를 (종목, 증권사) 묶음으로."""
    groups = []
    for p in sorted(an.STOCKS.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        by_b = {}
        for r in d["reports"]:
            if r["date"].startswith(day):
                by_b.setdefault(r["broker"], []).append(r)
        for b, reps in by_b.items():
            groups.append({"code": d["code"], "name": d["name"], "broker": b, "reps": reps})
    return groups


def batch_prompt(groups, only_keys=None):
    parts = []
    for g in groups:
        parts.append(f"=== {g['name']} / {g['broker']} ===")
        parts.append(an.extract_prompt(g["name"], g["broker"], g["reps"], only_keys))
    return "\n".join(parts)


def make_batches(groups):
    out, cur, n = [], [], 0
    for g in groups:
        k = sum(len(an.report_lines(r)) for r in g["reps"])
        if cur and n + k > BATCH_LINES:
            out.append(cur)
            cur, n = [], 0
        cur.append(g)
        n += k
    if cur:
        out.append(cur)
    return out


async def run_batch(groups, sem, label):
    """묶음 하나를 줄 단위로 추출. 빠진 줄은 그 줄만 다시 묻는다(analyze.extract_broker 와 같은 규칙)."""
    want = {k: t for g in groups for r in g["reps"] for k, t in an.report_lines(r)}
    got, pending, retried = {}, set(want), 0
    system = an.EXTRACT_SYSTEM + (BATCH_NOTE if len(groups) > 1 else "")
    for rnd in range(an.COVER_ROUNDS + 1):
        live = [g for g in groups if any(k in pending for r in g["reps"] for k, _ in an.report_lines(r))]
        async with sem:
            try:
                res = await an.ask_json(system, batch_prompt(live, None if rnd == 0 else pending),
                                        f"{label}{'' if rnd == 0 else f' 재질문{rnd}'}")
            except Exception as e:
                print(f"  {label} 실패: {e}", file=sys.stderr)
                res = {}
        for ln in res.get("lines") or []:
            k = str(ln.get("key") or "")
            if k in pending:
                items = ln.get("items") or []
                skip = str(ln.get("skip") or "").strip()
                if items or skip or not an._has_number(want[k]):
                    got[k] = {"items": items, "skip": skip}
        pending = set(want) - set(got)
        if not pending:
            break
        retried += len(pending)
    return got, sorted(pending), retried


async def run_mode(mode, groups):
    an.MODEL = MODELS[mode]
    sem = asyncio.Semaphore(PARALLEL[mode])
    batches = [[g] for g in groups] if mode == "A" else make_batches(groups)
    res = await asyncio.gather(*(run_batch(b, sem, f"{mode}{i + 1}/{len(batches)}") for i, b in enumerate(batches)))
    got, missing, retried = {}, [], 0
    for g, m, r in res:
        got.update(g)
        missing += m
        retried += r
    return got, missing, retried, len(batches)


def sigs(got, by_id):
    """검증을 통과한 항목을 줄별 비교용 서명으로."""
    m, rv, b, ev, dropped = an.verify_items(got, by_id, "")
    out = set()
    for x in m:
        out.add((x["key"], "metric", x["item"], x["period"], x["value"], x.get("prev")))
    for x in rv:
        out.add((x["key"], "revision", x["item"], x["period"], x.get("pct")))
    for x in b:
        out.add((x["key"], "basis", x["method"], x.get("multiple"), x["year"]))
    for x in ev:
        out.add((x["key"], "event", x.get("when")))
    return out, dropped


def main():
    day = sys.argv[1]
    modes = [a for a in sys.argv[2:] if a in MODELS] or ["A", "B", "C"]
    BENCH.mkdir(exist_ok=True)
    groups = day_groups(day)
    by_id = {r["id"]: r for g in groups for r in g["reps"]}
    lines = sum(len(an.report_lines(r)) for r in by_id.values())
    head = (f"{day}: 종목 {len({g['code'] for g in groups})}개 · (종목·증권사) {len(groups)}묶음 · "
            f"리포트 {len(by_id)}건 · 요약 줄 {lines}")
    print(head)
    for mode in modes:
        t0 = time.time()
        got, missing, retried, calls = asyncio.run(run_mode(mode, groups))
        sec = round(time.time() - t0)
        (BENCH / f"{day}_{mode}.json").write_text(json.dumps(
            {"mode": mode, "model": MODELS[mode], "parallel": PARALLEL[mode], "sec": sec, "calls": calls,
             "missing": missing, "retried": retried, "got": got}, ensure_ascii=False), encoding="utf-8")
        print(f"{mode}: {sec}초 · 호출 {calls} · 미처리 {len(missing)} · 재질문 {retried}")

    # 비교 (A 기준)
    res = {m: json.loads((BENCH / f"{day}_{m}.json").read_text(encoding="utf-8"))
           for m in MODELS if (BENCH / f"{day}_{m}.json").exists()}
    out = [head, ""]
    base = sigs(res["A"]["got"], by_id)[0] if "A" in res else None
    for m, r in res.items():
        s, dropped = sigs(r["got"], by_id)
        row = (f"{m} ({r['model']}, 동시 {r['parallel']}): {r['sec']}초 = {r['sec'] / 60:.1f}분 · 호출 {r['calls']} · "
               f"미처리 {len(r['missing'])} · 재질문 {r['retried']} · 검증 통과 항목 {len(s)} · 검증 탈락 {dropped}")
        if base is not None and m != "A":
            both = len(s & base)
            row += (f"\n   A 대비: A 항목 중 같이 잡음 {both}/{len(base)} ({both / max(len(base), 1) * 100:.0f}%) · "
                    f"A 에 없는 항목 {len(s - base)}")
            miss = sorted(base - s)[:25]
            extra = sorted(s - base)[:25]
            row += "\n   A 에만 있음(앞 25): " + json.dumps(miss, ensure_ascii=False)
            row += "\n   이쪽에만 있음(앞 25): " + json.dumps(extra, ensure_ascii=False)
        out.append(row)
    txt = "\n".join(out)
    (BENCH / f"{day}_report.txt").write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
