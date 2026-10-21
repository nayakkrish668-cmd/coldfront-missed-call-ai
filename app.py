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

Also included: a standalone LIVE VOICE DEMO (/voice/demo) a prospect can
call and actually talk to, out loud, in real time. It's a separate code
path from the real missed-call-text-back product above, needs no SMS/A2P
approval (voice calls aren't gated by 10DLC), and supports two framings
picked via a ?mode= query param on the Twilio webhook URL:
  - /voice/demo?mode=lead   -> "never miss a job" lead-capture receptionist
  - /voice/demo?mode=screen -> "stop wasting time on sales calls" gatekeeper
Point any Twilio number's "A call comes in" webhook at one of those two
URLs to turn the demo on for that number. Each call's conversation is
kept in memory only (not the SQLite tables used by the real product) and
is capped at a few turns so a stray demo call can't run up an API bill.
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
from twilio.base.exceptions import TwilioException
from anthropic import Anthropic, APIError

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

# Number the AI tells a texting customer to call for a true emergency.
# Defaults to the business's own Twilio number (which already forwards to
# FORWARD_TO) so there's never a placeholder like ###-###-#### in a live
# reply; override with EMERGENCY_PHONE if it should ring somewhere else.
EMERGENCY_PHONE = os.environ.get("EMERGENCY_PHONE", "") or TWILIO_NUMBER

# Shared secret for the /admin/clients onboarding page below. Set this on
# Render (same as the other env vars) — the page is unreachable without it.
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

# These env vars describe ONE business — kept as the fallback "default
# client" so the very first deployment needs zero database rows. Onboarding
# client #2 and beyond is done through /admin/clients instead (see below),
# so adding a new client never requires editing code or redeploying.
DEFAULT_CLIENT = {
    "twilio_number": TWILIO_NUMBER,
    "business_name": BUSINESS_NAME,
    "business_trade": BUSINESS_TRADE,
    "forward_to": FORWARD_TO,
    "emergency_phone": EMERGENCY_PHONE,
    "ring_timeout_seconds": RING_TIMEOUT_SECONDS,
}


def _format_phone_for_display(e164: str) -> str:
    """+17068082127 -> (706) 808-2127. Falls back to the raw value for
    anything that isn't a plain 11-digit US/Canada number."""
    digits = e164.lstrip("+")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:]}"
    return e164 or "our main number"

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
ai_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
request_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)  # stdout, so Render's log dashboard actually shows these events

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_notifications (
                phone TEXT PRIMARY KEY,
                notified_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                twilio_number TEXT PRIMARY KEY,
                business_name TEXT NOT NULL,
                business_trade TEXT NOT NULL,
                forward_to TEXT NOT NULL,
                emergency_phone TEXT,
                ring_timeout_seconds INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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


def already_notified_owner(phone: str) -> bool:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM lead_notifications WHERE phone = ?", (phone,)
        ).fetchone()
        return row is not None


def mark_owner_notified(phone: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO lead_notifications (phone, notified_at) VALUES (?, ?)",
            (phone, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


# ---------------------------------------------------------------------
# Per-client config. Each real client gets their own Twilio number; the
# "To" field Twilio sends on every webhook (call or text) IS that number,
# so it doubles as the lookup key for which business's name/trade/forward
# number/emergency number to use on that request. Onboarding client #2+ is
# then just a row in this table via /admin/clients below — no code change,
# no redeploy. If no row matches, we fall back to DEFAULT_CLIENT (the
# env-var-configured business), so the current single-client setup keeps
# working untouched.
# ---------------------------------------------------------------------
def get_client_row(twilio_number: str):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE twilio_number = ?", (twilio_number,)
        ).fetchone()
        return dict(row) if row else None


def list_clients() -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM clients ORDER BY business_name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_client(
    twilio_number: str,
    business_name: str,
    business_trade: str,
    forward_to: str,
    emergency_phone: str | None = None,
    ring_timeout_seconds: int | None = None,
):
    now = datetime.datetime.utcnow().isoformat()
    with closing(get_db()) as conn:
        conn.execute(
            """
            INSERT INTO clients
                (twilio_number, business_name, business_trade, forward_to,
                 emergency_phone, ring_timeout_seconds, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(twilio_number) DO UPDATE SET
                business_name=excluded.business_name,
                business_trade=excluded.business_trade,
                forward_to=excluded.forward_to,
                emergency_phone=excluded.emergency_phone,
                ring_timeout_seconds=excluded.ring_timeout_seconds,
                updated_at=excluded.updated_at
            """,
            (
                twilio_number,
                business_name,
                business_trade,
                forward_to,
                emergency_phone,
                ring_timeout_seconds,
                now,
                now,
            ),
        )
        conn.commit()


def delete_client(twilio_number: str):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM clients WHERE twilio_number = ?", (twilio_number,))
        conn.commit()


def resolve_client(to_number: str) -> dict:
    row = get_client_row(to_number) if to_number else None
    if not row:
        return DEFAULT_CLIENT
    return {
        "twilio_number": row["twilio_number"],
        "business_name": row["business_name"],
        "business_trade": row["business_trade"],
        "forward_to": row["forward_to"],
        "emergency_phone": row["emergency_phone"] or row["twilio_number"],
        "ring_timeout_seconds": row["ring_timeout_seconds"] or RING_TIMEOUT_SECONDS,
    }


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


def build_opt_in_prompt(client: dict) -> str:
    return (
        f"Thanks for calling {client['business_name']}. Press 1 to opt in to receive a "
        "follow-up text message if we're unable to answer your call right now. "
        "Message and data rates may apply."
    )


def build_system_prompt(client: dict) -> str:
    return f"""You are the friendly, efficient text-message assistant for \
{client['business_name']}, a {client['business_trade']} business. A customer just texted in, likely \
after a call wasn't answered. Your job:
- Greet them briefly and find out what they need (repair, quote, emergency, etc.)
- Answer simple questions (you don't know exact pricing — say a technician will \
confirm pricing on the visit)
- Try to get: their name, a short description of the issue, and a preferred day/time
- Once you have those three things, call the capture_lead tool so the business \
owner is notified right away — do this in the SAME turn as your reply to the \
customer, not instead of it. Call it only once per conversation, and only once \
you actually have real values (never call it with guessed or placeholder info).
- Still send the customer a short reply in every turn, confirming you're passing \
it to the team to schedule and that someone will follow up shortly
- Keep every reply SHORT (1-3 sentences) — this is a text conversation, not an email
- If it sounds like a true emergency (gas leak, no heat in freezing weather, water \
flooding), tell them to call {_format_phone_for_display(client['emergency_phone'])} directly \
right now instead of texting
Never make up a price, availability, or promise a specific technician."""

CAPTURE_LEAD_TOOL = {
    "name": "capture_lead",
    "description": (
        "Call this once — and only once — you have the customer's name, a "
        "description of what they need, and a preferred day/time for the visit. "
        "This immediately notifies the business owner with the lead details. "
        "Don't call it with guessed, placeholder, or incomplete values."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string", "description": "The customer's name"},
            "issue_description": {
                "type": "string",
                "description": "Short description of what the customer needs",
            },
            "preferred_time": {
                "type": "string",
                "description": "The day/time the customer said works for them",
            },
            "urgency": {
                "type": "string",
                "enum": ["normal", "emergency"],
                "description": "'emergency' only if the customer described a true emergency",
            },
        },
        "required": ["customer_name", "issue_description", "preferred_time", "urgency"],
    },
}


def log_event(kind: str, **fields):
    logging.info("%s | %s", kind, fields)


def send_sms(to: str, body: str, from_number: str):
    if not twilio_client:
        print(f"[DRY RUN] Would text {to} from {from_number}: {body}")
        return
    twilio_client.messages.create(to=to, from_=from_number, body=body)


def notify_owner_of_lead(caller: str, lead: dict, client: dict):
    """Text the client's forward_to number a summary of a captured lead, at
    most once per phone number. This is the thing that actually gets a real
    business owner's attention — without it, a captured lead just sits in a
    SQLite table nobody is looking at."""
    if already_notified_owner(caller):
        return
    forward_to = client["forward_to"]
    if not forward_to:
        log_event("lead_captured_no_forward_to", caller=caller, lead=lead, business=client["business_name"])
        mark_owner_notified(caller)
        return

    urgency = lead.get("urgency", "normal")
    tag = "🚨 EMERGENCY LEAD" if urgency == "emergency" else "🔔 New lead"
    body = (
        f"{tag} via AI text-back ({client['business_name']})\n"
        f"From: {_format_phone_for_display(caller)}\n"
        f"Name: {lead.get('customer_name', '—')}\n"
        f"Needs: {lead.get('issue_description', '—')}\n"
        f"Preferred time: {lead.get('preferred_time', '—')}"
    )
    try:
        send_sms(forward_to, body, client["twilio_number"])
        log_event("owner_notified", caller=caller, lead=lead)
    except TwilioException as exc:
        # Never let a failed owner-notification crash the customer's reply —
        # the customer still needs their text even if this alert fails.
        log_event("owner_notify_failed", caller=caller, error=str(exc))
    mark_owner_notified(caller)


def ai_reply(caller: str, incoming_text: str, client: dict) -> str:
    append_message(caller, "user", incoming_text)
    history = get_history(caller)

    if ai_client:
        try:
            resp = ai_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                system=build_system_prompt(client),
                tools=[CAPTURE_LEAD_TOOL],
                messages=history,
            )
            reply_text = ""
            for block in resp.content:
                if block.type == "text":
                    reply_text += block.text
                elif block.type == "tool_use" and block.name == "capture_lead":
                    notify_owner_of_lead(caller, block.input, client)
            reply_text = reply_text.strip() or (
                "Thanks — I've got what I need and I'm passing it to the team now!"
            )
        except APIError as exc:
            # Anthropic being down, rate-limited, or out of credits must
            # never surface as a raw 500 — that's what gets a real customer
            # "application error occurred" mid-conversation. Log it and
            # still text back something useful.
            log_event("ai_reply_failed", caller=caller, error=str(exc))
            reply_text = (
                "Thanks for your message! We're having a brief technical "
                "hiccup on our end — someone from our team will follow up "
                "with you shortly."
            )
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
# LIVE VOICE DEMO — a prospect calls in and talks to the AI out loud,
# in real time. Separate from the real product's SQLite-backed history;
# kept in a plain in-memory dict since a demo call never needs to survive
# a worker restart, and we don't want prospect demo chatter mixed into
# the real customer consent/message tables above.
# ---------------------------------------------------------------------
MAX_DEMO_TURNS = 6  # caps a stray/looping call so it can't run up the API bill

_demo_sessions: dict[str, list[dict]] = {}
_demo_empty_streak: dict[str, int] = {}

DEMO_GREETINGS = {
    "lead": (
        f"Hi! This is a live demo of {BUSINESS_NAME}'s AI phone receptionist, "
        "built by ColdFront Studio. Go ahead and talk to me like you're a "
        "customer calling in — what can I help you with today?"
    ),
    "screen": (
        "Hi, thanks for calling! Before I put you through, can I ask — "
        "what's the purpose of your call today?"
    ),
}

DEMO_SYSTEM_PROMPTS = {
    "lead": f"""You are a LIVE VOICE DEMO of an AI phone receptionist for \
{BUSINESS_TRADE} businesses, built by ColdFront Studio. The person on the \
line is a prospective customer trying the demo out loud over a real phone \
call, not an actual customer needing service — but play it straight, as if \
you were answering a real call for a {BUSINESS_TRADE} business, so they can \
hear how it would actually sound.
- Greet-and-ask has already happened; respond to what they say next
- Ask what they need (repair, quote, emergency, etc.), as a real receptionist would
- Try to gather: their name, a short description of the issue, and a preferred day/time
- Keep every reply SHORT and natural (1-2 sentences) — this is spoken out loud, not read
- If they ask what this is or how it works, briefly explain: this is a live demo of an \
AI receptionist that answers calls and can also text back anyone who calls when the \
business can't pick up
- Never make up a price, availability, or promise a specific technician
- If they say goodbye, thanks, or that's all, end warmly in one short sentence""",
    "screen": """You are a LIVE VOICE DEMO of an AI call-screening receptionist, built \
by ColdFront Studio, showing how it protects a busy small-business owner from unwanted \
sales and spam calls. The person on the line is a prospect trying the demo out loud over \
a real phone call. Play it straight, as the AI gatekeeper really would:
- You've already asked the purpose of their call; respond to what they say next
- If they say they're selling something, a vendor, marketing, or a cold call: politely \
decline to transfer them, explain the owner isn't taking sales calls right now, and offer \
to take a short message instead
- If they say they're a customer needing service, or anything that's clearly not a sales \
pitch: respond warmly and say you'd transfer them right through (for the demo, just say \
what you WOULD do, don't actually try to transfer)
- Keep every reply SHORT and natural (1-2 sentences) — this is spoken out loud, not read
- If they ask what this is or how it works, briefly explain: this screens incoming calls \
so the owner only gets interrupted for real customers, not sales pitches
- If they say goodbye, thanks, or that's all, end warmly in one short sentence""",
}

DEMO_CLOSING_LINE = (
    "That's the demo! Thanks for trying it out — have a great day."
)


def _demo_mode() -> str:
    mode = request.args.get("mode", "lead").strip().lower()
    return mode if mode in DEMO_SYSTEM_PROMPTS else "lead"


def _demo_ai_reply(call_sid: str, mode: str, caller_said: str) -> str:
    history = _demo_sessions.setdefault(call_sid, [])
    history.append({"role": "user", "content": caller_said})

    if ai_client:
        try:
            resp = ai_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=120,
                system=DEMO_SYSTEM_PROMPTS[mode],
                messages=history,
            )
            reply_text = resp.content[0].text
        except APIError as exc:
            # A crash here would bring down a live voice call mid-sentence —
            # worse than a bad reply, since the caller is on the line right now.
            log_event("demo_ai_reply_failed", call_sid=call_sid, error=str(exc))
            reply_text = (
                "Sorry, I'm having a brief technical hiccup — but that's the "
                "idea: this demo answers calls and texts back anyone we miss."
            )
    else:
        reply_text = (
            "Thanks! I'd note that down for a real call, but the AI isn't fully "
            "configured yet on this demo — ANTHROPIC_API_KEY needs to be set."
        )

    history.append({"role": "assistant", "content": reply_text})
    return reply_text


def _demo_gather(mode: str) -> Gather:
    return Gather(
        input="speech",
        speech_timeout="auto",
        action=f"{PUBLIC_BASE_URL}/voice/demo/respond?mode={mode}",
        method="POST",
        action_on_empty_result=True,
    )


@app.route("/voice/demo", methods=["POST"])
@validate_twilio_request
def voice_demo():
    call_sid = request.form.get("CallSid", "unknown")
    mode = _demo_mode()
    log_event("demo_call_started", call_sid=call_sid, mode=mode)

    _demo_sessions[call_sid] = []
    _demo_empty_streak[call_sid] = 0

    vr = VoiceResponse()
    gather = _demo_gather(mode)
    gather.say(DEMO_GREETINGS[mode])
    vr.append(gather)
    # If Gather's own action callback never fires, don't just hang silently.
    vr.say("Didn't catch that — thanks for trying the demo, bye for now!")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/voice/demo/respond", methods=["POST"])
@validate_twilio_request
def voice_demo_respond():
    call_sid = request.form.get("CallSid", "unknown")
    mode = _demo_mode()
    caller_said = request.form.get("SpeechResult", "").strip()
    turns_so_far = len(_demo_sessions.get(call_sid, []))

    vr = VoiceResponse()

    if not caller_said:
        streak = _demo_empty_streak.get(call_sid, 0) + 1
        _demo_empty_streak[call_sid] = streak
        if streak >= 2:
            log_event("demo_call_ended_silence", call_sid=call_sid)
            vr.say(DEMO_CLOSING_LINE)
            vr.hangup()
        else:
            gather = _demo_gather(mode)
            gather.say("Sorry, I didn't catch that — go ahead, I'm listening.")
            vr.append(gather)
            vr.say(DEMO_CLOSING_LINE)
            vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    _demo_empty_streak[call_sid] = 0
    log_event("demo_caller_said", call_sid=call_sid, mode=mode, text=caller_said)

    sign_off_words = ("bye", "goodbye", "that's all", "thats all", "no thanks", "nothing else")
    wants_to_end = any(w in caller_said.lower() for w in sign_off_words)

    if turns_so_far >= MAX_DEMO_TURNS * 2 or wants_to_end:
        if not wants_to_end:
            reply_text = _demo_ai_reply(call_sid, mode, caller_said)
            vr.say(reply_text)
        vr.say(DEMO_CLOSING_LINE)
        vr.hangup()
        log_event("demo_call_ended", call_sid=call_sid, reason="sign_off_or_cap")
        _demo_sessions.pop(call_sid, None)
        _demo_empty_streak.pop(call_sid, None)
        return Response(str(vr), mimetype="text/xml")

    reply_text = _demo_ai_reply(call_sid, mode, caller_said)
    log_event("demo_ai_reply", call_sid=call_sid, reply=reply_text)

    gather = _demo_gather(mode)
    gather.say(reply_text)
    vr.append(gather)
    vr.say(DEMO_CLOSING_LINE)
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


# ---------------------------------------------------------------------
# Voice: incoming call -> try to forward it to the real business line
# ---------------------------------------------------------------------
@app.route("/voice", methods=["POST"])
@validate_twilio_request
def voice():
    caller = request.form.get("From", "unknown")
    to_number = request.form.get("To", "")
    client = resolve_client(to_number)
    log_event("incoming_call", caller=caller, business=client["business_name"])

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
    gather.say(build_opt_in_prompt(client))
    vr.append(gather)
    # Safety net in case Gather's action callback itself never fires.
    vr.redirect(f"{PUBLIC_BASE_URL}/voice/opt-in")
    return Response(str(vr), mimetype="text/xml")


@app.route("/voice/opt-in", methods=["POST"])
@validate_twilio_request
def voice_opt_in():
    caller = request.form.get("From", "unknown")
    to_number = request.form.get("To", "")
    client = resolve_client(to_number)
    digits = request.form.get("Digits", "")
    consented = digits.strip() == "1"
    set_consent(caller, consented)
    log_event("sms_opt_in", caller=caller, consented=consented, digits_received=digits)

    vr = VoiceResponse()
    if client["forward_to"]:
        dial = Dial(
            timeout=client["ring_timeout_seconds"],
            action=f"{PUBLIC_BASE_URL}/voice/status",
            method="POST",
        )
        dial.number(client["forward_to"])
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
    to_number = request.values.get("To", "")
    client = resolve_client(to_number)
    # DialCallStatus normally arrives as a POST form field (Twilio's own
    # <Dial action=...> callback does this). But the no-FORWARD_TO fallback
    # above redirects here with it as a URL query param instead, which
    # request.form can't see — use request.values so both paths work.
    call_status = request.values.get("DialCallStatus", "unknown")
    log_event("call_status", caller=caller, status=call_status, business=client["business_name"])

    vr = VoiceResponse()
    if call_status in ("no-answer", "busy", "failed", "canceled"):
        if has_consent(caller):
            body = (
                f"Hey! Sorry we missed your call at {client['business_name']}. "
                f"What can we help with? (Reply here and we'll get right back to you.)"
            )
            send_sms(caller, body, client["twilio_number"])
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
    to_number = request.form.get("To", "")
    client = resolve_client(to_number)
    body = request.form.get("Body", "")
    log_event("incoming_sms", caller=caller, body=body, business=client["business_name"])

    reply_text = ai_reply(caller, body, client)
    log_event("ai_reply_sent", caller=caller, reply=reply_text)

    resp = MessagingResponse()
    resp.message(reply_text)
    return Response(str(resp), mimetype="text/xml")


# ---------------------------------------------------------------------
# Admin: onboard/update/remove a client without touching code or env vars.
# Protected by a shared secret (ADMIN_SECRET) passed as ?secret=... — not
# enterprise auth, but enough that the URL isn't guessable, which is the
# right bar for a solo-operator admin page.
# ---------------------------------------------------------------------
def _check_admin_secret():
    secret = request.args.get("secret") or request.form.get("secret") or request.headers.get("X-Admin-Secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        abort(403)


@app.route("/admin/clients", methods=["GET", "POST"])
def admin_clients():
    _check_admin_secret()
    secret = request.args.get("secret") or request.form.get("secret")
    message = ""

    if request.method == "POST":
        action = request.form.get("action", "save")
        twilio_number = request.form.get("twilio_number", "").strip()

        if action == "delete":
            if twilio_number:
                delete_client(twilio_number)
                message = f"Removed {twilio_number}."
        else:
            business_name = request.form.get("business_name", "").strip()
            business_trade = request.form.get("business_trade", "").strip()
            forward_to = request.form.get("forward_to", "").strip()
            emergency_phone = request.form.get("emergency_phone", "").strip() or None
            ring_timeout_raw = request.form.get("ring_timeout_seconds", "").strip()
            ring_timeout_seconds = int(ring_timeout_raw) if ring_timeout_raw.isdigit() else None

            if not (twilio_number and business_name and business_trade and forward_to):
                message = "Twilio number, business name, trade, and forward-to number are all required."
            else:
                upsert_client(
                    twilio_number, business_name, business_trade, forward_to,
                    emergency_phone, ring_timeout_seconds,
                )
                message = f"Saved {business_name} ({twilio_number}). Point that number's " \
                          f"Twilio webhooks at this app and it's live — no deploy needed."

    clients = list_clients()
    if clients:
        rows_html = "".join(
            f"""<tr>
                <td>{c['business_name']}</td><td>{c['twilio_number']}</td>
                <td>{c['business_trade']}</td><td>{c['forward_to']}</td>
                <td>{c['emergency_phone'] or '—'}</td>
                <td><form method="POST" style="display:inline" onsubmit="return confirm('Remove {c['business_name']}?')">
                    <input type="hidden" name="secret" value="{secret}">
                    <input type="hidden" name="action" value="delete">
                    <input type="hidden" name="twilio_number" value="{c['twilio_number']}">
                    <button type="submit">Remove</button></form></td>
            </tr>"""
            for c in clients
        )
    else:
        rows_html = (
            f"<tr><td colspan='6'>No per-client rows yet — "
            f"{DEFAULT_CLIENT['business_name']} ({DEFAULT_CLIENT['twilio_number'] or 'TWILIO_NUMBER not set'}) "
            f"is running on the built-in default config from env vars.</td></tr>"
        )

    return f"""<html><head><title>Clients — admin</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
table{{width:100%;border-collapse:collapse;margin-bottom:2rem;font-size:.85rem}}
td,th{{border:1px solid #ddd;padding:.5rem .6rem;text-align:left}}
th{{background:#f5f5f5}}
label{{display:block;margin-top:.8rem;font-weight:600;font-size:.9rem}}
input{{width:100%;padding:.5rem;box-sizing:border-box;margin-top:.2rem}}
button{{margin-top:1rem;padding:.6rem 1.2rem;cursor:pointer}}
.msg{{background:#e6f4ea;padding:.6rem .8rem;border-radius:4px;margin-bottom:1rem}}
</style></head><body>
<h1>Clients</h1>
{'<p class="msg">' + message + '</p>' if message else ''}
<table><tr><th>Business</th><th>Twilio #</th><th>Trade</th><th>Forward to</th><th>Emergency #</th><th></th></tr>
{rows_html}
</table>
<h2>Add / update a client</h2>
<form method="POST">
<input type="hidden" name="secret" value="{secret}">
<label>Twilio number (E.164, e.g. +17065551234) — the number this client's customers call/text</label>
<input name="twilio_number" required>
<label>Business name</label>
<input name="business_name" required>
<label>Trade (e.g. "plumbing and drain cleaning")</label>
<input name="business_trade" required>
<label>Forward-to number (the owner's real phone, E.164)</label>
<input name="forward_to" required>
<label>Emergency phone (optional — defaults to the Twilio number above)</label>
<input name="emergency_phone">
<label>Ring timeout, seconds (optional — defaults to {RING_TIMEOUT_SECONDS})</label>
<input name="ring_timeout_seconds">
<button type="submit">Save client</button>
</form>
</body></html>"""


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "business": BUSINESS_NAME, "time": str(datetime.datetime.now())}


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
