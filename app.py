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
import re
import time
import sqlite3
import datetime
import logging
from contextlib import closing
from functools import wraps
from flask import Flask, request, Response, abort
from werkzeug.security import generate_password_hash, check_password_hash
from twilio.twiml.voice_response import VoiceResponse, Dial, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.base.exceptions import TwilioException
from anthropic import Anthropic, APIError
import stripe

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

# How long a captured-lead notification to the owner "counts" before the
# same phone number can trigger another one. This stops one conversation's
# back-and-forth from texting the owner repeatedly, WITHOUT permanently
# silencing that customer — after this window a new issue from them still
# gets through. 24h comfortably covers a single conversation thread.
LEAD_DEDUPE_HOURS = int(os.environ.get("LEAD_DEDUPE_HOURS", "24"))

# Number the AI tells a texting customer to call for a true emergency.
# Defaults to the business's own Twilio number (which already forwards to
# FORWARD_TO) so there's never a placeholder like ###-###-#### in a live
# reply; override with EMERGENCY_PHONE if it should ring somewhere else.
EMERGENCY_PHONE = os.environ.get("EMERGENCY_PHONE", "") or TWILIO_NUMBER

# Login for the /admin pages below (HTTP Basic Auth — your browser will
# prompt for these, nothing to paste into a URL). These two env vars only
# seed the FIRST admin account, on first run — after that, admin accounts
# (there can be more than one) live in the admin_users table below, managed
# from /admin/users, so a second person can get their own revocable login
# instead of everyone sharing these credentials. If ADMIN_PASSWORD is unset
# and no admin account exists yet, every /admin page refuses all access.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Signing secret for the Stripe webhook (/stripe-webhook below) — created via
# the Stripe API, listening for checkout.session.completed on the live
# account. Without this set, that route refuses every request rather than
# trust an unsigned POST claiming to be Stripe.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# A RESTRICTED Stripe key (Subscriptions: Write only — never the full
# secret key) used solely to push a subscription's trial_end out once a
# client's number finishes provisioning, so every customer's trial covers
# a full 7 days of an actually-working product, not 7 days from the
# moment they paid regardless of how long their own setup took. Trial
# extension is skipped (not crashed) if this isn't set.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes

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
            CREATE TABLE IF NOT EXISTS sales_threads (
                phone TEXT PRIMARY KEY,
                business TEXT,
                trade TEXT,
                preferred_design TEXT,
                page TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_threads (
                phone TEXT PRIMARY KEY,
                email TEXT,
                customer_name TEXT,
                business TEXT,
                tier TEXT,
                checkout_session_id TEXT,
                subscription_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # subscription_id was added after this table already existed in
        # production — ALTER TABLE so an existing deployment's data.db picks
        # it up too; CREATE TABLE IF NOT EXISTS alone only helps a fresh one.
        try:
            conn.execute("ALTER TABLE onboarding_threads ADD COLUMN subscription_id TEXT")
        except sqlite3.OperationalError:
            pass  # already has the column
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS demo_overrides (
                phone TEXT PRIMARY KEY,
                business TEXT NOT NULL,
                trade TEXT NOT NULL,
                created_at TEXT NOT NULL
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                request_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def ensure_bootstrap_admin():
    """If no admin account exists yet, seed one from ADMIN_USERNAME /
    ADMIN_PASSWORD so the very first login never needs a DB row — matches
    how client onboarding falls back to env vars until a real row exists."""
    if not ADMIN_PASSWORD:
        return
    with closing(get_db()) as conn:
        if conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
            return
        conn.execute(
            "INSERT OR IGNORE INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


init_db()
ensure_bootstrap_admin()


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
    """True only if this phone was notified within the dedupe window. This is
    deliberately a ROLLING window, not a permanent flag — the point is to
    stop one back-and-forth conversation from texting the owner more than
    once, not to silently drop every future lead from a repeat customer.
    Without the window, a customer who called back in the future about a
    brand-new issue would just never generate an owner text again."""
    with closing(get_db()) as conn:
        cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(hours=LEAD_DEDUPE_HOURS)
        ).isoformat()
        row = conn.execute(
            "SELECT 1 FROM lead_notifications WHERE phone = ? AND notified_at > ?",
            (phone, cutoff),
        ).fetchone()
        return row is not None


def mark_owner_notified(phone: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO lead_notifications (phone, notified_at) VALUES (?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET notified_at=excluded.notified_at",
            (phone, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


def claim_request_once(request_key: str) -> bool:
    """True the first time this key is seen, False on a retry. Twilio
    resends a webhook POST if it doesn't get a timely response (network
    blip, cold-start delay on the free tier, etc.) — without this, a retry
    would re-run everything, including sending the SAME missed-call text or
    AI reply a second time. Callers build request_key from a Twilio-
    provided SID (CallSid, MessageSid), which Twilio keeps identical across
    retries of the same event but never reuses for a different one."""
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_webhooks (request_key, created_at) VALUES (?, ?)",
            (request_key, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0


def _normalize_us_phone(raw: str) -> str | None:
    """Turn whatever a lead typed into a request-form phone field (spaces,
    dashes, parens, a stray +1) into E.164, or None if it doesn't look like
    a real 10/11-digit US number. Twilio's API rejects a malformed `to`
    outright, so this is the difference between a lead quietly not getting
    texted and a 500 on an endpoint a real visitor's browser just called."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def _clean_text(raw: str, max_len: int = 200) -> str:
    """Strip control/formatting characters (newlines, tabs, null bytes,
    anything non-printable) out of anything a visitor typed into a public
    form before it goes anywhere near an outbound SMS or an AI prompt.
    Nothing here is sent anywhere a visitor couldn't already see it as
    plain text, so this isn't a security boundary — it just stops a
    stray newline from making one submitted field look like multiple SMS
    lines, or from breaking the formatting of an owner alert."""
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw if ch == " " or (ch.isprintable() and ch not in "\r\n\t"))
    return cleaned.strip()[:max_len]


# Simple in-memory per-key rate limiting for the public, unauthenticated
# endpoints below (/notify-request, /register-demo-caller) — each hit costs
# a real Claude API call and a real Twilio send, with no login and no
# CAPTCHA in front of it, so without this a bot hitting the form on a loop
# runs up both bills for free. In-memory is fine here, same reasoning as
# _failed_logins above: this service runs a single gunicorn worker.
_endpoint_hits: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW_SECONDS = 600  # 10 minutes
RATE_LIMIT_MAX_HITS = 5  # per key, per window


def _rate_limited(key: str) -> bool:
    now = time.time()
    recent = [t for t in _endpoint_hits.get(key, []) if now - t < RATE_LIMIT_WINDOW_SECONDS]
    recent.append(now)
    _endpoint_hits[key] = recent
    return len(recent) > RATE_LIMIT_MAX_HITS


# ---------------------------------------------------------------------
# ColdFront Studio's OWN sales outreach — separate from the per-client
# missed-call text-back product above. When a prospect submits the "free
# mockup" request form on the marketing site, we don't just alert Krish —
# we also have the AI itself text the LEAD back immediately, as a skilled
# sales assistant, using the exact same number/infra as the demo. A phone
# in this table means "the next inbound text from this number continues a
# ColdFront Studio sales conversation", so /sms routes it here instead of
# treating it as a customer of whatever demo business owns TWILIO_NUMBER.
# ---------------------------------------------------------------------
def mark_sales_thread(phone: str, business: str, trade: str, preferred_design: str, page: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO sales_threads (phone, business, trade, preferred_design, page, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET business=excluded.business, trade=excluded.trade, "
            "preferred_design=excluded.preferred_design, page=excluded.page",
            (phone, business, trade, preferred_design, page, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_sales_thread(phone: str) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM sales_threads WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------
# Onboarding — fires from the Stripe webhook the instant a NEW CUSTOMER
# (not a lead) pays. A phone in this table takes priority over a sales
# thread for the same number, since the conversation's job has changed
# from "close them" to "get them set up as a real client."
# ---------------------------------------------------------------------
def mark_onboarding_thread(
    phone: str, email: str, customer_name: str, business: str, tier: str,
    checkout_session_id: str, subscription_id: str = "",
):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO onboarding_threads (phone, email, customer_name, business, tier, checkout_session_id, subscription_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET email=excluded.email, customer_name=excluded.customer_name, "
            "business=excluded.business, tier=excluded.tier, checkout_session_id=excluded.checkout_session_id, "
            "subscription_id=excluded.subscription_id",
            (phone, email, customer_name, business, tier, checkout_session_id, subscription_id, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_onboarding_thread(phone: str) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM onboarding_threads WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None


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


def get_demo_override(phone: str) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM demo_overrides WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None


def mark_demo_override(phone: str, business: str, trade: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO demo_overrides (phone, business, trade, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET business=excluded.business, trade=excluded.trade, "
            "created_at=excluded.created_at",
            (phone, business, trade, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


def resolve_client(to_number: str, caller: str = "") -> dict:
    row = get_client_row(to_number) if to_number else None
    if row:
        return {
            "twilio_number": row["twilio_number"],
            "business_name": row["business_name"],
            "business_trade": row["business_trade"],
            "forward_to": row["forward_to"],
            "emergency_phone": row["emergency_phone"] or row["twilio_number"],
            "ring_timeout_seconds": row["ring_timeout_seconds"] or RING_TIMEOUT_SECONDS,
        }
    # No real client owns this number — it's the shared public demo line.
    # If this caller's number was registered (because they opened their
    # personalized outreach link with a `phone` param), play the demo back
    # in character as THEIR business/trade instead of the generic fallback
    # business. forward_to/emergency_phone/twilio_number stay Krish's own —
    # a missed demo call should still alert Krish, never the prospect.
    if caller:
        override = get_demo_override(caller)
        if override:
            demo_client = dict(DEFAULT_CLIENT)
            demo_client["business_name"] = override["business"] or demo_client["business_name"]
            demo_client["business_trade"] = override["trade"] or demo_client["business_trade"]
            return demo_client
    return DEFAULT_CLIENT


# ---------------------------------------------------------------------
# Admin accounts (separate from clients). Letting more than one named
# login exist means a second person can be handed their own credential —
# and have it revoked individually — instead of everyone sharing one
# password that can only ever be rotated for the whole group at once.
# ---------------------------------------------------------------------
def get_admin_password_hash(username: str):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE username = ?", (username,)
        ).fetchone()
        return row["password_hash"] if row else None


def list_admin_users() -> list[dict]:
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT username, created_at FROM admin_users ORDER BY username COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_admin_user(username: str, password: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash",
            (username, generate_password_hash(password), datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()


def delete_admin_user(username: str):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM admin_users WHERE username = ?", (username,))
        conn.commit()


# ---------------------------------------------------------------------
# Login rate limiting. In-memory and per-process, which is fine here since
# this service runs a single gunicorn worker (WEB_CONCURRENCY=1) — it just
# needs to turn "guess the password" from unlimited attempts into a
# handful per 5-minute window, not survive a restart.
# ---------------------------------------------------------------------
_failed_logins: dict[str, list[float]] = {}


def _login_rate_limit_key() -> str:
    # X-Forwarded-For first, since Render sits in front of the app as a proxy.
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "unknown"


def _is_locked_out(key: str) -> bool:
    now = time.time()
    recent = [t for t in _failed_logins.get(key, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _failed_logins[key] = recent
    return len(recent) >= MAX_LOGIN_ATTEMPTS


def _record_failed_login(key: str):
    _failed_logins.setdefault(key, []).append(time.time())


def _record_successful_login(key: str):
    _failed_logins.pop(key, None)


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
                model="claude-sonnet-5",
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
# ColdFront Studio's own AI sales assistant. Fires the moment someone
# submits the "free mockup" request form on the marketing site — texts
# the LEAD (not just Krish) immediately, as a skilled, honest, clearly-
# disclosed-as-AI sales rep, instead of waiting for a human to notice the
# alert and respond. Krish still gets the alert (see /notify-request)
# so he can jump in personally any time; this just means the lead never
# has to wait on that to hear back.
# ---------------------------------------------------------------------
# Real checkout links (same ones on the pricing section) — the model never
# types these itself. It only picks a tier; the backend sends the exact
# URL, so there's zero chance of a hallucinated or stale link going out.
CHECKOUT_LINKS = {
    "starter": "https://buy.stripe.com/bJe6oHdG44iebWP3dw3Je04",
    "growth": "https://buy.stripe.com/00w5kDfOc01Y3qjdSa3Je05",
}

SEND_CHECKOUT_TOOL = {
    "name": "send_checkout_link",
    "description": (
        "Call this the moment the lead says they want to start the trial, sign up, "
        "or get going — for the Starter or Growth tier only. This immediately texts "
        "them the real checkout link in a follow-up message (sent right after your "
        "reply), so don't also try to type a URL yourself. Don't call it more than "
        "once per conversation unless they change their mind about the tier."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["starter", "growth"],
                "description": "Which tier they want to start — Starter ($397/mo) or Growth ($697/mo)",
            },
            "business_name": {"type": "string", "description": "The lead's business name"},
        },
        "required": ["tier", "business_name"],
    },
}

FLAG_FOR_FOLLOWUP_TOOL = {
    "name": "flag_for_personal_followup",
    "description": (
        "Call this when the lead wants Full-Service (no self-serve checkout for that "
        "tier), has a question only Krish can answer, or explicitly asks to talk to a "
        "real person — Krish follows up BY TEXT, never a phone call. Don't call it "
        "with guessed values, and don't call it more than once per conversation unless "
        "something material changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "business_name": {"type": "string", "description": "The lead's business name"},
            "reason": {
                "type": "string",
                "description": "Why this needs Krish personally (e.g. 'wants Full-Service pricing', 'asked a custom question')",
            },
        },
        "required": ["business_name", "reason"],
    },
}


def build_sales_system_prompt(thread: dict) -> str:
    business = thread.get("business") or "their business"
    trade = thread.get("trade") or "a home-service"
    preferred_design = thread.get("preferred_design") or "one of the designs"
    return f"""You are the AI sales assistant for ColdFront Studio, texting a lead who just \
submitted the "get a free mockup" request form on ColdFront's own marketing site. This lead runs \
{business}, a {trade} business, and had already scrolled through the full pitch (including the \
personalized demo and pricing) before requesting this, so they are warm — your job is to close them \
over text, not to re-explain the whole pitch.

CRITICAL — this business runs entirely over TEXT, never a phone call:
- Never offer, suggest, or agree to a phone call, with you or with Krish. If a lead asks to "hop on a \
call" or "talk on the phone," say everything can be handled right here over text, and Krish will text \
them directly if anything needs his personal touch.
- If asked directly whether you're a real person, a bot, or an AI, always say yes, you're an AI — \
never claim or imply you're human. This is itself part of the pitch: you ARE the exact AI missed-call \
text-back product ColdFront sells, demonstrating itself on this very conversation. It's fine to point \
that out once, naturally, if it fits (e.g. "and yes — this is the same AI text-back you'd be buying").
- Never invent facts. What you actually know about the offer:
  - Starter: $397/mo + $399 one-time setup — AI missed-call text-back, a personalized website, monthly \
Google Business Profile posting.
  - Growth (most popular): $697/mo + $399 setup — everything in Starter, plus automated review requests, \
SMS appointment reminders, automated quote follow-up.
  - Full-Service: $1,200+/mo + $399 setup — everything in Growth, plus active local SEO, full CRM \
pipeline management, a dedicated monthly check-in call. There's no self-serve checkout for this tier — \
Krish scopes it personally, by text.
  - All tiers: 7-day trial, no contract, cancel anytime. The $399 setup is charged at checkout; monthly \
billing starts after the trial.
  - Preferred design they picked on the site: {preferred_design}.
- Never make up a start date, a technical capability, or promise something not listed above.

YOUR GOAL, in order of preference:
1. If they want Starter or Growth and seem ready, call send_checkout_link — this texts them the real \
checkout link right after your reply, so they can start the trial themselves immediately. Don't wait \
for them to ask twice; once they've said something like "sounds good" or "let's do it," send it.
2. If they want Full-Service, have a question you genuinely can't answer from the facts above, or \
explicitly ask for Krish personally, call flag_for_personal_followup — Krish will text them himself, \
never call.
3. If they say now's not a good time or they're not interested, thank them briefly and back off — don't \
be pushy past a clear "no"

STYLE:
- Keep every reply SHORT (1-3 sentences) — this is a real text conversation, not an email
- Be warm, confident, and a little conversational — not stiff or corporate-sounding, but still clearly \
an assistant doing a job, not pretending to be Krish
- Never pressure or use fake urgency/scarcity — the real differentiators (speed, no contract, free \
website) are strong enough on their own
- Messages wrapped in [SYSTEM: ...] are internal context notes about what just happened on the website \
— not something the lead said out loud. Act on them naturally; never mention "a system note" to the lead."""


def send_checkout_link_to_lead(phone: str, tier: str, business: str):
    """The model only picks a tier; this sends the real, hardcoded Stripe
    link in its own follow-up text so the lead can self-serve start the
    trial — no call, no waiting on Krish."""
    link = CHECKOUT_LINKS.get(tier, CHECKOUT_LINKS["starter"])
    label = "Growth ($697/mo)" if tier == "growth" else "Starter ($397/mo)"
    body = f"Here's the link to start your {label} 7-day trial: {link}"
    try:
        send_sms(phone, body, TWILIO_NUMBER)
        append_message(phone, "assistant", body)
        log_event("checkout_link_sent", phone=phone, tier=tier, business=business)
    except TwilioException as exc:
        log_event("checkout_link_send_failed", phone=phone, tier=tier, error=str(exc))
    # Also let Krish know, for visibility only — nothing for him to do.
    try:
        send_sms(
            FORWARD_TO,
            f"🟢 {business or '—'} just got the {label} checkout link texted to them "
            f"({_format_phone_for_display(phone)}). No action needed unless they reach out.",
            TWILIO_NUMBER,
        )
    except TwilioException as exc:
        log_event("checkout_link_owner_notify_failed", phone=phone, error=str(exc))


def notify_owner_of_sales_interest(phone: str, info: dict, business: str):
    """Text Krish when a lead needs him personally — Full-Service, a
    question the AI couldn't answer, or an explicit ask for a human.
    Always by TEXT; separate from notify_owner_of_lead (that one's for a
    client's own missed-call customers, this one's for ColdFront's own
    prospects)."""
    body = (
        "🟡 Lead needs you personally (text them, not a call)\n"
        f"Business: {info.get('business_name') or business or '—'}\n"
        f"Phone: {_format_phone_for_display(phone)}\n"
        f"Why: {info.get('reason', '—')}"
    )
    try:
        send_sms(FORWARD_TO, body, TWILIO_NUMBER)
        log_event("sales_followup_notified", phone=phone, info=info)
    except TwilioException as exc:
        log_event("sales_followup_notify_failed", phone=phone, error=str(exc))


def ai_sales_reply(phone: str, incoming_text: str, thread: dict) -> str:
    append_message(phone, "user", incoming_text)
    history = get_history(phone)

    if ai_client:
        try:
            resp = ai_client.messages.create(
                model="claude-sonnet-5",
                max_tokens=300,
                system=build_sales_system_prompt(thread),
                tools=[SEND_CHECKOUT_TOOL, FLAG_FOR_FOLLOWUP_TOOL],
                messages=history,
            )
            reply_text = ""
            for block in resp.content:
                if block.type == "text":
                    reply_text += block.text
                elif block.type == "tool_use" and block.name == "send_checkout_link":
                    send_checkout_link_to_lead(
                        phone, block.input.get("tier", "starter"), block.input.get("business_name")
                    )
                elif block.type == "tool_use" and block.name == "flag_for_personal_followup":
                    notify_owner_of_sales_interest(phone, block.input, thread.get("business"))
            reply_text = reply_text.strip() or (
                "Got it — sending that your way now!"
            )
        except APIError as exc:
            log_event("sales_ai_reply_failed", phone=phone, error=str(exc))
            reply_text = (
                "Thanks for the reply! I'm having a brief technical hiccup — "
                "Krish will follow up with you directly shortly."
            )
    else:
        reply_text = (
            "Thanks for reaching out! Krish will follow up shortly. "
            "(AI replies aren't configured yet — set ANTHROPIC_API_KEY to enable them.)"
        )

    append_message(phone, "assistant", reply_text)
    return reply_text


def kickoff_sales_outreach(phone: str, business: str, trade: str, preferred_design: str, page: str) -> str:
    """Send the AI's own opening text to a lead the moment they submit the
    request form, instead of waiting for them to text in first. Treats the
    form submission itself as the opening "turn" so the thread's history
    reads naturally for every reply after this one."""
    mark_sales_thread(phone, business, trade, preferred_design, page)
    thread = {"business": business, "trade": trade, "preferred_design": preferred_design, "page": page}
    context_note = (
        f"[SYSTEM: This lead just submitted the 'free mockup' request form on the ColdFront Studio "
        f"website — this is not something they typed to you. Business: {business or 'not given'}. "
        f"Trade: {trade or 'not given'}. Preferred design shown on the page: {preferred_design or 'none specified'}. "
        "Write your opening outbound text to them now — introduce yourself, reference their business, "
        "and invite them to text back with any questions.]"
    )
    return ai_sales_reply(phone, context_note, thread)


# ---------------------------------------------------------------------
# Onboarding AI — fires the instant someone actually pays (Stripe webhook,
# below). Collects the two things we don't already have from checkout
# (their trade, and the real phone number their calls should forward to)
# and, once it has both, auto-buys them a real Twilio number and wires up
# call forwarding immediately (see provision_client_number/complete_onboarding)
# — no waiting on Krish. The one thing that genuinely can't be made instant
# is text-back itself: a brand-new number can't SEND texts until its A2P
# 10DLC carrier registration clears (same thing the pricing page already
# tells customers), which is a telecom compliance review outside anyone's
# control — call forwarding works immediately regardless.
# ---------------------------------------------------------------------
ONBOARDING_CAPTURE_TOOL = {
    "name": "capture_onboarding_info",
    "description": (
        "Call this once you have BOTH their trade (what kind of work their business does) "
        "and the real phone number their missed calls should forward to. This immediately "
        "alerts Krish with everything needed to set them up as a client. Don't call it with "
        "guessed values, and don't call it more than once unless something changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "trade": {"type": "string", "description": "What their business does, e.g. 'plumbing', 'HVAC', 'electrical'"},
            "forward_to": {"type": "string", "description": "The real phone number their calls should ring/forward to"},
        },
        "required": ["trade", "forward_to"],
    },
}


def build_onboarding_system_prompt(thread: dict) -> str:
    business = thread.get("business") or "their business"
    customer_name = thread.get("customer_name") or "there"
    tier = thread.get("tier") or "their plan"
    return f"""You are ColdFront Studio's AI assistant, texting {customer_name}, who JUST PAID for the \
{tier} plan for their business, {business}. They're already a customer now, not a lead — this \
conversation is onboarding, not selling.

CRITICAL — this runs entirely over TEXT, never a phone call. You are an AI, and say so plainly if asked.

YOUR ONLY JOB: collect two things, naturally, in a short friendly welcome —
1. Their trade (what kind of work {business} does — plumbing, HVAC, electrical, etc.)
2. The real phone number their missed calls should forward to (may be the same number they're texting \
from, or a different business line — ask, don't assume)

As soon as you have BOTH, call capture_onboarding_info in the SAME turn as your reply, not instead of it. \
Then let them know their number is being set up right now and a confirmation text is coming in a moment.

Don't re-pitch them, don't discuss pricing (they already paid), and don't promise a specific setup time \
beyond "shortly." If they ask something you don't know, say Krish will cover it when he finishes their setup.

STYLE: keep every reply SHORT (1-3 sentences) — this is a text conversation. Warm and efficient, like a \
good onboarding specialist, not a salesperson. Messages wrapped in [SYSTEM: ...] are internal context \
notes, not something they said out loud — act on them naturally, never mention "a system note.\""""


def notify_owner_of_new_customer(business: str, tier: str, email: str, phone: str, amount_total: int | None):
    """Fires immediately from the Stripe webhook — before onboarding even
    starts texting — so Krish never finds out about a sale by checking his
    email or the Stripe dashboard."""
    amount = f"${amount_total / 100:,.2f}" if amount_total is not None else "—"
    body = (
        "🎉 New customer paid!\n"
        f"Business: {business or '—'}\n"
        f"Tier: {tier}\n"
        f"Email: {email or '—'}\n"
        f"Phone: {_format_phone_for_display(phone) if phone else '—'}\n"
        f"Charged today: {amount}\n"
        "The AI is texting them now to collect their trade + forward-to number."
    )
    try:
        send_sms(FORWARD_TO, body, TWILIO_NUMBER)
        log_event("new_customer_notified", business=business, tier=tier, email=email)
    except TwilioException as exc:
        log_event("new_customer_notify_failed", business=business, error=str(exc))


def notify_owner_ready_to_add_client(phone: str, thread: dict, info: dict):
    """Fallback for when auto-provisioning (below) couldn't buy a number —
    this is the "add them by hand in /admin/clients" moment."""
    body = (
        "✅ Ready to add as a client (number auto-buy failed, needs you)\n"
        f"Business: {thread.get('business') or '—'}\n"
        f"Trade: {info.get('trade', '—')}\n"
        f"Forward to: {info.get('forward_to', '—')}\n"
        f"Customer phone: {_format_phone_for_display(phone)}\n"
        f"Tier: {thread.get('tier') or '—'}\n"
        "Add them at /admin/clients once their Twilio number is set up."
    )
    try:
        send_sms(FORWARD_TO, body, TWILIO_NUMBER)
        log_event("onboarding_complete_notified", phone=phone, info=info)
    except TwilioException as exc:
        log_event("onboarding_complete_notify_failed", phone=phone, error=str(exc))


def _area_code(e164_phone: str) -> str | None:
    digits = re.sub(r"\D", "", e164_phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[:3]
    return None


def provision_client_number(business: str, trade: str, forward_to: str) -> str | None:
    """Buy a new Twilio number for this client and immediately wire it into
    the clients table with voice/sms webhooks pointed at this same app, so
    call forwarding works within seconds — no waiting on Krish to notice a
    text and do it by hand. Prefers the forward-to number's own area code
    for a more local-feeling number, falling back to any available one.
    Never raises: a provisioning hiccup (no numbers available, Twilio
    account restriction, etc.) should alert Krish, not crash the webhook
    that's also trying to confirm payment to the customer."""
    if not twilio_client or not PUBLIC_BASE_URL:
        return None
    try:
        # Catch a mistyped forward-to number BEFORE wiring a real client
        # number to it — Twilio Lookup's basic validity check is free (no
        # `fields` param = no paid add-ons) and catches garbage/malformed
        # numbers synchronously, rather than silently forwarding calls
        # nowhere. A lookup failure (network hiccup, rate limit) is treated
        # as "couldn't confirm it's valid," not "definitely invalid" — we'd
        # rather provision on an unconfirmed number than block a real
        # customer's setup over a Twilio Lookup outage.
        try:
            lookup = twilio_client.lookups.v2.phone_numbers(forward_to).fetch()
            if lookup.valid is False:
                log_event("forward_to_invalid", business=business, forward_to=forward_to)
                return None
        except TwilioException as exc:
            log_event("forward_to_lookup_failed", business=business, forward_to=forward_to, error=str(exc))

        area_code = _area_code(forward_to)
        candidates = []
        if area_code:
            candidates = twilio_client.available_phone_numbers("US").local.list(
                area_code=area_code, limit=1, sms_enabled=True, voice_enabled=True
            )
        if not candidates:
            candidates = twilio_client.available_phone_numbers("US").local.list(
                limit=1, sms_enabled=True, voice_enabled=True
            )
        if not candidates:
            return None
        bought = twilio_client.incoming_phone_numbers.create(
            phone_number=candidates[0].phone_number,
            voice_url=f"{PUBLIC_BASE_URL}/voice",
            voice_method="POST",
            sms_url=f"{PUBLIC_BASE_URL}/sms",
            sms_method="POST",
        )
        upsert_client(bought.phone_number, business or "New Client", trade or "home services", forward_to)
        log_event("number_provisioned", business=business, twilio_number=bought.phone_number)
        return bought.phone_number
    except TwilioException as exc:
        log_event("number_provisioning_failed", business=business, error=str(exc))
        return None


TRIAL_LENGTH_DAYS = 7


def extend_trial_to_full_term(subscription_id: str, business: str):
    """Pushes the subscription's trial_end out to exactly 7 days from RIGHT
    NOW — the moment call forwarding actually goes live — instead of 7 days
    from the moment they paid. Without this, a customer whose onboarding Q&A
    or number provisioning took longer effectively gets a shorter real
    trial than one whose setup was instant, for the exact same price. Only
    ever pushes the date LATER, never earlier, and is a no-op (not a crash)
    if STRIPE_SECRET_KEY isn't configured or the call fails for any reason —
    a billing hiccup here should never block the customer's actual setup."""
    if not STRIPE_SECRET_KEY or not subscription_id:
        log_event("trial_extension_skipped", business=business, reason="no_key_or_sub_id")
        return
    try:
        new_trial_end = int(time.time()) + TRIAL_LENGTH_DAYS * 86400
        stripe.Subscription.modify(subscription_id, trial_end=new_trial_end)
        log_event("trial_extended", business=business, subscription_id=subscription_id, new_trial_end=new_trial_end)
    except stripe.error.StripeError as exc:
        log_event("trial_extension_failed", business=business, subscription_id=subscription_id, error=str(exc))


def complete_onboarding(phone: str, thread: dict, info: dict):
    """Dispatched from capture_onboarding_info. Tries to auto-provision a
    real client number first; only falls back to "do it by hand" if that
    genuinely can't be done right now (no numbers available, Twilio
    hiccup, etc.) — the customer should never wait on Krish noticing a
    text if the whole thing can just happen automatically."""
    business = thread.get("business") or ""
    trade = info.get("trade", "")
    forward_to_raw = info.get("forward_to", "")
    forward_to = _normalize_us_phone(forward_to_raw) or forward_to_raw

    # Extend the trial regardless of whether auto-provisioning succeeds
    # below — this was previously only called on the success path, which
    # meant the one customer whose setup needed Krish's manual help (the
    # exact case the fairness fix exists for) was the one who never got
    # it. Not perfect when it falls back to manual (their forwarding may
    # not actually go live the instant this fires), but "now + 7 days"
    # beats "never extended."
    extend_trial_to_full_term(thread.get("subscription_id", ""), business)

    new_number = provision_client_number(business, trade, forward_to)
    if new_number:
        display_number = _format_phone_for_display(new_number)
        try:
            send_sms(
                FORWARD_TO,
                f"✅ Auto-provisioned {display_number} for {business or '—'} ({trade}), "
                f"forwarding to {_format_phone_for_display(forward_to)}. Call forwarding is live now. "
                "Text-back activates once carrier (A2P) registration clears — usually within a day or two.",
                TWILIO_NUMBER,
            )
        except TwilioException as exc:
            log_event("auto_provision_owner_notify_failed", phone=phone, error=str(exc))
        try:
            send_sms(
                phone,
                f"You're all set! Your number is {display_number} — save it, this is what customers will "
                "see and call. Call forwarding is live right now. Text-back (texting anyone you miss) "
                "switches on automatically once carrier registration clears, usually within a day or two. "
                f"Your {TRIAL_LENGTH_DAYS}-day trial starts counting from right now, not from when you paid, "
                "so you get the full trial on a working system.",
                TWILIO_NUMBER,
            )
            append_message(phone, "assistant", "[auto-provisioning confirmation sent]")
        except TwilioException as exc:
            log_event("auto_provision_customer_notify_failed", phone=phone, error=str(exc))
    else:
        notify_owner_ready_to_add_client(phone, thread, info)


def ai_onboarding_reply(phone: str, incoming_text: str, thread: dict) -> str:
    append_message(phone, "user", incoming_text)
    history = get_history(phone)

    if ai_client:
        try:
            resp = ai_client.messages.create(
                model="claude-sonnet-5",
                max_tokens=300,
                system=build_onboarding_system_prompt(thread),
                tools=[ONBOARDING_CAPTURE_TOOL],
                messages=history,
            )
            reply_text = ""
            for block in resp.content:
                if block.type == "text":
                    reply_text += block.text
                elif block.type == "tool_use" and block.name == "capture_onboarding_info":
                    complete_onboarding(phone, thread, block.input)
            reply_text = reply_text.strip() or "Got it, thanks!"
        except APIError as exc:
            log_event("onboarding_ai_reply_failed", phone=phone, error=str(exc))
            reply_text = (
                "Thanks! I'm having a brief technical hiccup — Krish will follow up by text shortly."
            )
    else:
        reply_text = "Thanks for your payment! Krish will text you shortly to finish setup."

    append_message(phone, "assistant", reply_text)
    return reply_text


def kickoff_onboarding(
    phone: str, email: str, customer_name: str, business: str, tier: str,
    checkout_session_id: str, subscription_id: str = "",
) -> str:
    """Send the AI's own welcome/onboarding text the instant Stripe confirms
    payment — same pattern as kickoff_sales_outreach, just a different
    thread table and a different job (collect info, not close a sale)."""
    mark_onboarding_thread(phone, email, customer_name, business, tier, checkout_session_id, subscription_id)
    thread = {"business": business, "customer_name": customer_name, "tier": tier, "subscription_id": subscription_id}
    context_note = (
        f"[SYSTEM: {customer_name or 'This customer'} just completed payment for the {tier} plan "
        f"for {business or 'their business'}. This is not something they typed to you. Write your "
        "opening welcome text now — thank them, and ask for their trade and the real forward-to "
        "number in one short, friendly message.]"
    )
    return ai_onboarding_reply(phone, context_note, thread)


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
                model="claude-sonnet-5",
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
    client = resolve_client(to_number, caller)
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
    client = resolve_client(to_number, caller)
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
    call_sid = request.values.get("CallSid", "")
    to_number = request.values.get("To", "")
    client = resolve_client(to_number, caller)
    # DialCallStatus normally arrives as a POST form field (Twilio's own
    # <Dial action=...> callback does this). But the no-FORWARD_TO fallback
    # above redirects here with it as a URL query param instead, which
    # request.form can't see — use request.values so both paths work.
    call_status = request.values.get("DialCallStatus", "unknown")
    log_event("call_status", caller=caller, status=call_status, business=client["business_name"])

    vr = VoiceResponse()
    if call_status in ("no-answer", "busy", "failed", "canceled"):
        # Twilio retries this webhook if it doesn't get a timely response
        # (cold-start delay on the free tier is a real way to trigger that).
        # Without this check, a retry would text the caller a second time.
        if call_sid and not claim_request_once(f"voice_status:{call_sid}"):
            log_event("voice_status_duplicate_retry_skipped", caller=caller, call_sid=call_sid)
        elif has_consent(caller):
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
STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_KEYWORDS = {"START", "YES", "UNSTOP"}
HELP_KEYWORDS = {"HELP", "INFO"}


@app.route("/sms", methods=["POST"])
@validate_twilio_request
def sms():
    caller = request.form.get("From", "unknown")
    to_number = request.form.get("To", "")
    body = request.form.get("Body", "")
    message_sid = request.form.get("MessageSid", "")

    # Twilio retries this webhook if it doesn't get a timely response. A
    # retry carries the same MessageSid as the original, so this makes a
    # retry a no-op instead of calling the AI again and sending a second
    # reply (or a second STOP/HELP confirmation) for one inbound text.
    if message_sid and not claim_request_once(f"sms:{message_sid}"):
        log_event("sms_duplicate_retry_skipped", caller=caller, message_sid=message_sid)
        return Response(str(MessagingResponse()), mimetype="text/xml")

    # A reply from someone who just PAID continues onboarding, not sales —
    # checked before the sales-thread check below, since a lead who converts
    # still has an open sales_threads row and we want the newer, more
    # relevant conversation (collecting setup info) to win.
    onboarding_thread = get_onboarding_thread(caller)
    if onboarding_thread:
        normalized = body.strip().upper()
        resp = MessagingResponse()
        if normalized in STOP_KEYWORDS:
            set_consent(caller, False)
            log_event("onboarding_sms_opt_out", caller=caller)
            resp.message("You've been unsubscribed from ColdFront Studio texts. Reply START to resubscribe.")
            return Response(str(resp), mimetype="text/xml")
        if normalized in START_KEYWORDS:
            set_consent(caller, True)
            log_event("onboarding_sms_opt_in_via_text", caller=caller)
            resp.message("You're resubscribed to ColdFront Studio texts. Reply STOP at any time to opt out.")
            return Response(str(resp), mimetype="text/xml")
        reply_text = ai_onboarding_reply(caller, body, onboarding_thread)
        log_event("onboarding_ai_reply_sent", caller=caller, reply=reply_text)
        resp.message(reply_text)
        return Response(str(resp), mimetype="text/xml")

    # A reply from a lead ColdFront's own sales AI already texted first (via
    # /notify-request) continues THAT conversation, regardless of which demo
    # business happens to own TWILIO_NUMBER — this must be checked before the
    # normal per-client resolution below, or the reply would get answered in
    # character as whatever HVAC demo business TWILIO_NUMBER defaults to.
    sales_thread = get_sales_thread(caller)
    if sales_thread:
        normalized = body.strip().upper()
        resp = MessagingResponse()
        if normalized in STOP_KEYWORDS:
            set_consent(caller, False)
            log_event("sales_sms_opt_out", caller=caller)
            resp.message("You've been unsubscribed from ColdFront Studio texts. Reply START to resubscribe.")
            return Response(str(resp), mimetype="text/xml")
        if normalized in START_KEYWORDS:
            set_consent(caller, True)
            log_event("sales_sms_opt_in_via_text", caller=caller)
            resp.message("You're resubscribed to ColdFront Studio texts. Reply STOP at any time to opt out.")
            return Response(str(resp), mimetype="text/xml")
        reply_text = ai_sales_reply(caller, body, sales_thread)
        log_event("sales_ai_reply_sent", caller=caller, reply=reply_text)
        resp.message(reply_text)
        return Response(str(resp), mimetype="text/xml")

    client = resolve_client(to_number, caller)
    log_event("incoming_sms", caller=caller, body=body, business=client["business_name"])

    # SMS opt-out/help keywords (TCPA / A2P 10DLC compliance). Twilio-level
    # Advanced Opt-Out may already intercept some of these before they ever
    # reach this webhook, but handling them here too keeps OUR OWN consent
    # record (sms_consent — what the missed-call text-back checks) in sync
    # regardless, and means HELP/START always work even if that Twilio
    # feature isn't enabled on this number. These short-circuit before the
    # AI entirely — no LLM call, no lead capture, just the required reply.
    normalized = body.strip().upper()
    resp = MessagingResponse()

    if normalized in STOP_KEYWORDS:
        set_consent(caller, False)
        log_event("sms_opt_out", caller=caller, business=client["business_name"])
        resp.message(
            f"You've been unsubscribed from {client['business_name']} text messages "
            "and won't receive any more. Reply START to resubscribe."
        )
        return Response(str(resp), mimetype="text/xml")

    if normalized in START_KEYWORDS:
        set_consent(caller, True)
        log_event("sms_opt_in_via_text", caller=caller, business=client["business_name"])
        resp.message(
            f"You're resubscribed to {client['business_name']} text messages. "
            "Reply STOP at any time to opt out."
        )
        return Response(str(resp), mimetype="text/xml")

    if normalized in HELP_KEYWORDS:
        resp.message(
            f"{client['business_name']}: text us here for help with your service "
            f"request, or call {_format_phone_for_display(client['twilio_number'])}. "
            "Reply STOP to opt out. Msg & data rates may apply."
        )
        return Response(str(resp), mimetype="text/xml")

    reply_text = ai_reply(caller, body, client)
    log_event("ai_reply_sent", caller=caller, reply=reply_text)

    resp.message(reply_text)
    return Response(str(resp), mimetype="text/xml")


# ---------------------------------------------------------------------
# Admin: onboard/update/remove a client without touching code or env vars,
# and manage who's allowed to do that. Protected by HTTP Basic Auth against
# the admin_users table rather than a secret baked into the URL — a
# URL-embedded secret ends up in browser history, can get logged by
# intermediate services, and is easy to leak by pasting the link somewhere.
# Basic Auth keeps the credential in a login prompt instead, passwords are
# hashed at rest (never stored or compared as plaintext), and a handful of
# wrong guesses locks out that source for a few minutes.
# ---------------------------------------------------------------------
def require_admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        rl_key = _login_rate_limit_key()
        if _is_locked_out(rl_key):
            log_event("admin_login_locked_out", source=rl_key)
            return Response("Too many failed login attempts. Try again in a few minutes.", 429)

        auth = request.authorization
        password_hash = get_admin_password_hash(auth.username) if auth else None
        ok = bool(auth and password_hash and check_password_hash(password_hash, auth.password or ""))

        if not ok:
            _record_failed_login(rl_key)
            return Response(
                "Login required.", 401,
                {"WWW-Authenticate": 'Basic realm="ColdFront admin"'},
            )
        _record_successful_login(rl_key)
        return f(*args, **kwargs)

    return decorated


@app.route("/admin/clients", methods=["GET", "POST"])
@require_admin_auth
def admin_clients():
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
nav a{{font-size:.85rem;color:#555}}
</style></head><body>
<nav><a href="/admin/users">Manage admin logins &rarr;</a></nav>
<h1>Clients</h1>
{'<p class="msg">' + message + '</p>' if message else ''}
<table><tr><th>Business</th><th>Twilio #</th><th>Trade</th><th>Forward to</th><th>Emergency #</th><th></th></tr>
{rows_html}
</table>
<h2>Add / update a client</h2>
<form method="POST">
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


# ---------------------------------------------------------------------
# Admin: manage who can log into /admin. Each admin gets their own
# username/password (hashed, never stored or shown in plaintext) so access
# can be granted or revoked for one person without rotating a shared
# password everyone else would also have to re-enter.
# ---------------------------------------------------------------------
@app.route("/admin/users", methods=["GET", "POST"])
@require_admin_auth
def admin_users_page():
    message = ""

    if request.method == "POST":
        action = request.form.get("action", "save")
        username = request.form.get("username", "").strip()
        existing = list_admin_users()

        if action == "delete":
            if len(existing) <= 1:
                message = "Can't remove the last remaining admin account — you'd lock everyone out."
            elif username:
                delete_admin_user(username)
                message = f"Removed admin login '{username}'."
        else:
            password = request.form.get("password", "")
            if not username or len(password) < 8:
                message = "Username is required and password must be at least 8 characters."
            else:
                upsert_admin_user(username, password)
                message = f"Saved admin login '{username}'."

    admins = list_admin_users()
    can_remove = len(admins) > 1
    rows_html = "".join(
        f"""<tr>
            <td>{a['username']}</td><td>{a['created_at'][:10]}</td>
            <td><form method="POST" style="display:inline" onsubmit="return confirm('Remove admin login for {a['username']}?')">
                <input type="hidden" name="action" value="delete">
                <input type="hidden" name="username" value="{a['username']}">
                <button type="submit" {'disabled' if not can_remove else ''}>Remove</button></form></td>
        </tr>"""
        for a in admins
    )

    return f"""<html><head><title>Admin logins</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:600px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
table{{width:100%;border-collapse:collapse;margin-bottom:2rem;font-size:.85rem}}
td,th{{border:1px solid #ddd;padding:.5rem .6rem;text-align:left}}
th{{background:#f5f5f5}}
label{{display:block;margin-top:.8rem;font-weight:600;font-size:.9rem}}
input{{width:100%;padding:.5rem;box-sizing:border-box;margin-top:.2rem}}
button{{margin-top:1rem;padding:.6rem 1.2rem;cursor:pointer}}
.msg{{background:#e6f4ea;padding:.6rem .8rem;border-radius:4px;margin-bottom:1rem}}
nav a{{font-size:.85rem;color:#555}}
</style></head><body>
<nav><a href="/admin/clients">&larr; Back to clients</a></nav>
<h1>Admin logins</h1>
<p style="font-size:.85rem;color:#555">Each row below is its own username/password — add one per person who needs
access, and remove a row the moment someone shouldn't have it anymore instead of changing a password everyone shares.</p>
{'<p class="msg">' + message + '</p>' if message else ''}
<table><tr><th>Username</th><th>Added</th><th></th></tr>{rows_html}</table>
<h2>Add / update an admin login</h2>
<form method="POST">
<label>Username</label>
<input name="username" required>
<label>Password (at least 8 characters)</label>
<input name="password" type="password" required minlength="8">
<button type="submit">Save login</button>
</form>
</body></html>"""


ALLOWED_SITE_ORIGIN = "https://coldfront.studio"


@app.route("/notify-request", methods=["POST", "OPTIONS"])
def notify_request():
    """Text FORWARD_TO the moment someone submits the "free mockup" request
    form on the outreach site. Formspree only delivers to an inbox nobody is
    watching in real time; this is the thing that actually reaches a phone.
    Best-effort and intentionally forgiving: a bad or missing field should
    never 500 on the visitor, it should just skip the text."""
    if request.method == "OPTIONS":
        resp = Response("", status=204)
    elif _rate_limited(f"notify-request:{request.remote_addr}"):
        log_event("notify_request_rate_limited", remote_addr=request.remote_addr)
        resp = Response("", status=204)
    else:
        business = _clean_text(request.form.get("business", ""), 120)
        trade = _clean_text(request.form.get("trade", ""), 60)
        phone = _clean_text(request.form.get("phone", ""), 30)
        preferred_design = _clean_text(request.form.get("preferred_design", ""), 40)
        page = _clean_text(request.form.get("page", ""), 200)

        if business and phone and FORWARD_TO and TWILIO_NUMBER:
            lead_e164 = _normalize_us_phone(phone)
            body = (
                "🔔 New mockup request (outreach site)\n"
                f"Business: {business}\n"
                f"Trade: {trade or 'not given'}\n"
                f"Phone: {phone}"
                + ("" if lead_e164 else " — couldn't parse this as a real US number, AI follow-up text was skipped, you'll need to reach out yourself")
                + f"\nPreferred design: {preferred_design or 'none picked'}\n"
                f"Page: {page or 'unknown'}"
            )
            try:
                send_sms(FORWARD_TO, body, TWILIO_NUMBER)
                log_event("mockup_request_notified", business=business, phone=phone)
            except TwilioException as exc:
                log_event("mockup_request_notify_failed", business=business, error=str(exc))

            # Have the AI itself text the LEAD back immediately too, as a
            # sales assistant — rather than leaving the lead waiting on
            # Krish to see the alert above and respond personally. Best-
            # effort: a bad phone number or a hiccup here must never affect
            # the owner alert that already succeeded above.
            if lead_e164:
                try:
                    opening_text = kickoff_sales_outreach(lead_e164, business, trade, preferred_design, page)
                    send_sms(lead_e164, opening_text, TWILIO_NUMBER)
                    log_event("sales_outreach_sent", business=business, phone=lead_e164)
                except TwilioException as exc:
                    log_event("sales_outreach_failed", business=business, phone=lead_e164, error=str(exc))
            else:
                log_event("sales_outreach_skipped_bad_phone", business=business, phone=phone)
        else:
            log_event("mockup_request_skipped", business=business, phone=phone)

        resp = Response("", status=204)

    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_SITE_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/register-demo-caller", methods=["POST", "OPTIONS"])
def register_demo_caller():
    """Called silently from the outreach page itself (fire-and-forget, same
    pattern as /notify-request) so the shared public demo line — (706)
    808-2127 — can answer IN CHARACTER as the actual prospect's business and
    trade instead of the generic fallback, without requiring them to type
    anything. The outreach link already carries the business's own public
    phone number as a `phone` URL param (scraped from their Google listing),
    so if they call the demo line from that same number, we already know
    who they are. If they call from a different number, it just falls back
    to the generic demo — no harm, no error, nothing to break."""
    if request.method == "OPTIONS":
        resp = Response("", status=204)
    elif _rate_limited(f"register-demo-caller:{request.remote_addr}"):
        log_event("register_demo_caller_rate_limited", remote_addr=request.remote_addr)
        resp = Response("", status=204)
    else:
        business = _clean_text(request.form.get("business", ""), 120)
        trade = _clean_text(request.form.get("trade", ""), 60)
        phone = _clean_text(request.form.get("phone", ""), 30)

        caller_e164 = _normalize_us_phone(phone)
        if business and trade and caller_e164:
            mark_demo_override(caller_e164, business, trade)
            log_event("demo_override_registered", business=business, trade=trade)
        else:
            log_event("demo_override_skipped", business=business, trade=trade, phone=phone)

        resp = Response("", status=204)

    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_SITE_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# Real Payment Link IDs (Starter / Growth) — used to tag which tier someone
# just bought. Full-Service has no self-serve link (it's a "talk to us"
# mailto), so it never reaches this webhook.
PAYMENT_LINK_TIERS = {
    "plink_1UQbcFHAFB70lN9eGmYKOKLL": "Starter ($397/mo)",
    "plink_1UQbcHHAFB70lN9e7I2PWrGd": "Growth ($697/mo)",
}


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """Fires the instant a checkout completes on either live Payment Link.
    Texts Krish immediately, then kicks off the AI onboarding conversation
    with the new customer — see kickoff_onboarding above for why actual
    Twilio-number provisioning stays a manual step for now."""
    if not STRIPE_WEBHOOK_SECRET:
        log_event("stripe_webhook_no_secret_configured")
        abort(403)

    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except (stripe.error.SignatureVerificationError, ValueError) as exc:
        log_event("stripe_webhook_signature_invalid", error=str(exc))
        abort(400)

    # Stripe retries a webhook that doesn't get a timely 2xx — same
    # idempotency pattern as the Twilio webhooks above, keyed by event id.
    if not claim_request_once(f"stripe:{event['id']}"):
        log_event("stripe_webhook_duplicate_retry_skipped", event_id=event["id"])
        return Response("", status=204)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_details = session.get("customer_details") or {}
        email = _clean_text(customer_details.get("email") or "", 200)
        customer_name = _clean_text(customer_details.get("name") or "", 120)
        raw_phone = _clean_text(customer_details.get("phone") or "", 30)
        amount_total = session.get("amount_total")
        tier = PAYMENT_LINK_TIERS.get(session.get("payment_link"), "unknown tier")

        business = ""
        for field in session.get("custom_fields") or []:
            if field.get("key") == "business_name":
                business = _clean_text((field.get("text") or {}).get("value") or "", 120)

        notify_owner_of_new_customer(business, tier, email, raw_phone, amount_total)

        customer_e164 = _normalize_us_phone(raw_phone)
        if customer_e164:
            try:
                opening_text = kickoff_onboarding(
                    customer_e164, email, customer_name, business, tier, session.get("id", ""),
                    session.get("subscription") or ""
                )
                send_sms(customer_e164, opening_text, TWILIO_NUMBER)
                log_event("onboarding_kickoff_sent", business=business, phone=customer_e164)
            except TwilioException as exc:
                log_event("onboarding_kickoff_failed", business=business, error=str(exc))
        else:
            # This is a PAYING customer we now have no text-based way to
            # reach — the generic "new customer paid" alert above already
            # went out, but make sure this specific problem can't get lost
            # in it: a customer who already gave us money and got silence
            # back is the worst version of this gap.
            log_event("onboarding_kickoff_skipped_no_phone", business=business, email=email)
            try:
                send_sms(
                    FORWARD_TO,
                    f"⚠️ Couldn't parse a phone number for {business or email or 'the customer above'} "
                    f"(raw value: \"{raw_phone or 'none given'}\") — no onboarding text was sent. "
                    f"Reach them by email ({email or 'no email either'}) and set them up by hand.",
                    TWILIO_NUMBER,
                )
            except TwilioException as exc:
                log_event("onboarding_no_phone_alert_failed", business=business, error=str(exc))

    return Response("", status=204)


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "business": BUSINESS_NAME, "time": str(datetime.datetime.now())}


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
