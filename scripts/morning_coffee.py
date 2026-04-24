#!/usr/bin/env python3
"""
Morning Coffee — fetches today's calendar and portfolio, sends a combined
briefing to Telegram as a screenshot image and as a professional HTML email.
"""

import os
import re
import sys
import json
import time
import uuid
import base64
import subprocess
import datetime
import traceback
import urllib.request
import urllib.error
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
REPO_ROOT    = SCRIPT_DIR.parent
ENV_FILE     = REPO_ROOT / '.env'

GCAL_SCRIPT   = REPO_ROOT / 'calendar' / 'gcal.py'
HOLDINGS_FILE = REPO_ROOT / 'stock-portfolio' / 'holdings.json'

sys.path.insert(0, str(REPO_ROOT / 'stock-portfolio'))

BRIEFING_EMAIL = 'aiautomations0001@gmail.com'


# ── Load .env ─────────────────────────────────────────────────────────────────

def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ── Run calendar script without Telegram/Slack sends ─────────────────────────

def run_silent(script_path: Path, args: list = []) -> str:
    env = os.environ.copy()
    env['TELEGRAM_BOT_TOKEN'] = ''
    env['SLACK_BOT_TOKEN']    = ''
    result = subprocess.run(
        [sys.executable, str(script_path)] + args,
        capture_output=True, text=True,
        encoding='utf-8', errors='replace', env=env
    )
    return result.stdout.strip()


# ── Strip noise lines from script output ─────────────────────────────────────

def clean_output(raw: str) -> str:
    skip_prefixes = (
        'Fetching', 'Sending', '[Telegram]', '[Slack]',
        '[Info]', '[Warning]', 'Unknown timeframe',
    )
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not any(stripped.startswith(p) for p in skip_prefixes):
            lines.append(line)
    return '\n'.join(lines).strip()


# ── Fetch portfolio table ─────────────────────────────────────────────────────

def fetch_portfolio_table() -> str:
    try:
        import portfolio as pf
        holdings = pf.load_holdings()
        table_html = pf.build_telegram_table(holdings)
        return table_html.replace('<pre>', '').replace('</pre>', '').strip()
    except Exception as e:
        return f"(Portfolio unavailable: {e})"


# ── HTML helpers ──────────────────────────────────────────────────────────────

def html_escape(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def md_to_telegram_html(text: str) -> str:
    parts = re.split(r'(\*[^*\n]+\*)', text)
    out = []
    for part in parts:
        if part.startswith('*') and part.endswith('*') and len(part) > 2:
            out.append(f'<b>{html_escape(part[1:-1])}</b>')
        else:
            out.append(html_escape(part))
    return ''.join(out)


# ── Retry helper ──────────────────────────────────────────────────────────────

def _retry(fn, label: str, attempts: int = 3):
    """Call fn() up to `attempts` times with exponential backoff. Returns fn's result on success, None on total failure."""
    delay = 1
    last_err = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            print(f"  [{label}] attempt {i}/{attempts} failed: {e}")
            if i < attempts:
                time.sleep(delay)
                delay *= 2
    print(f"  [{label}] giving up after {attempts} attempts.")
    traceback.print_exception(type(last_err), last_err, last_err.__traceback__)
    return None


# ── Telegram helpers ──────────────────────────────────────────────────────────

def get_telegram_chat_id(bot_token: str):
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        for update in reversed(data.get('result', [])):
            msg = update.get('message') or update.get('channel_post')
            if msg:
                return str(msg['chat']['id'])
    except Exception as e:
        print(f"  [Telegram] Could not auto-detect chat ID: {e}")
    return None


def _get_bot_and_chat():
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        print("  [Telegram] Skipped — TELEGRAM_BOT_TOKEN not set in .env")
        return None, None
    chat_id = os.environ.get('TELEGRAM_CHAT_ID') or get_telegram_chat_id(bot_token)
    if not chat_id:
        print("  [Telegram] Could not detect chat ID.")
        return None, None
    return bot_token, chat_id


def send_telegram_photo(image_bytes: bytes, caption: str) -> bool:
    """Send a PNG image to Telegram via sendPhoto (multipart upload). Returns True on success."""
    bot_token, chat_id = _get_bot_and_chat()
    if not bot_token:
        return False

    boundary = uuid.uuid4().hex.encode('ascii')

    def field(name: str, value: str) -> bytes:
        return (
            b'--' + boundary + b'\r\n'
            b'Content-Disposition: form-data; name="' + name.encode() + b'"\r\n\r\n' +
            value.encode('utf-8') + b'\r\n'
        )

    body = (
        field('chat_id', str(chat_id)) +
        field('caption', caption) +
        b'--' + boundary + b'\r\n'
        b'Content-Disposition: form-data; name="photo"; filename="briefing.png"\r\n'
        b'Content-Type: image/png\r\n\r\n' +
        image_bytes + b'\r\n' +
        b'--' + boundary + b'--\r\n'
    )

    def _send():
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary.decode()}'}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if not result.get('ok'):
            raise RuntimeError(f"Telegram API error: {result.get('description')}")
        return result

    if _retry(_send, "Telegram") is not None:
        print("  [Telegram] Briefing image sent.")
        return True
    return False


def send_telegram_text(message: str) -> bool:
    """Fallback: send plain HTML text message. Returns True on success."""
    bot_token, chat_id = _get_bot_and_chat()
    if not bot_token:
        return False
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()

    def _send():
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if not result.get('ok'):
            raise RuntimeError(f"Telegram API error: {result.get('description')}")
        return result

    if _retry(_send, "Telegram") is not None:
        print("  [Telegram] Briefing text sent (fallback).")
        return True
    return False


# ── Playwright screenshot ─────────────────────────────────────────────────────

def render_briefing_screenshot(html: str) -> bytes:
    """
    Render HTML to PNG using html2image (headless Edge/Chrome — no compiler needed).
    Writes to a temp file, reads it back, auto-crops the bottom gray margin.
    """
    import tempfile

    try:
        from html2image import Html2Image
    except ImportError:
        print("  [Screenshot] Installing html2image (one-time)...")
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'html2image'])
        from html2image import Html2Image

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            hti = Html2Image(output_path=tmpdir)
            # 648px wide = 600px card + 24px padding each side
            # 1200px tall = safe ceiling; we crop the bottom gray margin after
            hti.screenshot(html_str=html, save_as='briefing.png', size=(648, 1200))
            raw = (Path(tmpdir) / 'briefing.png').read_bytes()

        # Auto-crop: remove trailing rows of body background color (#f4f4f7)
        raw = _autocrop_bottom(raw)
        print("  [Screenshot] Rendered.")
        return raw
    except Exception as e:
        print(f"  [Screenshot] Failed: {e}")
        return None


def _autocrop_bottom(png_bytes: bytes) -> bytes:
    """Trim rows of background gray (#f4f4f7 = 244,244,247) from the bottom."""
    try:
        from PIL import Image
    except ImportError:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'Pillow'])
        from PIL import Image

    import io
    img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
    w, h = img.size
    pixels = img.load()
    bg = (244, 244, 247)  # #f4f4f7

    last_row = h - 1
    for y in range(h - 1, -1, -1):
        if any(pixels[x, y] != bg for x in range(w)):
            last_row = y
            break

    cropped = img.crop((0, 0, w, last_row + 28))  # 28px bottom breathing room
    buf = io.BytesIO()
    cropped.save(buf, format='PNG')
    return buf.getvalue()


# ── Parse calendar text ───────────────────────────────────────────────────────

def parse_calendar_for_email(calendar_text: str) -> list:
    skip_prefixes = ('Calendar:', 'Today', 'Tomorrow', 'This week', 'Next week', 'Next 30 days')
    events = []
    current_event = None

    for line in calendar_text.splitlines():
        m = re.match(r'^\*([^*]+)\*\s*$', line.strip())
        if m:
            header = m.group(1)
            if any(header.startswith(p) for p in skip_prefixes):
                continue
            if current_event:
                events.append(current_event)
                current_event = None
            events.append({'date_header': header})
            continue

        if not line.strip() or re.match(r'^[-=]{3,}', line.strip()):
            continue

        m = re.match(r'^-\s+(.+?)\s+\|\s+(.+)$', line.strip())
        if m:
            if current_event:
                events.append(current_event)
            current_event = {
                'time': m.group(1).strip(),
                'title': m.group(2).strip(),
                'detail': '',
                'detail_type': '',
            }
            continue

        if current_event:
            m_meet = re.match(r'^\s*\[Meet\]\s+(.+)$', line)
            if m_meet:
                current_event['detail'] = m_meet.group(1).strip()
                current_event['detail_type'] = 'meet'
                continue
            m_loc = re.match(r'^\s*\[Location\]\s+(.+)$', line)
            if m_loc:
                current_event['detail'] = m_loc.group(1).strip()
                current_event['detail_type'] = 'location'
                continue

    if current_event:
        events.append(current_event)
    return events


# ── Parse portfolio text ──────────────────────────────────────────────────────

def parse_portfolio_for_email(portfolio_text: str) -> dict:
    result = {'title': '', 'headers': [], 'rows': [], 'summary': ''}
    for line in portfolio_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('PORTFOLIO'):
            result['title'] = stripped
        elif re.match(r'^-{4,}', stripped):
            continue
        elif stripped.startswith('MKT:') or stripped.startswith('Total'):
            result['summary'] = stripped
        elif not result['headers'] and re.match(r'^STOCK\s+', stripped):
            result['headers'] = stripped.split()
        elif result['headers'] and not stripped.startswith('-'):
            cols = stripped.split()
            if len(cols) >= 2:
                result['rows'].append(cols)
    return result


# ── Build HTML ────────────────────────────────────────────────────────────────

def build_email_html(date_str: str, time_str: str, calendar_text: str, portfolio_text: str) -> str:
    # Calendar section
    cal_events = parse_calendar_for_email(calendar_text)
    cal_parts = []

    if not cal_events:
        cal_parts.append(
            '<p style="color:#666;font-style:italic;margin:8px 0;">No events today.</p>'
        )
    else:
        for item in cal_events:
            if 'date_header' in item:
                cal_parts.append(
                    f'<p style="font-size:12px;font-weight:700;color:#1a1a2e;'
                    f'margin:16px 0 8px 0;text-transform:uppercase;'
                    f'letter-spacing:0.05em;">{item["date_header"]}</p>'
                )
            else:
                if item['detail_type'] == 'meet':
                    detail_html = (
                        f'<a href="{item["detail"]}" style="color:#4f46e5;'
                        f'font-size:12px;text-decoration:none;">Join Google Meet</a>'
                    )
                elif item['detail_type'] == 'location':
                    detail_html = (
                        f'<span style="color:#888;font-size:12px;">'
                        f'{item["detail"]}</span>'
                    )
                else:
                    detail_html = ''

                detail_row = (
                    f'<div style="margin-top:3px;">{detail_html}</div>'
                    if detail_html else ''
                )
                cal_parts.append(
                    f'<div style="display:flex;align-items:flex-start;'
                    f'margin-bottom:8px;background:#f8f9ff;border-radius:8px;'
                    f'padding:10px 14px;border-left:3px solid #4f46e5;">'
                    f'<div style="min-width:130px;font-size:12px;color:#4f46e5;'
                    f'font-weight:600;padding-top:1px;">{item["time"]}</div>'
                    f'<div>'
                    f'<div style="font-size:14px;font-weight:600;color:#1a1a2e;">'
                    f'{item["title"]}</div>'
                    f'{detail_row}'
                    f'</div></div>'
                )

    cal_section = '\n'.join(cal_parts)

    # Portfolio section
    port_data = parse_portfolio_for_email(portfolio_text)

    if port_data['headers'] and port_data['rows']:
        header_cells = ''
        for i, h in enumerate(port_data['headers']):
            align = 'left' if i == 0 else 'right'
            header_cells += (
                f'<th style="padding:8px 10px;text-align:{align};font-size:11px;'
                f'text-transform:uppercase;letter-spacing:0.05em;color:#888;'
                f'border-bottom:2px solid #e5e7eb;">{h}</th>'
            )

        row_htmls = []
        for cols in port_data['rows']:
            cells = ''
            for i, col in enumerate(cols):
                align = 'left' if i == 0 else 'right'
                if i > 0 and col.startswith('+'):
                    color = '#16a34a'
                elif i > 0 and col.startswith('-'):
                    color = '#dc2626'
                else:
                    color = '#1a1a2e'
                cells += (
                    f'<td style="padding:7px 10px;font-size:13px;'
                    f'text-align:{align};color:{color};'
                    f'border-bottom:1px solid #f0f0f0;">{col}</td>'
                )
            row_htmls.append(f'<tr>{cells}</tr>')

        summary_html = ''
        if port_data['summary']:
            summary_html = (
                f'<div style="margin-top:12px;padding:10px 14px;'
                f'background:#1a1a2e;border-radius:6px;font-size:13px;'
                f'color:#e5e7eb;font-family:monospace;">'
                f'{port_data["summary"]}</div>'
            )

        port_section = (
            f'<p style="font-size:11px;color:#888;margin:0 0 8px;">'
            f'{port_data["title"]}</p>'
            f'<table style="width:100%;border-collapse:collapse;font-family:monospace;">'
            f'<thead><tr>{header_cells}</tr></thead>'
            f'<tbody>{"".join(row_htmls)}</tbody>'
            f'</table>'
            f'{summary_html}'
        )
    else:
        port_section = (
            f'<pre style="font-family:monospace;font-size:12px;background:#f8f9ff;'
            f'padding:14px;border-radius:8px;overflow-x:auto;">'
            f'{portfolio_text}</pre>'
        )

    return f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:Arial,Helvetica,sans-serif;">
<div style="width:600px;margin:24px auto;background:#ffffff;border-radius:12px;
            overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

  <!-- Header -->
  <div style="background:#1a1a2e;padding:28px 32px;">
    <div style="font-size:22px;font-weight:700;color:#ffffff;margin-bottom:6px;">
      Morning Coffee Briefing
    </div>
    <div style="font-size:14px;color:#a5b4fc;">
      {date_str} &nbsp;|&nbsp; {time_str}
    </div>
  </div>

  <!-- Calendar Section -->
  <div style="padding:24px 32px;border-bottom:1px solid #f0f0f0;">
    <div style="font-size:11px;font-weight:700;color:#4f46e5;text-transform:uppercase;
                letter-spacing:0.1em;margin-bottom:14px;">Calendar - Today</div>
    {cal_section}
  </div>

  <!-- Portfolio Section -->
  <div style="padding:24px 32px;border-bottom:1px solid #f0f0f0;">
    <div style="font-size:11px;font-weight:700;color:#4f46e5;text-transform:uppercase;
                letter-spacing:0.1em;margin-bottom:14px;">Portfolio - Today</div>
    {port_section}
  </div>

  <!-- Footer -->
  <div style="padding:16px 32px;background:#f8f9ff;">
    <p style="font-size:11px;color:#aaa;margin:0;text-align:center;">
      Sent by Morning Coffee Agent &middot; {BRIEFING_EMAIL}
    </p>
  </div>

</div>
</body>
</html>'''


# ── Fallback Telegram text ────────────────────────────────────────────────────

def build_telegram_text_fallback(calendar_text: str, portfolio_text: str) -> str:
    now  = datetime.datetime.now()
    date = now.strftime('%A, %B %d, %Y')
    time = now.strftime('%I:%M %p')
    cal_html  = md_to_telegram_html(calendar_text) if calendar_text else 'No events today.'
    port_html = f'<code>{html_escape(portfolio_text)}</code>' if portfolio_text else 'No portfolio data.'
    return '\n'.join([
        '<b>Morning Coffee Briefing</b>',
        f'<b>{html_escape(date)}</b>  |  {html_escape(time)} PHT',
        '',
        '<b>CALENDAR - TODAY</b>',
        cal_html,
        '',
        '<b>PORTFOLIO - TODAY</b>',
        port_html,
    ])


# ── Send HTML email via Gmail ─────────────────────────────────────────────────

def send_email_briefing(html_body: str, now: datetime.datetime) -> bool:
    """Send the HTML briefing via Gmail API. Returns True on success."""
    shared_dir = REPO_ROOT / 'shared'
    if str(shared_dir) not in sys.path:
        sys.path.insert(0, str(shared_dir))

    try:
        from google_auth import get_credentials
    except ImportError as e:
        print(f"  [Email] google_auth not available: {e}")
        traceback.print_exc()
        return False

    try:
        from googleapiclient.discovery import build as gbuild
    except ImportError:
        print("  [Email] Installing google-api-python-client...")
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'google-api-python-client'])
        from googleapiclient.discovery import build as gbuild

    try:
        creds   = get_credentials()
        service = gbuild('gmail', 'v1', credentials=creds)
    except Exception as e:
        print(f"  [Email] Auth failed: {e}")
        traceback.print_exc()
        return False

    subject = f"Morning Coffee Briefing - {now.strftime('%b %d, %Y')}"
    message_parts = [
        f'From: {BRIEFING_EMAIL}',
        f'To: {BRIEFING_EMAIL}',
        f'Subject: {subject}',
        'MIME-Version: 1.0',
        'Content-Type: text/html; charset="UTF-8"',
        '',
        html_body,
    ]
    raw = base64.urlsafe_b64encode(
        '\r\n'.join(message_parts).encode('utf-8')
    ).decode().rstrip('=')

    def _send():
        return service.users().messages().send(userId='me', body={'raw': raw}).execute()

    if _retry(_send, "Email") is not None:
        print(f"  [Email] Briefing sent to {BRIEFING_EMAIL}")
        return True
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    load_env()
    failures = []

    print("Fetching today's calendar...")
    cal_raw  = run_silent(GCAL_SCRIPT, ['today'])
    cal_text = clean_output(cal_raw)

    print("Fetching portfolio status...")
    port_text = fetch_portfolio_table()

    # Build HTML once — shared between screenshot and email
    now      = datetime.datetime.now()
    date_str = now.strftime('%A, %B %d, %Y')
    time_str = now.strftime('%I:%M %p PHT')
    html     = build_email_html(date_str, time_str, cal_text, port_text)

    # Render screenshot and send to Telegram
    print("\nRendering screenshot for Telegram...")
    img_bytes = render_briefing_screenshot(html)

    print("Sending to Telegram...")
    if img_bytes:
        caption = f"Morning Coffee - {now.strftime('%b %d, %Y')}"
        telegram_ok = send_telegram_photo(img_bytes, caption)
    else:
        print("  Falling back to text...")
        telegram_ok = send_telegram_text(build_telegram_text_fallback(cal_text, port_text))

    if not telegram_ok:
        failures.append("Telegram send failed")

    # Send HTML email
    print("\nSending email briefing...")
    if not send_email_briefing(html, now):
        failures.append("Email send failed")

    if failures:
        print("\n=== RUN FAILED ===")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    print("\n=== RUN OK ===")


if __name__ == '__main__':
    main()
