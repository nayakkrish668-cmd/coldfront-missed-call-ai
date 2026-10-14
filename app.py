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
import datetime
import logging
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Dial, Gather
from twilio.twiml.messaging_response import MessagingResponse
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

logging.basicConfig(
    filename="events.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)

# In-memory store of conversation history per caller, for this demo.
# A real deployment would use a database (SQLite/Postgres) instead.
CONVERSATIONS: dict[str, list[dict]] = {}

# In-memory store of whether a caller pressed 1 to opt in to SMS follow-up,
# keyed by their phone number. A real deployment would persist this
# (SQLite/Postgres) rather than lose it on restart.
SMS_CONSENT: dict[str, bool] = {}

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
    history = CONVERSATIONS.setdefault(caller, [])
    history.append({"role": "user", "content": incoming_text})

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

    history.append({"role": "assistant", "content": reply_text})
    return reply_text


# ---------------------------------------------------------------------
# Voice: incoming call -> try to forward it to the real business line
# ---------------------------------------------------------------------
@app.route("/voice", methods=["POST"])
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
def voice_opt_in():
    caller = request.form.get("From", "unknown")
    digits = request.form.get("Digits", "")
    consented = digits.strip() == "1"
    SMS_CONSENT[caller] = consented
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
def voice_status():
    caller = request.form.get("From", "unknown")
    call_status = request.form.get("DialCallStatus", "unknown")
    log_event("call_status", caller=caller, status=call_status)

    vr = VoiceResponse()
    if call_status in ("no-answer", "busy", "failed", "canceled"):
        if SMS_CONSENT.get(caller):
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
