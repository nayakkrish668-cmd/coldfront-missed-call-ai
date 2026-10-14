"""
Missed-Call AI Responder — starter template
---------------------------------------------
What this does:
1. Someone calls your Twilio number -> we try to forward the call to the
   real business phone (FORWARD_TO).
2. If it rings out / goes unanswered / busy, Twilio hits our /voice/status
   webhook, and we immediately text the caller so the lead isn't lost.
3. If the caller texts back (or texts first), /sms uses an LLM to carry on
   a short, helpful conversation and try to capture what they need /
   offer to book an appointment.
4. Every call + text event is logged to events.log so you can show a
   prospect real evidence of what the system caught.

This is a demo-grade template, not a production system — see README.md
for what to harden before using it with a real client's phone number.
"""

import os
import sqlite3
import datetime
import logging
from contextlib import closing
from functools import wraps
from flask import Flask, request, Response, abort
from twilio.twiml.voice_response import VoiceResponse, Dial, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from anthropic import Anthropic

app = Flask(__name__)

# ---- Config (set these as environment variables, see .env.example) ----
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "")  # the number you bought, e.g. +15551234567
FORWARD_TO = os.environ.get("FORWARD_TO", "")  # the business owner's real phone, e.g. +15559876543
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")  # e.g. https://your-app.onrender.com

BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "ABC Home Services")
BUSINESS_TRADE = os.environ.get("BUSINESS_TRADE", "HVAC repair and installation")
RING_TIMEOUT_SECONDS = int(os.environ.get("RING_TIMEOUT_SECONDS", "18"))

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
ai_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
request_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

logging.basicConfig(
    filename="events.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)

# ---------------------------------------------------------------------
# Storage: SQLite instead of in-memory dicts, so consent records and
# conversation history survive a worker restart (not just a live process).
#
# NOTE: on Render's free tier (no attached persistent disk), this file
# still lives on ephemeral local storage and is wiped on a fresh deploy
# or a dyno-level rebuild — it is NOT a substitute for a managed database
# like Postgres if you need records to survive indefinitely. Attach a
# Render disk (or move to Postgres) before this matters for real audits.
# ---------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_consent (
                phone TEXT PRIMARY KEY,
                consented INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


init_db()


def set_consent(phone: str, consented: bool):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO sms_consent (phone, consented, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET consented=excluded.consented, updated_at=excluded.updated_at",
            (phone, int(consented), datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


def has_consent(phone: str) -> bool:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT consented FROM sms_consent WHERE phone = ?", (phone,)).fetchone()
        return bool(row["consented"]) if row else False


def get_history(phone: str) -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE phone = ? ORDER BY id ASC", (phone,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]


def append_message(phone: str, role: str, content: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO messages (phone, role, content, created_at) VALUES (?, ?, ?, ?)",
            (phone, role, content, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


def validate_twilio_request(f):
    """Reject any request to a Twilio webhook that isn't actually signed by
    Twilio. Without this, anyone who finds the URL can POST fake call/SMS
    events and trigger real text sends on your Twilio bill."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if request_validator is None:
            log_event("twilio_signature_check_skipped_no_auth_token")
            abort(403)

        signature = request.headers.get("X-Twilio-Signature", "")
        query_string = request.query_string.decode()
        url = f"{PUBLIC_BASE_URL.rstrip('/')}{request.path}"
        if query_string:
            url = f"{url}?{query_string}"

        if not request_validator.validate(url, request.form, signature):
            log_event(
                "twilio_signature_invalid",
                path=request.path,
                remote_addr=request.remote_addr,
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated


OPT_IN_PROMPT = (
    f"Thanks for calling {BUSINESS_NAME}. Press 1 to opt in to receive a "
    "follow-up text message if we're unable to answer your call right now. "
    "Message and data rates may apply."
)

SYSTEM_PROMPT = f"""You are the friendly, efficient text-message assistant for \
{BUSINESS_NAME}, a {BUSINESS_TRADE} business. A customer just texted in, likely \
after a call wasn't answered. Your job:
- Greet them briefly and find out what they need (repair, quote, emergency, etc.)
- Answer simple questions (you don't know exact pricing — say a technician will \
confirm pricing on the visit)
- Try to get: their name, a short description of the issue, and a preferred day/time
- Once you have those, confirm you're passing it to the team to schedule, and say \
someone will follow up shortly to finalize the appointment
- Keep every reply SHORT (1-3 sentences) — this is a text conversation, not an email
- If it sounds like a true emergency (gas leak, no heat in freezing weather, water \
flooding), tell them to call ###-###-#### directly right now instead of texting
Never make up a price, availability, or promise a specific technician."""


def log_event(kind: str, **fields):
    logging.info("%s | %s", kind, fields)


def send_sms(to: str, body: str):
    if not twilio_client:
        print(f"[DRY RUN] Would text {to}: {body}")
        return
    twilio_client.messages.create(to=to, from_=TWILIO_NUMBER, body=body)


def ai_reply(caller: str, incoming_text: str) -> str:
    append_message(caller, "user", incoming_text)
    history = get_history(caller)

    if ai_client:
        resp = ai_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        reply_text = resp.content[0].text
    else:
        # Fallback so the demo still works without an API key configured yet.
        reply_text = (
            "Thanks for reaching out! What's going on with your system, and "
            "what day works best for a visit? (AI replies aren't configured yet "
            "— set ANTHROPIC_API_KEY to enable real responses.)"
        )

    append_message(caller, "assistant", reply_text)
    return reply_text


# ---------------------------------------------------------------------
# Voice: incoming call -> try to forward it to the real business line
# ---------------------------------------------------------------------
@app.route("/voice", methods=["POST"])
@validate_twilio_request
def voice():
    caller = request.form.get("From", "unknown")
    log_event("incoming_call", caller=caller)

    # Explicit opt-in: caller must press 1 before we'll text them later if
    # the call goes unanswered. This is required for A2P 10DLC compliance —
    # "by continuing the call" is not an explicit consent action on its own.
    vr = VoiceResponse()
    gather = Gather(
        num_digits=1,
        timeout=6,
        action=f"{PUBLIC_BASE_URL}/voice/opt-in",
        method="POST",
        action_on_empty_result=True,
    )
    gather.say(OPT_IN_PROMPT)
    vr.append(gather)
    # Safety net in case Gather's action callback itself never fires.
    vr.redirect(f"{PUBLIC_BASE_URL}/voice/opt-in")
    return Response(str(vr), mimetype="text/xml")


@app.route("/voice/opt-in", methods=["POST"])
@validate_twilio_request
def voice_opt_in():
    caller = request.form.get("From", "unknown")
    digits = request.form.get("Digits", "")
    consented = digits.strip() == "1"
    set_consent(caller, consented)
    log_event("sms_opt_in", caller=caller, consented=consented, digits_received=digits)

    vr = VoiceResponse()
    if FORWARD_TO:
        dial = Dial(
            timeout=RING_TIMEOUT_SECONDS,
            action=f"{PUBLIC_BASE_URL}/voice/status",
            method="POST",
        )
        dial.number(FORWARD_TO)
        vr.append(dial)
    else:
        # No forwarding number configured — go straight to voicemail-style message.
        vr.say("Thanks for calling. We can't take your call right now.")
        vr.redirect(f"{PUBLIC_BASE_URL}/voice/status?DialCallStatus=no-answer")
    return Response(str(vr), mimetype="text/xml")


# ---------------------------------------------------------------------
# Voice status callback: fires after the Dial attempt finishes.
# If it wasn't answered, text the caller immediately.
# ---------------------------------------------------------------------
@app.route("/voice/status", methods=["POST"])
@validate_twilio_request
def voice_status():
    caller = request.form.get("From", "unknown")
    call_status = request.form.get("DialCallStatus", "unknown")
    log_event("call_status", caller=caller, status=call_status)

    vr = VoiceResponse()
    if call_status in ("no-answer", "busy", "failed", "canceled"):
        if has_consent(caller):
            body = (
                f"Hey! Sorry we missed your call at {BUSINESS_NAME}. "
                f"What can we help with? (Reply here and we'll get right back to you.)"
            )
            send_sms(caller, body)
            log_event("missed_call_text_sent", caller=caller)
        else:
            log_event("missed_call_no_consent_skipped", caller=caller)
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


# ---------------------------------------------------------------------
# SMS: incoming text -> AI reply -> text back
# ---------------------------------------------------------------------
@app.route("/sms", methods=["POST"])
@validate_twilio_request
def sms():
    caller = request.form.get("From", "unknown")
    body = request.form.get("Body", "")
    log_event("incoming_sms", caller=caller, body=body)

    reply_text = ai_reply(caller, body)
    log_event("ai_reply_sent", caller=caller, reply=reply_text)

    resp = MessagingResponse()
    resp.message(reply_text)
    return Response(str(resp), mimetype="text/xml")


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "business": BUSINESS_NAME, "time": str(datetime.datetime.now())}


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
