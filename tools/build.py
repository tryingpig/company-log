"""
raw 메시지(1회 덤프 + 포워더 실시간 기록) → 웹페이지용 data/ JSON 생성.

  raw/epic_messages.json   1회 덤프 (dump_epic.py)
  raw/epic_live.jsonl      포워더 archive 모드가 이어 붙이는 실시간 기록
        ↓ [✨ 리서치 요약] 만 파싱, 메시지 id 로 중복 제거
  data/index.json          종목 목록 (최근 리포트순)
  data/stocks/{code}.json  종목별 일봉(약 4개월) + 리포트 로그(최신순)

종목코드: EpicAI-Monitor/_universe.json(KIND) 이름 일치 → 실패 시 네이버 자동완성.
일봉: 네이버 fchart.

사용법:
    C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python39-32\\python.exe build.py [--no-price]
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW_DUMP = ROOT / "raw" / "epic_messages.json"
RAW_LIVE = ROOT / "raw" / "epic_live.jsonl"
DATA = ROOT / "data"
STOCKS = DATA / "stocks"
NAMES = DATA / "_names.json"          # 종목명 → 코드 캐시 (못 찾은 것은 null)
UNIVERSE = Path(r"C:\ClaudeCode\EpicAI-Monitor\_universe.json")
ALIASES = Path(__file__).with_name("aliases.json")

BARS = 150                            # 6개월(약 124거래일) + 20일선·볼린저 워밍업
UA = {"User-Agent": "Mozilla/5.0"}
KST = timezone(timedelta(hours=9))

HEAD_RE = re.compile(r'^목표주가 (상향|하향)\s*\|\s*(.+?)\s*\|\s*"(.*?)\s*"\s*$', re.M)
# "목표주가 13,000원 >> 12,000원 | 투자의견 매수-유지 | 유진투자증권"  또는 이전값 없이 "목표주가 740,000원 | ..."
PRICE_RE = re.compile(r'^목표주가 (?:(.+?) >> )?(.+?) \| 투자의견 (.+?) \| (.+?)\s*$', re.M)
SUMMARY_RE = re.compile(r'^✅[^\n]*\n(.*)\Z', re.M | re.S)


def _won(s):
    if s is None:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def load_messages():
    msgs = {}
    if RAW_DUMP.exists():
        for m in json.loads(RAW_DUMP.read_text(encoding="utf-8"))["messages"]:
            msgs[m["id"]] = m
    if RAW_LIVE.exists():
        for line in RAW_LIVE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = json.loads(line)
                msgs[m["id"]] = m
    return sorted(msgs.values(), key=lambda m: m["id"])


def parse(m):
    text = m["text"]
    if not text.startswith("[✨ 리서치 요약]"):
        return None
    h, p = HEAD_RE.search(text), PRICE_RE.search(text)
    if not (h and p):
        return None
    prev, new = _won(p.group(1)), _won(p.group(2))
    s = SUMMARY_RE.search(text)
    return {
        "id": m["id"],
        "date": m["date"][:16].replace("T", " "),
        "name": h.group(2),
        "dir": "up" if h.group(1) == "상향" else "down",
        "title": h.group(3).strip(),
        "from": prev,
        "to": new,
        "diff": (new - prev) if (prev is not None and new is not None) else None,
        "opinion": p.group(3).strip(),
        "broker": p.group(4).strip(),
        "summary": s.group(1).strip() if s else "",
        "foreign": bool(re.search(r"USD|EUR|JPY|HKD|CNY|TWD|GBP|달러|엔|위안", p.group(2))),   # 311USD 처럼 외화 목표가 = 해외 종목 ("NONE" 표기는 국내)
    }


# ── 종목코드 ────────────────────────────────────────────────────────────────

def _norm(s):
    return re.sub(r"\s+", "", s).lower()


def resolve_codes(names):
    DATA.mkdir(parents=True, exist_ok=True)
    cache = json.loads(NAMES.read_text(encoding="utf-8")) if NAMES.exists() else {}
    uni = {}
    aliases = json.loads(ALIASES.read_text(encoding="utf-8")) if ALIASES.exists() else {}
    if UNIVERSE.exists():
        for code, v in json.loads(UNIVERSE.read_text(encoding="utf-8")).items():
            uni[_norm(v["name"])] = code
    for name in names:
        if name in aliases:                 # 사명 변경 등 수동 연결이 우선
            cache[name] = aliases[name]
            continue
        if cache.get(name):                 # 못 찾은 이름(None)은 매번 다시 조회
            continue
        code = uni.get(_norm(name))
        if not code:
            try:
                r = requests.get("https://ac.stock.naver.com/ac",
                                 params={"q": name, "target": "stock"}, headers=UA, timeout=10)
                items = [i for i in r.json().get("items", []) if i.get("nationCode") == "KOR"]
                exact = [i for i in items if _norm(i["name"]) == _norm(name)]
                code = (exact or items or [{}])[0].get("code")
                time.sleep(0.1)
            except Exception as e:
                print(f"  코드 조회 실패 {name}: {e}")
        cache[name] = code
    NAMES.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return cache


ALIAS_NAMES = set(json.loads(ALIASES.read_text(encoding="utf-8"))) if ALIASES.exists() else set()


def current_name(code):
    """종목코드로 지금 상장 이름 조회(사명 변경 종목만, 몇 건 안 됨)."""
    try:
        r = requests.get("https://ac.stock.naver.com/ac", params={"q": code, "target": "stock"}, headers=UA, timeout=10)
        for i in r.json().get("items", []):
            if i.get("code") == code:
                return i["name"]
    except Exception as e:
        print(f"  현재 이름 조회 실패 {code}: {e}")
    return None


# ── 일봉 ────────────────────────────────────────────────────────────────────

def fetch_bars(code):
    for attempt in range(3):
        try:
            r = requests.get("https://fchart.stock.naver.com/sise.nhn",
                             params={"symbol": code, "timeframe": "day",
                                     "count": str(BARS), "requestType": "0"},
                             headers=UA, timeout=10)
            r.raise_for_status()
            rows = []
            for s in re.findall(r'data="([^"]+)"', r.text):
                p = s.split("|")
                if len(p) == 6 and p[0].isdigit():
                    d = p[0]
                    o, h, l, c, v = (int(float(x)) for x in p[1:])
                    if c <= 0:          # 거래정지일은 0으로 옴
                        continue
                    rows.append({"d": f"{d[:4]}-{d[4:6]}-{d[6:]}", "o": o, "h": h, "l": l, "c": c, "v": v})
            return rows
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


# ── 증권사별 집계 (코드 계산 — 로그에 적힌 목표가만 쓴다) ──────────────────────

WINDOW_DAYS = 92     # 통합 목표가·증권사별 의견·AI 정리는 최근 3개월 리포트만 쓴다 (차트·로그 카드는 6개월)
FLIP_DAYS = 183      # 전환(같은 증권사 상향↔하향)은 직전 리포트를 6개월까지 거슬러 찾는다 (2026-10-20 사용자 결정)


HALF_LIFE = 30       # 최근 가중: 30일 지날 때마다 무게 절반 (오늘 1 → 30일 전 0.5 → 60일 전 0.25)


def recency_weight(date_str, today=None):
    today = today or datetime.now(KST).date()
    age = (today - datetime.strptime(date_str[:10], "%Y-%m-%d").date()).days
    return round(0.5 ** (max(age, 0) / HALF_LIFE), 3)


def _gap(target, close):
    return round((target / close - 1) * 100, 1) if (target and close) else None


def summarize_brokers(reps, close):
    """reps: 최신순. 최근 3개월 리포트로 증권사별 최신 목표가·괴리율·통합 목표가.
    전환(같은 증권사 상향↔하향)은 직전 리포트를 6개월(FLIP_DAYS)까지 거슬러 찾는다."""
    now = datetime.now(KST)
    cutoff = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    flip_cut = (now - timedelta(days=FLIP_DAYS)).strftime("%Y-%m-%d")
    for r in reps:
        r["win"] = r["date"][:10] >= cutoff
        r["flip"] = False
        r.pop("prev_dir", None)
        r.pop("prev_date", None)
    last6, n6 = {}, {}
    for r in sorted((r for r in reps if r["date"][:10] >= flip_cut), key=lambda r: r["id"]):   # 오래된 것부터
        prev = last6.get(r["broker"])
        if prev and prev["dir"] != r["dir"]:
            r["flip"], r["prev_dir"], r["prev_date"] = True, prev["dir"], prev["date"][:10]
        last6[r["broker"]] = r
        n6[r["broker"]] = n6.get(r["broker"], 0) + 1
    reps = [r for r in reps if r["win"]]
    by_broker = {}
    for r in sorted(reps, key=lambda r: r["id"]):
        by_broker[r["broker"]] = r

    brokers = []
    for name, last in by_broker.items():
        mine = [r for r in reps if r["broker"] == name]       # 최신순
        brokers.append({
            "broker": name, "count": len(mine),
            "up": sum(r["dir"] == "up" for r in mine), "down": sum(r["dir"] == "down" for r in mine),
            "last_id": last["id"], "last_date": last["date"], "last_dir": last["dir"],
            "target": last["to"], "prev_target": last["from"], "opinion": last["opinion"],
            "gap": _gap(last["to"], close),
            "weight": recency_weight(last["date"]),
            "flips": [r["id"] for r in mine if r.get("flip")],
            "single": n6.get(name, 0) == 1,                   # 6개월 안 1건 → 전환 판단 불가
        })
    brokers.sort(key=lambda b: b["last_id"], reverse=True)

    tgts = sorted(b["target"] for b in brokers if b["target"])
    wts = [(b["target"], b["weight"]) for b in brokers if b["target"]]
    consensus = None
    if tgts:
        n = len(tgts)
        med = tgts[n // 2] if n % 2 else (tgts[n // 2 - 1] + tgts[n // 2]) / 2
        mean = sum(tgts) / n
        wmean = sum(t * w for t, w in wts) / sum(w for _, w in wts)
        consensus = {"n": n, "wmean": round(wmean), "gap_wmean": _gap(wmean, close), "half_life": HALF_LIFE,
                     "mean": round(mean), "median": round(med), "max": tgts[-1], "min": tgts[0],
                     "gap_mean": _gap(mean, close), "gap_median": _gap(med, close),
                     "up": sum(1 for b in brokers if b["last_dir"] == "up"),
                     "days": WINDOW_DAYS}
    return brokers, consensus


def main():
    no_price = "--no-price" in sys.argv
    msgs = load_messages()
    reports = [r for r in (parse(m) for m in msgs) if r]
    foreign = sorted({r["name"] for r in reports if r["foreign"]})
    if foreign:
        print(f"해외 종목 제외 {len(foreign)}개: {foreign}")
    reports = [r for r in reports if not r.pop("foreign")]
    research = sum(1 for m in msgs if m["text"].startswith("[✨ 리서치 요약]"))
    print(f"메시지 {len(msgs)}건 / 리서치 요약 {research}건 / 파싱 {len(reports)}건")

    names = sorted({r["name"] for r in reports})
    codes = resolve_codes(names)
    missing = [n for n in names if not codes.get(n)]
    if missing:
        print(f"코드 못 찾음 {len(missing)}개: {missing}")

    by_code = {}
    for r in reports:
        code = codes.get(r["name"])
        if code:
            by_code.setdefault(code, {"name": r["name"], "reports": []})["reports"].append(r)

    STOCKS.mkdir(parents=True, exist_ok=True)
    prices = {}
    if not no_price:
        with ThreadPoolExecutor(8) as ex:
            for code, bars in zip(by_code, ex.map(fetch_bars, by_code)):
                prices[code] = bars
        print(f"일봉 수집 실패 {sum(1 for b in prices.values() if not b)}개")

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    index = []
    for code, v in by_code.items():
        path = STOCKS / f"{code}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        bars = prices.get(code) or old.get("prices", [])   # 수집 실패 시 기존 값 유지
        reps = sorted(v["reports"], key=lambda r: r["id"], reverse=True)
        v["name"] = reps[0]["name"]                          # 사명이 바뀌었으면 최근 이름으로
        if v["name"] in ALIAS_NAMES:                         # 옛 이름 리포트뿐이면 지금 상장 이름으로
            v["name"] = current_name(code) or v["name"]
        for r in reps:
            r.pop("name", None)
        close = bars[-1]["c"] if bars else None
        brokers, consensus = summarize_brokers(reps, close)
        path.write_text(json.dumps({"code": code, "name": v["name"], "updated": now,
                                    "close": close, "close_date": bars[-1]["d"] if bars else None,
                                    "consensus": consensus, "brokers": brokers,
                                    "prices": bars, "reports": reps},
                                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        last = reps[0]
        index.append({"code": code, "name": v["name"], "count": len(reps),
                      "up": sum(r["dir"] == "up" for r in reps),
                      "down": sum(r["dir"] == "down" for r in reps),
                      "last_date": last["date"], "last_dir": last["dir"],
                      "last_broker": last["broker"], "last_to": last["to"],
                      "win_count": sum(r["win"] for r in reps)})
    index.sort(key=lambda x: x["last_date"], reverse=True)
    (DATA / "index.json").write_text(json.dumps({"updated": now, "stocks": index},
                                                ensure_ascii=False, separators=(",", ":")),
                                     encoding="utf-8")
    print(f"종목 {len(index)}개 기록 완료 → {DATA}")


if __name__ == "__main__":
    main()
