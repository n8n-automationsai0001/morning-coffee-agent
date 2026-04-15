#!/usr/bin/env python3
"""Check Google Calendar and send results to Telegram and Slack."""

import os
import sys
import datetime
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import json
import urllib.request
import urllib.error
from typing import Optional
from pathlib import Path

# ── Import shared Google auth ─────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / 'shared'))
from google_auth import get_credentials

from googleapiclient.discovery import build

CALENDAR_EMAIL = 'aiautomations0001@gmail.com'
SLACK_CHANNEL = '#claude-dump'


# ── Timeframe parsing ────────────────────────────────────────────────────────

def parse_timeframe(arg: str):
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    arg = (arg or 'today').strip().lower()

    if arg in ('today', ''):
        return today, today + datetime.timedelta(days=1), 'Today'
    elif arg == 'tomorrow':
        return today + datetime.timedelta(days=1), today + datetime.timedelta(days=2), 'Tomorrow'
    elif arg in ('this week', 'week'):
        return today, today + datetime.timedelta(days=7), 'This Week'
    elif arg == 'next week':
        return today + datetime.timedelta(days=7), today + datetime.timedelta(days=14), 'Next Week'
    elif arg == 'next 7 days':
        return today, today + datetime.timedelta(days=7), 'Next 7 Days'
    elif arg == 'next 30 days':
        return today, today + datetime.timedelta(days=30), 'Next 30 Days'
    else:
        try:
            date = datetime.datetime.strptime(arg, '%Y-%m-%d').replace(tzinfo=datetime.timezone.utc)
            return date, date + datetime.timedelta(days=1), arg
        except ValueError:
            print(f"Unknown timeframe '{arg}'. Defaulting to today.")
            return today, today + datetime.timedelta(days=1), 'Today'


# ── Message formatting ───────────────────────────────────────────────────────

def build_message(events: list, label: str, start: datetime.datetime, end: datetime.datetime) -> str:
    header = (
        f"*Calendar: {CALENDAR_EMAIL}*\n"
        f"*{label}* ({start.strftime('%b %d')} - {end.strftime('%b %d, %Y')})\n"
        f"{'-' * 40}"
    )

    if not events:
        return f"{header}\n\n- No events found."

    lines = [header]
    current_day = None

    for event in events:
        start_str = event['start'].get('dateTime', event['start'].get('date'))

        if 'T' in start_str:
            start_dt = datetime.datetime.fromisoformat(start_str)
            end_dt = datetime.datetime.fromisoformat(event['end'].get('dateTime', start_str))
            time_str = f"{start_dt.strftime('%I:%M %p')} - {end_dt.strftime('%I:%M %p')}"
            event_day = start_dt.strftime('%A, %B %d')
        else:
            time_str = "All day"
            event_day = datetime.datetime.strptime(start_str, '%Y-%m-%d').strftime('%A, %B %d')

        if event_day != current_day:
            lines.append(f"\n*{event_day}*")
            current_day = event_day

        summary = event.get('summary', '(No title)')
        hangout = event.get('hangoutLink', '')
        location = event.get('location', '')

        bullet = f"- {time_str}  |  {summary}"
        if hangout:
            bullet += f"\n  [Meet] {hangout}"
        elif location:
            bullet += f"\n  [Location] {location}"

        lines.append(bullet)

    return "\n".join(lines)


# ── Telegram ─────────────────────────────────────────────────────────────────

def get_telegram_chat_id(bot_token: str) -> Optional[str]:
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        for update in reversed(data.get('result', [])):
            msg = update.get('message') or update.get('channel_post')
            if msg:
                return str(msg['chat']['id'])
    except Exception as e:
        print(f"  Could not auto-detect Telegram chat ID: {e}")
    return None


def send_telegram(message: str):
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        print("  [Telegram] Skipped — TELEGRAM_BOT_TOKEN not set in .env")
        return

    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not chat_id:
        print("  [Telegram] TELEGRAM_CHAT_ID not set — attempting auto-detect...")
        chat_id = get_telegram_chat_id(bot_token)
        if chat_id:
            print(f"  [Telegram] Detected chat ID: {chat_id}. Add to .env to skip next time.")
        else:
            print("  [Telegram] Could not detect chat ID. Send a message to your bot first.")
            return

    plain = message.replace('*', '')
    payload = json.dumps({"chat_id": chat_id, "text": plain}).encode()

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get('ok'):
            print("  [Telegram] Sent.")
        else:
            print(f"  [Telegram] Error: {result.get('description')}")
    except urllib.error.HTTPError as e:
        print(f"  [Telegram] Failed: {e} — {e.read().decode('utf-8', errors='replace')}")
    except Exception as e:
        print(f"  [Telegram] Failed: {e}")


# ── Slack ────────────────────────────────────────────────────────────────────

def send_slack(message: str):
    token = os.environ.get('SLACK_BOT_TOKEN')
    if not token:
        print("  [Slack] Skipped — SLACK_BOT_TOKEN not set in .env")
        return

    payload = json.dumps({"channel": SLACK_CHANNEL, "text": message, "mrkdwn": True}).encode()

    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get('ok'):
            print(f"  [Slack] Sent to {SLACK_CHANNEL}.")
        else:
            print(f"  [Slack] Error: {result.get('error')}")
    except Exception as e:
        print(f"  [Slack] Failed: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    arg = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else 'today'
    start, end, label = parse_timeframe(arg)

    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)

    events_result = service.events().list(
        calendarId=CALENDAR_EMAIL,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])
    message = build_message(events, label, start, end)

    print(message)
    print("\nSending notifications...")
    send_telegram(message)
    send_slack(message)


if __name__ == '__main__':
    main()
