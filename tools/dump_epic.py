"""
'Epic AI - 투자어시스턴트' 방의 최근 N일 메시지를 JSON으로 1회 덤프한다.

- telegram-forwarder 의 로그인 세션(forwarder.session)을 재사용하므로
  반드시 TelegramForwarder 를 끈 상태에서 실행할 것(세션 동시 사용 금지).
- 출력: company-log/raw/epic_messages.json

사용법:
    C:\\ClaudeCode\\telegram-forwarder\\venv\\Scripts\\python.exe dump_epic.py [일수=92] [방이름키워드=Epic AI]
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon.sync import TelegramClient

FWD_DIR = r"C:\ClaudeCode\telegram-forwarder"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "raw")

load_dotenv(os.path.join(FWD_DIR, ".env"))
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_PATH = os.path.join(FWD_DIR, os.environ.get("SESSION_NAME", "forwarder"))

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 92
KEYWORD = sys.argv[2] if len(sys.argv) > 2 else "Epic AI"


def main():
    since = datetime.now(timezone.utc) - timedelta(days=DAYS)
    with TelegramClient(SESSION_PATH, API_ID, API_HASH) as client:
        if not client.is_user_authorized():
            raise SystemExit("세션 미인증 - 로그인 필요")

        matches = [d for d in client.iter_dialogs() if KEYWORD.lower() in (d.name or "").lower()]
        if not matches:
            raise SystemExit(f"'{KEYWORD}' 이 들어간 방을 찾지 못함")
        for d in matches:
            print(f"후보: {d.id}  {d.name}")
        dialog = matches[0]

        msgs = []
        for m in client.iter_messages(dialog.entity):
            if m.date < since:
                break
            msgs.append({
                "id": m.id,
                "date": m.date.astimezone(timezone(timedelta(hours=9))).isoformat(),
                "sender_id": m.sender_id,
                "text": m.message or "",
                "has_media": m.media is not None,
            })
        msgs.reverse()

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "epic_messages.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "chat_id": dialog.id,
            "chat_name": dialog.name,
            "dumped_at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
            "days": DAYS,
            "messages": msgs,
        }, f, ensure_ascii=False, indent=1)
    print(f"저장: {out}  ({len(msgs)}건, {dialog.name})")


if __name__ == "__main__":
    main()
