# -*- coding: utf-8 -*-
"""링크 미리보기(카카오톡·텔레그램 등) 이미지 assets/og.png 를 만든다. 1200×630, 한 번만 돌리면 된다.

    Python39-32\\python.exe tools\\make_og.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "og.png"
FONT = r"C:\Windows\Fonts\malgun.ttf"
FONT_B = r"C:\Windows\Fonts\malgunbd.ttf"

BG, TEXT, TEXT2, MUTED, LINE = "#f7f7f5", "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
UP, DOWN, MA20 = "#e34948", "#2a78d6", "#6250d6"

W, H = 1200, 630
img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# 오른쪽: 캔들 + 20일선 모양 (장식이 아니라 사이트가 보여주는 것 그대로)
x0, y0, x1, y1 = 640, 150, 1120, 470
for y in range(y0, y1 + 1, 80):
    d.line([(x0, y), (x1, y)], fill=LINE, width=2)
closes = [300, 290, 305, 320, 310, 330, 350, 340, 365, 380, 370, 395, 410, 400, 425, 440]
step = (x1 - x0) / len(closes)
prev = 295
pts = []
for i, c in enumerate(closes):
    cx = x0 + step * (i + 0.5)
    o, up = prev, c >= prev
    top, bot = max(o, c), min(o, c)
    col = UP if up else DOWN
    sy = lambda v: y1 - (v - 270) * 1.6
    d.line([(cx, sy(top + 8)), (cx, sy(bot - 8))], fill=col, width=3)
    d.rectangle([cx - step * 0.28, sy(top), cx + step * 0.28, sy(bot) + 1], fill=col)
    pts.append((cx, sy((o + c) / 2 - 6)))
    prev = c
d.line(pts, fill=MA20, width=5, joint="curve")
# 목표가 상향 마커
for i in (5, 11):
    cx = x0 + step * (i + 0.5)
    d.polygon([(cx, y1 + 18), (cx - 12, y1 + 40), (cx + 12, y1 + 40)], fill=UP)

# 왼쪽: 이름과 설명
d.text((80, 150), "Company Log", font=ImageFont.truetype(FONT_B, 78), fill=TEXT)
d.text((84, 262), "종목별 증권사 리포트 로그", font=ImageFont.truetype(FONT_B, 40), fill=TEXT2)
small = ImageFont.truetype(FONT, 28)
for i, line in enumerate(["목표가 상향·하향 원문 · 최근 의견 전환",
                          "최근 가중 통합 목표가 · 시장 대비 성과",
                          "평일 10시·20시 갱신"]):
    d.text((84, 340 + i * 46), line, font=small, fill=MUTED)
d.text((84, 540), "tryingpig.github.io/company-log", font=ImageFont.truetype(FONT, 26), fill=MUTED)

img.save(OUT, optimize=True)
print(OUT, OUT.stat().st_size)
