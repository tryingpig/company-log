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
SIGNAL_LOG = DATA / "signal_log.json"     # 거래량 신호 + 신호 뒤 결과 누적 기록 (지우지 않음)
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

MARKETS = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}


def fetch_index(sym):
    """시장 지수 일봉 종가(소수점 유지). 실패하면 None."""
    for attempt in range(3):
        try:
            r = requests.get("https://fchart.stock.naver.com/sise.nhn",
                             params={"symbol": sym, "timeframe": "day", "count": str(BARS), "requestType": "0"},
                             headers=UA, timeout=10)
            r.raise_for_status()
            rows = []
            for s in re.findall(r'data="([^"]+)"', r.text):
                p = s.split("|")
                if len(p) == 6 and p[0].isdigit() and float(p[4]) > 0:
                    rows.append({"d": f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]}", "c": round(float(p[4]), 2)})
            if rows:
                return rows
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None


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
FEED_DAYS = 31       # '의견 전환'·'거래량 신호' 탭에 담는 기간 (화면은 이번 주+지난주 펼침, 이전은 주 단위 더 보기)
FLIP_BADGE_DAYS = 14 # 목록 종목 줄 '⚠ 최근 2주 전환' 배지·칩 기간
SIG_TDAYS = 5        # 목록 종목 줄 '● 신호'·'▼ 매도 우위' 배지·칩 기간 (최근 거래일 수)
AFTER_H = (5, 10, 20)  # 신호 성적표: 신호 뒤 몇 거래일 결과를 보나
TREND_SIG = 20       # 성적표 추세 차트: 결과가 나온 최근 20거래일 동안의 신호로 한 점
TREND_MIN = 8        # 신호가 이보다 적은 날은 점을 찍지 않는다
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


# ── 거래량 신호·에지 (earnings.py 도 같이 쓴다) ──────────────────────────────

def strength_signals_mc(bars, mc):
    """강도·거래량 신호(사용자 정의 조건, 2026-10-20): 그날
      ① 상대강도(종가 ÷ 지수)가 직전 20거래일 최고치를 넘고
      ② 거래량이 평소(그날 뺀 직전 20거래일 평균)의 2배 이상이고
      ③ 종가가 20일 이동평균 위
      ④ 양봉(종가 > 시가)이고 전일 종가보다 상승 — 음봉 대량 거래는 매도 우위라 제외(사용자 결정 2026-10-21).
    nth = 직전 20거래일 안 신호 수 + 1 (1이면 첫 신호). mc = 지수 종가 {날짜: 값}."""
    rows = [b for b in bars if b["d"] in mc]
    rs = [b["c"] / mc[b["d"]] for b in rows]
    out, idx = [], []
    for i in range(21, len(rows)):
        va = sum(b["v"] for b in rows[i - 20:i]) / 20
        ma = sum(b["c"] for b in rows[i - 19:i + 1]) / 20
        if (va and rs[i] > max(rs[i - 20:i]) and rows[i]["v"] / va >= 2 and rows[i]["c"] > ma
                and rows[i]["c"] > rows[i]["o"] and rows[i]["c"] > rows[i - 1]["c"]):
            nth = sum(1 for j in idx if i - 20 <= j < i) + 1
            idx.append(i)
            out.append({"d": rows[i]["d"], "volx": round(rows[i]["v"] / va, 1), "nth": nth,
                        "chg": round((rows[i]["c"] / rows[i - 1]["c"] - 1) * 100, 1)})
    return out


def sell_signals(bars):
    """매도 우위 대량 거래(사용자 정의, 2026-10-21): 음봉(종가 < 시가) + 거래량 평소(직전 20거래일 평균)의 2배 이상.
    trend = 전일 종가가 20일선 위였는지(상승 추세 중에 나온 매도 우위인지)."""
    out = []
    for i in range(21, len(bars)):
        b = bars[i]
        va = sum(x["v"] for x in bars[i - 20:i]) / 20
        if va and b["c"] < b["o"] and b["v"] / va >= 2:
            ma_prev = sum(x["c"] for x in bars[i - 20:i]) / 20
            out.append({"d": b["d"], "volx": round(b["v"] / va, 1),
                        "chg": round((b["c"] / bars[i - 1]["c"] - 1) * 100, 1),
                        "trend": bars[i - 1]["c"] > ma_prev})
    return out


def edge_states(bars):
    """매수·매도 에지(사용자 정의, 2026-10-21) — 날짜마다 1 / -1 / 0. 모두 AND.
      매수: 5일선·10일선이 둘 다 20일선 위, 20일선이 60일선 위, 네 선 모두 전일보다 상승
      매도: 5일선·10일선이 둘 다 20일선 아래, 20일선이 60일선 아래, 네 선 모두 전일보다 하락"""
    c = [b["c"] for b in bars]
    out = []
    for i in range(len(c)):
        if i < 60:
            out.append(0)
            continue
        m = {n: sum(c[i - n + 1:i + 1]) / n for n in (5, 10, 20, 60)}
        p = {n: sum(c[i - n:i]) / n for n in (5, 10, 20, 60)}
        if min(m[5], m[10]) > m[20] > m[60] and all(m[n] > p[n] for n in m):
            out.append(1)
        elif max(m[5], m[10]) < m[20] < m[60] and all(m[n] < p[n] for n in m):
            out.append(-1)
        else:
            out.append(0)
    return out


def edge_now(states, bars):
    cur = states[-1] if states else 0
    run = 0
    for v in reversed(states):
        if cur and v == cur:
            run += 1
        else:
            break
    return {"s": cur, "run": run, "since": bars[len(bars) - run]["d"]} if cur else None


def write_signal_stats(slog, base, dates, now):
    """신호 기록 저장 + 성적표(signal_stats.json): 묶음별 h거래일 뒤 평균·시장 대비·시장 이긴 비율,
    추세 = 날짜마다 '그날까지 결과가 나온 최근 TREND_SIG거래일 신호'의 시장 이긴 비율·평균 수익률."""
    rows = sorted(slog.values(), key=lambda r: (r["d"], r["k"], r["code"]))
    SIGNAL_LOG.write_text(json.dumps({"updated": now, "rows": rows}, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")

    def med(v):
        v = sorted(v)
        return round((v[(len(v) - 1) // 2] + v[len(v) // 2]) / 2, 1) if v else None

    def summ(rs, pairs=None):
        out = {}
        for h in AFTER_H:
            p = pairs[h] if pairs else [(r[f"r{h}"], r[f"x{h}"]) for r in rs if f"x{h}" in r]
            if p:
                out[str(h)] = {"n": len(p), "r": round(sum(a for a, _ in p) / len(p), 1),
                               "x": round(sum(b for _, b in p) / len(p), 1),
                               "beat": round(100 * sum(b > 0 for _, b in p) / len(p))}
        if rs:
            out["hi20"] = med([r["hi20"] for r in rs if "hi20" in r])
            out["lo20"] = med([r["lo20"] for r in rs if "lo20" in r])
            out["since"] = rs[0]["d"]
        return out

    buy = [r for r in rows if r["k"] == "buy"]
    sell = [r for r in rows if r["k"] == "sell"]
    groups = {"buy": buy, "buy_first": [r for r in buy if r["nth"] == 1], "buy_repeat": [r for r in buy if r["nth"] > 1],
              "sell_trend": [r for r in sell if r["trend"]], "sell_below": [r for r in sell if not r["trend"]]}
    card = {g: summ(rs) for g, rs in groups.items()}
    card["base"] = summ([], base)

    pos = {d: i for i, d in enumerate(dates)}
    trend = {}
    for g in ("buy", "sell_trend"):
        idx = [(pos[r["d"]], r) for r in groups[g] if r["d"] in pos]
        t_ = {}
        for h in (5, 20):
            beat, ret = [], []
            for t in range(len(dates)):
                sel = [r for i, r in idx if t - h - TREND_SIG + 1 <= i <= t - h and f"x{h}" in r]
                ok = len(sel) >= TREND_MIN
                beat.append(round(100 * sum(r[f"x{h}"] > 0 for r in sel) / len(sel)) if ok else None)
                ret.append(round(sum(r[f"r{h}"] for r in sel) / len(sel), 1) if ok else None)
            t_[f"beat{h}"], t_[f"r{h}"] = beat, ret
        trend[g] = t_
    (DATA / "signal_stats.json").write_text(json.dumps({"updated": now, "card": card, "dates": dates, "trend": trend,
                                                        "trend_sig": TREND_SIG},
                                                       ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


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

    # 시장 대비 성과용 지수(코스피·코스닥). 하나라도 실패하면 기존 파일 유지
    mpath = DATA / "market.json"
    if not no_price:
        got = {m: fetch_index(m) for m in MARKETS}
        if all(got.values()):
            mpath.write_text(json.dumps({"updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), **got},
                                        separators=(",", ":")), encoding="utf-8")
        else:
            print(f"지수 수집 실패: {[m for m, v in got.items() if not v]} (기존 market.json 유지)")
    uni = json.loads(UNIVERSE.read_text(encoding="utf-8")) if UNIVERSE.exists() else {}
    mdata = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
    mclose = {m: {x["d"]: x["c"] for x in mdata.get(m, [])} for m in MARKETS}

    def strength_signals(bars, market):
        return strength_signals_mc(bars, mclose.get(market) or {})

    def pct(a, b):
        return round((a / b - 1) * 100, 1)

    def after(bars, market, d):
        """신호 뒤 결과(신호일 종가 기준): h거래일 뒤 수익률 r·시장 대비 x(%p), 20거래일 안 최고·최저, 지금까지(now)."""
        mc = mclose.get(market) or {}
        i = next((k for k, b in enumerate(bars) if b["d"] == d), None)
        if i is None or not bars[i]["c"]:
            return {}, {}
        c0, m0 = bars[i]["c"], mc.get(d)
        o = {"c0": c0}
        for h in AFTER_H:
            if i + h < len(bars):
                o[f"r{h}"] = pct(bars[i + h]["c"], c0)
                mj = mc.get(bars[i + h]["d"])
                if m0 and mj:
                    o[f"x{h}"] = round(o[f"r{h}"] - pct(mj, m0), 1)
        w = bars[i + 1:i + 21]
        if len(w) == 20:
            o["hi20"], o["lo20"] = pct(max(b["h"] for b in w), c0), pct(min(b["l"] for b in w), c0)
        last, ml = bars[-1], mc.get(bars[-1]["d"])
        now = {"days": len(bars) - 1 - i, "rnow": pct(last["c"], c0)}
        if m0 and ml:
            now["xnow"] = round(now["rnow"] - pct(ml, m0), 1)
        if len(bars) >= 20:
            now["ma20"] = last["c"] > sum(b["c"] for b in bars[-20:]) / 20
        return o, now

    # 신호 기록은 지우지 않고 쌓는다(일봉은 6개월만 받아도 옛 신호 결과가 남도록, 2026-10-27 사용자 결정)
    slog = {}
    if SIGNAL_LOG.exists():
        slog = {(r["k"], r["code"], r["d"]): r for r in json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))["rows"]}
    base = {h: [] for h in AFTER_H}                          # 기준선: 같은 종목들을 아무 날이나 샀다면 (r, x)

    tdays = sorted(set(mclose.get("KOSPI", {})))[-SIG_TDAYS:]
    sellfeed = []
    sig_cut = tdays[0] if tdays else "9999"
    sigfeed = []

    def relperf(bars, market):
        """목록용 시장 대비: 1주(5)·1개월(21)·3개월(63거래일) 초과 수익률(%p) + 3개월 상대강도 주 단위 흐름(3개월 전=100)."""
        mc = mclose.get(market) or {}
        rows = [b for b in bars if b["d"] in mc]
        out = {}
        for k, n in (("r1w", 5), ("r1m", 21), ("rel3", 63)):
            if len(rows) > n:
                a, b = rows[-1], rows[-1 - n]
                out[k] = round(((a["c"] / b["c"]) - (mc[a["d"]] / mc[b["d"]])) * 100, 1)
            else:
                out[k] = None
        if len(rows) > 63:
            base = rows[-64]["c"] / mc[rows[-64]["d"]]
            pts = list(range(len(rows) - 64, len(rows), 5)) + [len(rows) - 1]
            out["rs"] = [round(rows[i]["c"] / mc[rows[i]["d"]] / base * 100, 1) for i in sorted(set(pts))]
        else:
            out["rs"] = None
        return out

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    index = []
    feed = []                                                # '의견 전환' 탭 (최근 FEED_DAYS일)
    feed_cut = (datetime.now(KST) - timedelta(days=FEED_DAYS)).strftime("%Y-%m-%d")
    badge_cut = (datetime.now(KST) - timedelta(days=FLIP_BADGE_DAYS)).strftime("%Y-%m-%d")
    for code, v in by_code.items():
        path = STOCKS / f"{code}.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        bars = prices.get(code) or old.get("prices", [])   # 수집 실패 시 기존 값 유지
        for b_ in bars:                                      # 거래정지일: 네이버가 시가·고가·저가를 0으로 준다 → 종가로 채움
            if not (b_["o"] and b_["h"] and b_["l"]):
                b_["o"] = b_["h"] = b_["l"] = b_["c"]
        reps = sorted(v["reports"], key=lambda r: r["id"], reverse=True)
        v["name"] = reps[0]["name"]                          # 사명이 바뀌었으면 최근 이름으로
        if v["name"] in ALIAS_NAMES:                         # 옛 이름 리포트뿐이면 지금 상장 이름으로
            v["name"] = current_name(code) or v["name"]
        for r in reps:
            r.pop("name", None)
        close = bars[-1]["c"] if bars else None
        brokers, consensus = summarize_brokers(reps, close)
        market = (uni.get(code) or {}).get("market") or old.get("market")
        sigs = strength_signals(bars, market) if market else []
        sells = sell_signals(bars)
        edges = edge_states(bars)
        for kind, lst, out in (("sell", sells, sellfeed), ("buy", sigs, sigfeed)):
            for s_ in lst:
                o, aft = after(bars, market, s_["d"])
                slog[(kind, code, s_["d"])] = {"k": kind, "code": code, "name": v["name"], "market": market, **s_, **o}
                if s_["d"] >= feed_cut:
                    out.append({"date": s_["d"], "code": code, "name": v["name"], "market": market, **s_, **aft})
        mc_ = mclose.get(market) or {}
        for i in range(20, len(bars)):
            if bars[i]["d"] in mc_ and bars[i]["c"]:
                for h in AFTER_H:
                    if i + h < len(bars) and bars[i + h]["d"] in mc_:
                        r_ = pct(bars[i + h]["c"], bars[i]["c"])
                        base[h].append((r_, r_ - pct(mc_[bars[i + h]["d"]], mc_[bars[i]["d"]])))
        path.write_text(json.dumps({"code": code, "name": v["name"], "updated": now, "market": market,
                                    "signals": sigs, "sells": sells,
                                    "edges": "".join({1: "+", -1: "-", 0: "."}[x] for x in edges),
                                    "close": close, "close_date": bars[-1]["d"] if bars else None,
                                    "consensus": consensus, "brokers": brokers,
                                    "prices": bars, "reports": reps},
                                   ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        rb = {"up": set(), "down": set()}                        # 최근 2주 전환: 바뀐 뒤 방향별 증권사(중복 없이)
        rlast = None
        for r in reps:
            if r.get("flip") and r["date"][:10] >= feed_cut:
                if r["date"][:10] >= badge_cut:
                    rb[r["dir"]].add(r["broker"])
                    rlast = max(rlast or "", r["date"][:10])
                feed.append({"date": r["date"][:16], "code": code, "name": v["name"], "broker": r["broker"],
                             "prev_dir": r["prev_dir"], "dir": r["dir"], "from": r["from"], "to": r["to"], "id": r["id"]})
        last = reps[0]
        index.append({"code": code, "name": v["name"], "count": len(reps),
                      "up": sum(r["dir"] == "up" for r in reps),
                      "down": sum(r["dir"] == "down" for r in reps),
                      "last_date": last["date"], "last_dir": last["dir"],
                      "last_broker": last["broker"], "last_to": last["to"],
                      "win_count": sum(r["win"] for r in reps),
                      # 목록 정렬·필터용 (최근 3개월 기준)
                      "market": market,
                      "up3": sum(r["win"] and r["dir"] == "up" for r in reps),
                      "down3": sum(r["win"] and r["dir"] == "down" for r in reps),
                      "flips3": sum(len(b["flips"]) for b in brokers),
                      "brokers3": len(brokers),
                      "wmean": (consensus or {}).get("wmean"), "gap": (consensus or {}).get("gap_wmean"),
                      **relperf(bars, market),
                      "flip2w": {"up": len(rb["up"]), "down": len(rb["down"]), "last": rlast} if rlast else None,
                      "edge": edge_now(edges, bars),
                      "sell5": next(({"d": s_["d"], "volx": s_["volx"], "trend": s_["trend"]} for s_ in reversed(sells)
                                     if s_["d"] >= sig_cut), None),
                      "sig5": next(({"d": s_["d"], "nth": s_["nth"], "volx": s_["volx"]} for s_ in reversed(sigs)
                                    if s_["d"] >= sig_cut), None)})
    index.sort(key=lambda x: x["last_date"], reverse=True)
    feed.sort(key=lambda f: (f["date"], f["id"]), reverse=True)
    for f_ in sigfeed:                                        # 목록 줄과 같은 숫자를 쓰도록 시장 대비·흐름선을 붙인다
        e = next((x for x in index if x["code"] == f_["code"]), {})
        f_.update(rel3=e.get("rel3"), rs=e.get("rs"))
        f_.pop("d", None)
    sigfeed.sort(key=lambda f: (f["date"], f["volx"]), reverse=True)
    for f_ in sellfeed:
        e = next((x for x in index if x["code"] == f_["code"]), {})
        f_.update(rel3=e.get("rel3"), rs=e.get("rs"))
        f_.pop("d", None)
    sellfeed.sort(key=lambda f: (f["date"], f["volx"]), reverse=True)
    (DATA / "signals.json").write_text(json.dumps({"updated": now, "tdays": tdays, "signals": sigfeed, "sells": sellfeed},
                                                  ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA / "flips.json").write_text(json.dumps({"updated": now, "days": FEED_DAYS, "flips": feed},
                                                ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_signal_stats(slog, base, sorted(mclose.get("KOSPI", {})), now)
    (DATA / "index.json").write_text(json.dumps({"updated": now, "stocks": index},
                                                ensure_ascii=False, separators=(",", ":")),
                                     encoding="utf-8")
    print(f"종목 {len(index)}개 기록 완료 → {DATA}")


if __name__ == "__main__":
    main()
