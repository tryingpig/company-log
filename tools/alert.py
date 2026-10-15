# -*- coding: utf-8 -*-
"""매일 갱신 장애 알림: '이슬이와 소곤소곤(AI)' 방으로 ❌/⚠️, 정상으로 돌아오면 ✅ 1회.

같은 문제가 이어지면 다시 보내지 않고, 문제 내용이 바뀌면 다시 보낸다. 상태는 logs/alert_state.json.
봇 토큰은 공개 레포에 두지 않고 tools/alert.local.json(.gitignore)의 token_from 폴더 config.json 에서 빌린다.
"""
import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "tools" / "alert.local.json"
STATE = ROOT / "logs" / "alert_state.json"
LIVE = ROOT / "raw" / "epic_live.jsonl"
STALE_HOURS = 30          # 주말을 뺀 시간 기준, 이만큼 방 메시지가 없으면 수집 멈춤 의심


def _load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def send(text):
    cfg = _load(CFG, {})
    src = _load(Path(cfg["token_from"]) / "config.json", {})
    token = (src.get("telegram") or {}).get("bot_token") or src.get("bot_token")
    body = json.dumps({"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def weekday_hours(since, now):
    """since~now 사이에서 토·일을 뺀 시간."""
    h, t = 0.0, since
    while t < now:
        nxt = min(now, datetime.combine(t.date() + timedelta(days=1), datetime.min.time(), t.tzinfo))
        if t.weekday() < 5:
            h += (nxt - t).total_seconds() / 3600
        t = nxt
    return h


def collect_problem(now):
    """포워더가 Epic AI 방 메시지를 계속 받아 적고 있는지. 멈췄으면 사유 문자열."""
    last = None
    try:
        with open(LIVE, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    last = ln
        last = datetime.fromisoformat(json.loads(last)["date"])
    except Exception:
        return "실시간 수집 파일(raw/epic_live.jsonl)을 읽지 못함"
    gap = weekday_hours(last, now)
    if gap > STALE_HOURS:
        return f"Epic AI 방 메시지가 {last:%m-%d %H:%M} 이후 없음 (평일 기준 {gap:.0f}시간) — 포워더 수집 멈춤 의심"
    return None


def report(problems, now, logname):
    """이번 실행 결과로 알림 상태를 갱신하고 필요할 때만 보낸다. 보내기 실패는 조용히 넘기고 로그에 남긴다."""
    st = _load(STATE, {})
    stamp = f"{now:%m-%d %H:%M}"
    if problems:
        if problems != st.get("problems"):
            send("❌ company-log 매일 갱신 문제\n"
                 + "\n".join(f"- {p}" for p in problems)
                 + f"\n\n실행 {stamp} · 로그 logs/{logname}\n같은 문제는 다시 알리지 않고, 정상이 되면 ✅로 알려요.")
        st = {"problems": problems, "since": st.get("since") or stamp}
    elif st.get("problems"):
        send(f"✅ company-log 매일 갱신 정상 복구\n문제 시작 {st['since']} → 정상 확인 {stamp}")
        st = {}
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
