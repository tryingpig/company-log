# -*- coding: utf-8 -*-
"""매일 갱신: 데이터 빌드 → 새 리포트만 AI 정리 → GitHub 반영. 스케줄러(CompanyLogDaily, 평일 10:00·20:00)가 실행한다.

  1) build.py (Python39-32): 일봉·3개월 구간·통합 목표가(최근 가중)·증권사별 표
  2) analyze.run_async: 3개월 안 리포트 3건 이상 종목 + 이미 정리한 종목, 새 리포트만 추출·의견 정리
  3) index.json 에 종목별 AI 정리 상태를 붙이고 git push (실행 중에도 30분마다)

같은 시각에 두 개가 돌지 않게 잠금 파일을 잡는다. 오래 걸리면(첫 따라잡기) MAX_HOURS 에서 멈추고
다음 실행이 이어 간다.

    C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python312\\python.exe tools\\daily.py
"""
import asyncio
import json
import msvcrt
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as an  # noqa: E402  (stdout 을 utf-8 로 바꿔 둔다)

ROOT = an.ROOT
PY39 = r"C:\Users\admin\AppData\Local\Programs\Python\Python39-32\python.exe"
LOCK = ROOT / "logs" / "daily.lock"
MIN_REPORTS = 3
MAX_HOURS = 22
PUSH_EVERY = 30 * 60


def log(msg):
    print(f"[{datetime.now(an.KST):%H:%M:%S}] {msg}", flush=True)


def git(*args, check=False):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


def enrich_index():
    """메인 목록에 종목별 AI 정리 상태(날짜·대기 리포트 수)를 붙인다."""
    p = ROOT / "data" / "index.json"
    idx = json.loads(p.read_text(encoding="utf-8"))
    for s in idx["stocks"]:
        ap = an.OUT / f"{s['code']}.json"
        if ap.exists():
            a = json.loads(ap.read_text(encoding="utf-8"))
            s["ai"] = {"date": a.get("generated"), "pending": a.get("pending", 0),
                       "summary_pending": bool(a.get("needs_summary"))}
        elif s.get("win_count", 0) >= MIN_REPORTS:
            s["ai"] = {"queued": True}
        else:
            s.pop("ai", None)
    p.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def push(note):
    enrich_index()
    git("add", "data")
    if not git("diff", "--cached", "--quiet").returncode:
        return False
    git("commit", "-q", "-m", f"자동 갱신 {datetime.now(an.KST):%Y-%m-%d %H:%M} · {note}", check=True)
    if git("push", "-q").returncode:
        git("pull", "--rebase", "-q", check=True)
        git("push", "-q", check=True)
    return True


async def pusher(stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), PUSH_EVERY)
        except asyncio.TimeoutError:
            try:
                if await asyncio.to_thread(push, "AI 정리 진행 중"):
                    log("중간 반영 push")
            except Exception as e:
                log(f"중간 push 실패: {e}")


async def main_async():
    t0 = time.time()
    log("build 시작")
    r = subprocess.run([PY39, str(ROOT / "tools" / "build.py")], cwd=ROOT, capture_output=True)
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")     # PYTHONUTF8=1 로 실행되므로 utf-8
    for ln in out.strip().splitlines()[-6:]:
        log(f"  build | {ln}")
    if r.returncode:
        raise RuntimeError(f"build.py 실패 (exit {r.returncode})")
    push("데이터 빌드")

    codes = an.targets(MIN_REPORTS)
    log(f"AI 정리 대상 {len(codes)}종목")
    stop = asyncio.Event()
    bg = asyncio.create_task(pusher(stop))
    try:
        st = await an.run_async(codes, deadline=t0 + MAX_HOURS * 3600)
    finally:
        stop.set()
        await bg
    an.rebuild_all()                       # 대상 밖으로 빠진 종목도 구간·가중치 다시 계산
    note = f"새 리포트 {st['reports']}건·의견 {st['summaries']}종목"
    push(note)
    log(f"끝 {(time.time() - t0) / 60:.0f}분 · {st}")
    if st["summary_fail"] or st["missing_lines"] or st["stopped"]:
        log("확인 필요: 의견 정리 실패·미처리 줄·시간 초과 중 하나가 있음 (다음 실행이 이어 감)")


def main():
    LOCK.parent.mkdir(exist_ok=True)
    fh = open(LOCK, "a+")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("이전 실행이 아직 돌고 있어 건너뜀")
        return
    try:
        asyncio.run(main_async())
    except Exception as e:
        log(f"실패: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
