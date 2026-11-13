# -*- coding: utf-8 -*-
"""매일 갱신: 데이터 빌드 → 새 리포트만 AI 정리 → GitHub 반영. 스케줄러 CompanyLogDaily(평일 10:00·15:40·20:05)·CompanyLogLight(--light, 08:30·11:30·14:00)가 실행한다.

  1) build.py (Python39-32): 일봉·3개월 구간·통합 목표가(최근 가중)·증권사별 표
  1-1) 정규 실행: flow.py — 투자자별 순매수(외국인·기관·개인, 네이버) 차트 아래 '수급' 칸
  1-2) 정규 실행: company.py — 기업 분석 자료(DART 실적 + 와이즈리포트 추정, DART 사업보고서 「사업의 내용」 원문) → 끝에 bizsum 이 새 사업보고서만 AI 정리
  2) analyze.run_async: 3개월 안 리포트 3건 이상 종목 + 이미 정리한 종목, 새 리포트만 추출·의견 정리
  3) index.json 에 종목별 AI 정리 상태를 붙이고 git push (실행 중에도 30분마다)
  4) 정규 실행이면 prune.run: 매달 첫 저녁 실행에 3개월 넘은 기록을 합침(전체는 PC 보관소), 디스크 5GB 밑이면 알림

--light (CompanyLogLight, 평일 08:30·11:30·14:00): 텔레그램 리포트·네이버·스탁이지만 — DART 를 부르는
실적 스크리닝·전 종목 매수 에지는 건너뛰고, 실적 캘린더는 받아 둔 DART 원문만 쓴다(--no-dart).
08:30 은 넥스트 장전 거래가 섞인 반쪽 봉을 피하려고 일봉을 새로 받지 않는다(build --no-price).

같은 시각에 두 개가 돌지 않게 잠금 파일을 잡는다(정규·가벼운 실행 공용). 오래 걸리면(첫 따라잡기) MAX_HOURS 에서 멈추고
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
import alert  # noqa: E402
import bizsum  # noqa: E402
import prune  # noqa: E402

ROOT = an.ROOT
PY39 = r"C:\Users\admin\AppData\Local\Programs\Python\Python39-32\python.exe"
LOCK = ROOT / "logs" / "daily.lock"
MIN_REPORTS = 3
MAX_HOURS = 22
PUSH_EVERY = 30 * 60
LIGHT = "--light" in sys.argv


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


def run_earnings():
    """실적 캘린더(earnings.py). 실패해도 나머지는 계속 — 문제 문장만 돌려준다."""
    log("실적 캘린더 시작")
    try:
        r = subprocess.run([PY39, str(ROOT / "tools" / "earnings.py")] + (["--no-dart"] if LIGHT else []),
                           cwd=ROOT, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "실적 캘린더 30분 넘게 걸려 중단 (기존 화면 유지)"
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    for ln in out.strip().splitlines()[-4:]:
        log(f"  earnings | {ln}")
    if r.returncode == 3:
        return ("실적 캘린더: 스탁이지 로그인 세션 만료 (기존 화면 유지) — 크롬에서 스탁이지 로그인 후 "
                "_api-maps\\stockeasy_session_import.py 로 세션 다시 저장")
    if r.returncode == 2:
        return "실적 캘린더: 일부 종목 재무 수집 실패 — " + (out.strip().splitlines() or [""])[-1][:200]
    if r.returncode:
        return f"실적 캘린더 실패 (exit {r.returncode}) — " + (out.strip().splitlines() or [""])[-1][:200]
    return None


def run_screen():
    """실적 스크리닝(screen.py, DART). 실패해도 나머지는 계속 — 문제 문장만 돌려준다."""
    log("실적 스크리닝 시작")
    try:
        r = subprocess.run([PY39, str(ROOT / "tools" / "screen.py")], cwd=ROOT, capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return "실적 스크리닝 1시간 넘게 걸려 중단 (기존 화면 유지)"
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    for ln in out.strip().splitlines()[-6:]:
        log(f"  screen | {ln}")
    if r.returncode == 4:
        return "실적 스크리닝: DART 접속 끊김으로 1차 자료를 못 받음 (기존 화면 유지)"
    if r.returncode == 2:
        return "실적 스크리닝: DART 접속이 도중에 끊겨 일부 종목 2·3차 미확인 (다음 실행이 이어 받음)"
    if r.returncode:
        return f"실적 스크리닝 실패 (exit {r.returncode}) — " + (out.strip().splitlines() or [""])[-1][:200]
    return None


def run_edges():
    """전 상장사 매수 에지(edges.py). 장중(10:00) 실행은 스스로 건너뛴다. 실패해도 나머지는 계속."""
    log("매수 에지 시작")
    try:
        r = subprocess.run([PY39, str(ROOT / "tools" / "edges.py")], cwd=ROOT, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        return "매수 에지 15분 넘게 걸려 중단 (기존 화면 유지)"
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    for ln in out.strip().splitlines()[-3:]:
        log(f"  edges | {ln}")
    if r.returncode:
        return f"매수 에지 실패 (exit {r.returncode}) — " + (out.strip().splitlines() or [""])[-1][:200]
    return None


def run_flow():
    """투자자별 순매수(flow.py, 네이버). 매수 에지(data/charts) 다음에 돈다. 실패해도 나머지는 계속."""
    log("수급 시작")
    try:
        r = subprocess.run([PY39, str(ROOT / "tools" / "flow.py")], cwd=ROOT, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        return "수급 15분 넘게 걸려 중단 (기존 화면 유지)"
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    for ln in out.strip().splitlines()[-2:]:
        log(f"  flow | {ln}")
    if r.returncode:
        return f"수급 수집 실패 (exit {r.returncode}) — " + (out.strip().splitlines() or [""])[-1][:200]
    return None


def run_company():
    """기업 분석 자료(company.py): 스탁이지 실적·추정(바뀐 종목만) + DART 사업보고서 「사업의 내용」 원문. 실패해도 나머지는 계속."""
    log("기업 분석 자료 시작")
    try:
        r = subprocess.run([PY39, str(ROOT / "tools" / "company.py")], cwd=ROOT, capture_output=True, timeout=2400)
    except subprocess.TimeoutExpired:
        return "기업 분석 자료 40분 넘게 걸려 중단 (다음 실행이 이어 받음)"
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    for ln in out.strip().splitlines()[-4:]:
        log(f"  company | {ln}")
    if r.returncode & 4:
        return "기업 분석: DART 접속이 끊겨 사업 내용 일부 미수집 (다음 실행이 이어 받음)"
    if r.returncode & 2:
        return "기업 분석: 스탁이지 실적 일부 수집 실패 — " + (out.strip().splitlines() or [""])[-1][:200]
    if r.returncode:
        return f"기업 분석 자료 실패 (exit {r.returncode}) — " + (out.strip().splitlines() or [""])[-1][:200]
    return None


async def main_async():
    t0 = time.time()
    no_price = LIGHT and datetime.now(an.KST).strftime("%H:%M") < "09:00"     # 장 시작 전: 전날 확정 일봉 그대로
    log("build 시작" + (" (가벼운 실행" + (", 일봉 안 받음" if no_price else "") + ")" if LIGHT else ""))
    r = subprocess.run([PY39, str(ROOT / "tools" / "build.py")] + (["--no-price"] if no_price else []),
                       cwd=ROOT, capture_output=True)
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")     # PYTHONUTF8=1 로 실행되므로 utf-8
    for ln in out.strip().splitlines()[-6:]:
        log(f"  build | {ln}")
    if r.returncode:
        raise RuntimeError(f"build.py 실패 (exit {r.returncode})")
    earn_problem = run_earnings()
    scr_problem = None if LIGHT else run_screen()          # DART 를 많이 부른다 → 정규 실행에서만
    edge_problem = None if LIGHT else run_edges()
    flow_problem = None if LIGHT else run_flow()
    comp_problem = None if LIGHT else run_company()
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
    biz_fail = [] if LIGHT else await bizsum.run(60, log)     # 새 사업보고서만 AI 정리
    note = f"새 리포트 {st['reports']}건·의견 {st['summaries']}종목"
    push(note)
    log(f"끝 {(time.time() - t0) / 60:.0f}분 · {st}")
    problems = [p for p in (earn_problem, scr_problem, edge_problem, flow_problem, comp_problem) if p]
    if biz_fail:
        problems.append(f"사업 내용 정리 실패 {len(biz_fail)}종목 (다음 실행이 다시 시도)")
    if st["stopped"]:
        problems.append(f"{MAX_HOURS}시간 안에 못 끝내고 멈춤 (다음 실행이 이어 감)")
    if st["summary_fail"]:
        problems.append(f"의견 정리 실패 {st['summary_fail']}종목")
    if st["missing_lines"]:
        problems.append(f"추출 못 한 줄 {st['missing_lines']}개")
    if not LIGHT:
        problems += prune.run(log)          # 매달 첫 저녁 실행: GitHub 3개월치로 정리, 전체는 PC 보관소에
    if problems:
        log("확인 필요: " + " / ".join(problems))
    return problems


def main():
    LOCK.parent.mkdir(exist_ok=True)
    fh = open(LOCK, "a+")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("이전 실행이 아직 돌고 있어 건너뜀")
        return
    code = 0
    try:
        problems = asyncio.run(main_async())
    except Exception as e:
        log(f"실패: {type(e).__name__}: {e}")
        problems, code = [f"실행 실패: {type(e).__name__}: {str(e)[:200]}"], 1
    now = datetime.now(an.KST)
    stale = alert.collect_problem(now)
    if stale:
        log(stale)
        problems.append(stale)
    try:
        alert.report(problems, now, f"daily_{now:%Y%m%d}.log")
    except Exception as e:
        log(f"알림 보내기 실패: {type(e).__name__}: {e}")
    sys.exit(code)


if __name__ == "__main__":
    main()
