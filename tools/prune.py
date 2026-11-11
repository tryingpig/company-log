# -*- coding: utf-8 -*-
"""저장소 기록 정리: GitHub 에는 최근 3개월치 기록만, PC 보관소에는 전체 기록을 계속 쌓는다.

매달 첫 정규 저녁 실행(20:00 이후, daily.py 끝에서 잠금을 잡은 채)에 한 번 돈다.
  1) 지금 main 전체를 PC 보관소(HIST, bare 저장소)의 upto-YYYYMMDD 가지로 넣는다 — 같은 파일은 다시 안 쌓여 달마다 새로 생긴 만큼만 는다
  2) KEEP_DAYS 보다 오래된 커밋을 그 시점 파일 상태 한 벌로 합치고, 이후 커밋은 같은 내용·날짜·메시지로 다시 잇는다
  3) 마지막 파일 상태가 원래와 똑같은지 확인한 뒤 GitHub 에 force-with-lease 로 올리고 PC 작업 폴더의 옛 커밋을 치운다
C 드라이브 남은 공간이 LOW_GB 밑이면 문제로 돌려줘 소곤소곤 방에 알린다.

    python tools\\prune.py --dry              무엇을 합칠지만 보여 준다
    python tools\\prune.py --now [--dry]      이번 달에 이미 돌았어도 다시 판단
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = Path(r"C:\ClaudeCode\company-log-history.git")
STATE = ROOT / "logs" / "prune_state.json"
KEEP_DAYS = 92
LOW_GB = 5
KST = timezone(timedelta(hours=9))


def git(*args, env=None, inp=None):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, input=inp)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def disk_problem():
    free = shutil.disk_usage(str(ROOT)).free / 1024 ** 3
    if free < LOW_GB:
        return f"C 드라이브 남은 공간 {free:.1f}GB — {LOW_GB}GB 밑 (company-log 기록 보관소 {HIST} 포함, 정리 필요)"
    return None


def archive(tip, stamp):
    if not HIST.exists():
        git("init", "-q", "--bare", str(HIST))
    git("push", "-q", str(HIST), f"{tip}:refs/heads/upto-{stamp}")


def squash(log, dry):
    """KEEP_DAYS 넘은 커밋을 합친 새 main 을 만들어 올린다. 합칠 게 없으면 None."""
    tip = git("rev-parse", "main")
    cut = datetime.now(KST) - timedelta(days=KEEP_DAYS)
    rows = [ln.split(" ", 1) for ln in git("log", "--reverse", "--format=%H %ct", "main").splitlines()]
    old = [h for h, t in rows if datetime.fromtimestamp(int(t), KST) < cut]
    if len(old) < 2:
        log(f"  정리: {cut:%Y-%m-%d} 이전 합칠 커밋 {len(old)}개 — 건너뜀")
        return None
    base, rest = old[-1], [h for h, _ in rows[len(old):]]
    if git("log", "--merges", "--format=%H", "main"):
        raise RuntimeError("병합 커밋이 있어 자동 정리 불가")
    log(f"  정리: {cut:%Y-%m-%d} 이전 {len(old)}개 → 1개로 합치고, 이후 {len(rest)}개 다시 잇기")
    if dry:
        return None
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M")
    archive(tip, stamp)
    log(f"  PC 보관소에 전체 기록 저장: {HIST.name} upto-{stamp}")

    def meta(h):
        f = git("show", "-s", "--format=%an%x00%ae%x00%aD%x00%cn%x00%ce%x00%cD%x00%B", h).split("\x00", 6)
        env = dict(os.environ, GIT_AUTHOR_NAME=f[0], GIT_AUTHOR_EMAIL=f[1], GIT_AUTHOR_DATE=f[2],
                   GIT_COMMITTER_NAME=f[3], GIT_COMMITTER_EMAIL=f[4], GIT_COMMITTER_DATE=f[5])
        return env, f[6]

    env, _ = meta(base)
    first = git("show", "-s", "--format=%cs", old[0])
    last = git("show", "-s", "--format=%cs", base)
    msg = (f"이전 기록 합침: {first} ~ {last} 커밋 {len(old)}개\n\n"
           f"전체 기록은 PC 보관소 {HIST.name} 의 upto-{stamp} 가지에 있다.\n")
    new = git("commit-tree", f"{base}^{{tree}}", env=env, inp=msg)
    for h in rest:
        env, body = meta(h)
        new = git("commit-tree", f"{h}^{{tree}}", "-p", new, env=env, inp=body)
    if git("rev-parse", f"{new}^{{tree}}") != git("rev-parse", f"{tip}^{{tree}}"):
        raise RuntimeError("다시 이은 마지막 파일 상태가 원래와 달라 중단 (GitHub 그대로)")
    git("push", "-q", f"--force-with-lease=main:{tip}", "origin", f"{new}:main")
    git("update-ref", "refs/heads/main", new, tip)
    git("reflog", "expire", "--expire=now", "--all")
    git("gc", "-q", "--prune=now")
    log(f"  정리 완료: GitHub 커밋 {len(rows)} → {len(rest) + 1}개")
    return new


def run(log, dry=False, force=False):
    """daily.py 가 정규 저녁 실행 끝에 부른다. 문제 문장 목록을 돌려준다."""
    problems = []
    now = datetime.now(KST)
    month = now.strftime("%Y-%m")
    st = {}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    if force or (st.get("month") != month and now.strftime("%H:%M") >= "20:00"):
        try:
            squash(log, dry)
            if not dry:
                STATE.write_text(json.dumps({"month": month, "at": now.isoformat(timespec="seconds")}),
                                 encoding="utf-8")
        except Exception as e:
            log(f"  정리 실패: {e}")
            problems.append(f"기록 정리 실패: {str(e)[:200]}")
    d = disk_problem()
    if d:
        log("  " + d)
        problems.append(d)
    return problems


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for p in run(print, dry="--dry" in sys.argv, force="--now" in sys.argv):
        print("문제:", p)
