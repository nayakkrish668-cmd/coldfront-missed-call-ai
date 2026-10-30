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
# The A2P-10DLC-approved Messaging Service every auto-provisioned client
# number must be attached to, or carriers filter/block its SMS entirely.
# Defaults to the real "ColdFront Studio / Starter" campaign's service
# (approved Oct 13, 2026); override via env if a different one is used.
TWILIO_MESSAGING_SERVICE_SID = os.environ.get(
    "TWILIO_MESSAGING_SERVICE_SID", "MG98dd1f6a06107af753b2ac3758ed0604"
)
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
ROUTING_RING_FIRST = "ring_first"
ROUTING_CARRIER_FORWARD = "carrier_forward"

DEFAULT_CLIENT = {
    "routing_mode": ROUTING_RING_FIRST,
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
                subscription_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # subscription_id was added after this table already existed in
        # production — same ALTER TABLE migration pattern as
        # onboarding_threads.subscription_id above.
        try:
            conn.execute("ALTER TABLE clients ADD COLUMN subscription_id TEXT")
        except sqlite3.OperationalError:
            pass  # already has the column
        # routing_mode: how calls reach this client's ColdFront number.
        #   ring_first      - customers call the ColdFront number directly; it
        #                     rings forward_to first and texts back if unanswered.
        #   carrier_forward - the business keeps its existing number and its
        #                     carrier forwards UNANSWERED calls to ColdFront. The
        #                     call has already gone unanswered, so we text right
        #                     away and take a voicemail; ringing forward_to again
        #                     would bounce straight back here in a loop.
        try:
            conn.execute("ALTER TABLE clients ADD COLUMN routing_mode TEXT")
        except sqlite3.OperationalError:
            pass
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
    subscription_id: str | None = None,
    routing_mode: str | None = None,
):
    now = datetime.datetime.utcnow().isoformat()
    with closing(get_db()) as conn:
        conn.execute(
            """
            INSERT INTO clients
                (twilio_number, business_name, business_trade, forward_to,
                 emergency_phone, ring_timeout_seconds, subscription_id, routing_mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(twilio_number) DO UPDATE SET
                business_name=excluded.business_name,
                business_trade=excluded.business_trade,
                forward_to=excluded.forward_to,
                emergency_phone=excluded.emergency_phone,
                ring_timeout_seconds=excluded.ring_timeout_seconds,
                subscription_id=COALESCE(excluded.subscription_id, clients.subscription_id),
                routing_mode=COALESCE(excluded.routing_mode, clients.routing_mode),
                updated_at=excluded.updated_at
            """,
            (
                twilio_number,
                business_name,
                business_trade,
                forward_to,
                emergency_phone,
                ring_timeout_seconds,
                subscription_id,
                routing_mode,
                now,
                now,
            ),
        )
        conn.commit()


def delete_client(twilio_number: str):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM clients WHERE twilio_number = ?", (twilio_number,))
        conn.commit()


def get_client_by_subscription(subscription_id: str):
    if not subscription_id:
        return None
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE subscription_id = ?", (subscription_id,)
        ).fetchone()
        return dict(row) if row else None


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
            "routing_mode": row.get("routing_mode") or ROUTING_RING_FIRST,
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
    if client.get("routing_mode") == ROUTING_CARRIER_FORWARD:
        return (
            f"Thanks for calling {client['business_name']}. Sorry we couldn't get to the phone. "
            "Press 1 and we'll text you right now so we can help. "
            "Message and data rates may apply."
        )
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


# Uptime monitoring only confirms the server is reachable, not that the AI
# is actually working — a bad Anthropic key, an exhausted credit balance, or
# an outage would still return "200 ok" on the health check while every real
# customer silently gets a canned fallback reply instead of the AI. This is
# the one thing that actually surfaces that to a human: the first time any
# of the AI reply paths fails, text the business owner directly. Keyed by
# `kind` with a cooldown so one bad stretch doesn't send dozens of texts.
_last_failure_alert: dict[str, float] = {}
FAILURE_ALERT_COOLDOWN_SECONDS = 1800  # 30 min


def alert_owner_of_system_failure(kind: str, detail: str):
    if not FORWARD_TO:
        return
    now = time.time()
    last = _last_failure_alert.get(kind, 0.0)
    if now - last < FAILURE_ALERT_COOLDOWN_SECONDS:
        return
    _last_failure_alert[kind] = now
    try:
        send_sms(
            FORWARD_TO,
            f"⚠️ ColdFront system alert: {detail} Customers are getting a "
            f"fallback reply instead of the real AI response right now. "
            f"Check Render logs and your Anthropic API balance.",
            TWILIO_NUMBER,
        )
    except TwilioException as exc:
        log_event("owner_failure_alert_failed", kind=kind, error=str(exc))


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
        record_event(client["twilio_number"], "lead_captured", caller, lead.get("issue_description", "")[:200])
    except TwilioException as exc:
        # Never let a failed owner-notification crash the customer's reply —
        # the customer still needs their text even if this alert fails.
        log_event("owner_notify_failed", caller=caller, error=str(exc))
    mark_owner_notified(caller)


def ai_reply(caller: str, incoming_text: str, client: dict, state: dict | None = None) -> str:
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
                    if state is not None:
                        state["captured"] = True
            reply_text = reply_text.strip() or (
                "Thanks — I've got what I need and I'm passing it to the team now!"
            )
        except APIError as exc:
            # Anthropic being down, rate-limited, or out of credits must
            # never surface as a raw 500 — that's what gets a real customer
            # "application error occurred" mid-conversation. Log it and
            # still text back something useful.
            log_event("ai_reply_failed", caller=caller, error=str(exc))
            alert_owner_of_system_failure("anthropic_api_failure", "The missed-call AI text-back is failing.")
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


# ---------------------------------------------------------------------
# Demo line -> sale. The demo text conversation is pure role-play (the AI
# plays the prospect's own receptionist), so on its own it never pitches
# anything. Once a prospect has seen enough of it, add ONE short note
# saying "that was the demo, reply SETUP to get it on your line". A SETUP
# reply hands them to the sales assistant above, which can text the real
# checkout link.
# ---------------------------------------------------------------------
DEMO_LINE_SOURCE = "demo-line"
DEMO_WRAPUP_AFTER_USER_TURNS = 3
DEMO_SETUP_KEYWORDS = {"SETUP", "SET UP", "SET IT UP", "SIGN UP", "SIGNUP", "SIGN ME UP"}


def _request_key_seen(request_key: str) -> bool:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_webhooks WHERE request_key = ?", (request_key,)
        ).fetchone()
        return row is not None


def demo_wrapup_text(caller: str) -> str:
    override = get_demo_override(caller)
    whose = f"{override['business']}'s customers" if override else "your customers"
    return (
        f"(That was the demo: it's exactly what {whose} would get when you miss a call. "
        "Want it running on your real line? Reply SETUP and I'll send the details. "
        "- ColdFront Studio)"
    )


def maybe_demo_wrapup(caller: str, captured: bool) -> str | None:
    """The wrap-up text to add after this demo reply, or None. Sent once per
    caller, after the AI captured a lead or a few back-and-forths in."""
    user_turns = sum(1 for m in get_history(caller) if m["role"] == "user")
    if not captured and user_turns < DEMO_WRAPUP_AFTER_USER_TURNS:
        return None
    if not claim_request_once(f"demo_wrapup:{caller}"):
        return None
    text = demo_wrapup_text(caller)
    append_message(caller, "assistant", text)
    log_event("demo_wrapup_sent", caller=caller, captured=captured)
    return text


def start_sales_from_demo(caller: str) -> str:
    override = get_demo_override(caller) or {}
    business, trade = override.get("business", ""), override.get("trade", "")
    mark_sales_thread(caller, business, trade, "", DEMO_LINE_SOURCE)
    thread = get_sales_thread(caller) or {"business": business, "trade": trade, "page": DEMO_LINE_SOURCE}
    try:
        send_sms(
            FORWARD_TO,
            f"🔥 {business or 'A demo caller'} ({_format_phone_for_display(caller)}) tried the demo line "
            "and replied SETUP. The AI is texting them the details now.",
            TWILIO_NUMBER,
        )
    except TwilioException as exc:
        log_event("demo_setup_owner_notify_failed", caller=caller, error=str(exc))
    log_event("demo_setup_requested", caller=caller, business=business)
    note = (
        "[SYSTEM: The lead just replied SETUP after the demo — they want this on their real line. "
        "Thank them, give the plan that fits in a sentence, and help them start the trial.]"
    )
    return ai_sales_reply(caller, note, thread)


def build_sales_system_prompt(thread: dict) -> str:
    business = thread.get("business") or "their business"
    trade = thread.get("trade") or "a home-service"
    preferred_design = thread.get("preferred_design") or "one of the designs"
    if thread.get("page") == DEMO_LINE_SOURCE:
        intro = f"""You are the AI sales assistant for ColdFront Studio, texting a lead who runs \
{business}, a {trade} business. They got a cold email from ColdFront, called the live demo line, and \
just went through the demo text conversation (the earlier messages in this thread were the demo: the AI \
was playing THEIR receptionist and they were playing a customer). After the demo they replied SETUP, \
meaning they want it on their real line. They have NOT necessarily seen the pricing page, so briefly \
give the price that fits when it's relevant, then help them start the trial — don't re-explain how the \
text-back works, they just experienced it."""
    else:
        intro = f"""You are the AI sales assistant for ColdFront Studio, texting a lead who just \
submitted the "get a free mockup" request form on ColdFront's own marketing site. This lead runs \
{business}, a {trade} business, and had already scrolled through the full pitch (including the \
personalized demo and pricing) before requesting this, so they are warm — your job is to close them \
over text, not to re-explain the whole pitch."""
    return intro + f"""

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
            alert_owner_of_system_failure("anthropic_api_failure", "The missed-call AI text-back is failing.")
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
        "Call this once you have their trade (what kind of work their business does), their "
        "main business number, and the cell number for lead alerts. This immediately "
        "alerts Krish with everything needed to set them up as a client. Don't call it with "
        "guessed values, and don't call it more than once unless something changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "trade": {"type": "string", "description": "What their business does, e.g. 'plumbing', 'HVAC', 'electrical'"},
            "business_phone": {"type": "string", "description": "Their main business number, the one customers already call"},
            "forward_to": {"type": "string", "description": "The cell number where new-lead alerts should be texted"},
        },
        "required": ["trade", "business_phone", "forward_to"],
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

YOUR ONLY JOB: collect these, naturally, in a short friendly welcome —
1. Their trade (what kind of work {business} does — plumbing, HVAC, electrical, etc.)
2. Their main business phone number — the one customers already call (from Google, their trucks, \
their website).
3. A cell phone number where they want new-lead alerts texted (often the owner's cell — may be the \
same number they're texting from; ask, don't assume).

How it works, if they ask: they KEEP their existing business number. We give them a ColdFront number \
behind the scenes, and they set their current line to forward unanswered calls to it, so every call \
they miss gets a text back and they still get voicemails. Nothing changes on Google or their trucks.

As soon as you have all three, call capture_onboarding_info in the SAME turn as your reply, not instead \
of it. Then let them know their setup is happening right now and the next text will have their one \
last step.

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


def provision_client_number(
    business: str, trade: str, forward_to: str, subscription_id: str = "",
    routing_mode: str = ROUTING_RING_FIRST, emergency_phone: str | None = None,
) -> str | None:
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
        # A raw number is NOT covered by A2P 10DLC approval on its own —
        # carriers filter/block its SMS until it's attached to the approved
        # Messaging Service. Without this, every auto-provisioned client
        # number would buy fine, forward calls fine, and then silently be
        # unable to send a single text. Never let this block provisioning:
        # a failure here gets logged (and surfaced below) rather than
        # unwinding the number purchase or failing the whole onboarding.
        if TWILIO_MESSAGING_SERVICE_SID:
            try:
                twilio_client.messaging.v1.services(TWILIO_MESSAGING_SERVICE_SID).phone_numbers.create(
                    phone_number_sid=bought.sid
                )
                log_event("number_added_to_messaging_service", business=business, twilio_number=bought.phone_number)
            except TwilioException as exc:
                log_event(
                    "number_messaging_service_attach_failed",
                    business=business,
                    twilio_number=bought.phone_number,
                    error=str(exc),
                )
        upsert_client(
            bought.phone_number, business or "New Client", trade or "home services", forward_to,
            emergency_phone=emergency_phone,
            subscription_id=subscription_id or None,
            routing_mode=routing_mode,
        )
        log_event("number_provisioned", business=business, twilio_number=bought.phone_number)
        return bought.phone_number
    except TwilioException as exc:
        log_event("number_provisioning_failed", business=business, error=str(exc))
        return None


def release_client_number(twilio_number: str, business: str):
    """Releases a churned client's real Twilio number back to Twilio
    (stops the ~$1.15/mo charge) and removes their row from the clients
    table. Never raises: if Twilio can't find or release the number for
    any reason, that's logged and Krish is told to clean it up by hand —
    a billing-side hiccup here should never crash the cancellation
    webhook. Looks the number up by phone_number rather than trusting a
    stored SID, since none was ever stored."""
    if twilio_client:
        try:
            matches = twilio_client.incoming_phone_numbers.list(phone_number=twilio_number, limit=1)
            if matches:
                matches[0].delete()
                log_event("number_released", business=business, twilio_number=twilio_number)
            else:
                log_event("number_release_not_found_in_twilio", business=business, twilio_number=twilio_number)
        except TwilioException as exc:
            log_event("number_release_failed", business=business, twilio_number=twilio_number, error=str(exc))
    delete_client(twilio_number)


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


ONBOARDING_FORM_URL = "https://onboarding.coldfront.studio/onboarding/"


def send_intake_form_link(phone: str):
    """The website half of the plan needs their logo, photos, services and
    hours, which the separate intake form collects. Nothing else ever told a
    paying customer that form exists."""
    try:
        send_sms(
            phone,
            "For your website: add your logo, a few job photos, your services and hours here "
            f"(sign in with the email you used at checkout): {ONBOARDING_FORM_URL}",
            TWILIO_NUMBER,
        )
        append_message(phone, "assistant", "[intake form link sent]")
    except TwilioException as exc:
        log_event("intake_form_link_failed", phone=phone, error=str(exc))


def send_growth_howto(owner_phone: str, client_number: str):
    client = get_client_row(client_number)
    if not client or not owner_phone:
        return
    try:
        send_sms(
            owner_phone,
            owner_help_text(client) + "\nStart with your review link: Google Business Profile > Ask for reviews > copy, "
            f"then text 'reviewlink <link>'.\nYour dashboard (sign in with this phone): {PORTAL_URL}",
            TWILIO_NUMBER,
        )
    except TwilioException as exc:
        log_event("growth_howto_failed", error=str(exc))


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
    business_phone_raw = info.get("business_phone", "")
    business_phone = _normalize_us_phone(business_phone_raw) or business_phone_raw or None

    # Extend the trial regardless of whether auto-provisioning succeeds
    # below — this was previously only called on the success path, which
    # meant the one customer whose setup needed Krish's manual help (the
    # exact case the fairness fix exists for) was the one who never got
    # it. Not perfect when it falls back to manual (their forwarding may
    # not actually go live the instant this fires), but "now + 7 days"
    # beats "never extended."
    extend_trial_to_full_term(thread.get("subscription_id", ""), business)

    new_number = provision_client_number(
        business, trade, forward_to, thread.get("subscription_id", ""),
        routing_mode=ROUTING_CARRIER_FORWARD, emergency_phone=business_phone,
    )
    if new_number:
        plan = plan_from_tier(thread.get("tier", ""))
        update_client_fields(new_number, plan=plan, timezone=DEFAULT_TIMEZONE)
        display_number = _format_phone_for_display(new_number)
        try:
            send_sms(
                FORWARD_TO,
                f"✅ Auto-provisioned {display_number} for {business or '—'} ({trade}). "
                f"Their business line: {_format_phone_for_display(business_phone) if business_phone else '—'}. "
                f"Lead alerts go to {_format_phone_for_display(forward_to)}.\n"
                "THEY STILL NEED TO: forward unanswered calls on their business line to "
                f"{display_number}. They were told to reply with their carrier; send them the exact "
                "steps and test-call their line once it's on. Text-back activates once A2P clears.",
                TWILIO_NUMBER,
            )
        except TwilioException as exc:
            log_event("auto_provision_owner_notify_failed", phone=phone, error=str(exc))
        try:
            send_sms(
                phone,
                f"You're almost set! Your ColdFront number is {display_number}. You keep your current "
                "business number; customers never see this one. One last step: set your business line to "
                f"forward UNANSWERED calls to {display_number} (most phone companies do this with a short "
                "dial code or a setting in your phone or VoIP app). Reply with your phone company's name "
                "and Krish will text you the exact steps. Text-back switches on once carrier registration "
                f"clears, usually within a day or two. Your {TRIAL_LENGTH_DAYS}-day trial starts counting "
                "from right now, not from when you paid.",
                TWILIO_NUMBER,
            )
            append_message(phone, "assistant", "[auto-provisioning confirmation sent]")
            send_intake_form_link(phone)
            if plan in (PLAN_GROWTH, PLAN_FULL):
                send_growth_howto(forward_to, new_number)
        except TwilioException as exc:
            log_event("auto_provision_customer_notify_failed", phone=phone, error=str(exc))
    else:
        notify_owner_ready_to_add_client(phone, thread, info)
        send_intake_form_link(phone)


def _defer_complete_onboarding(phone: str, thread: dict, info: dict, delay: float = 6.0):
    def _run():
        try:
            complete_onboarding(phone, thread, info)
        except Exception as exc:  # never lose a paid customer's setup silently
            log_event("complete_onboarding_failed", phone=phone, error=str(exc))
            try:
                alert_owner_of_system_failure("onboarding_setup_failed", f"Setup failed for {phone}: {exc}")
            except Exception:
                pass
    t = threading.Timer(delay, _run)
    t.daemon = True
    t.start()


def ai_onboarding_reply(phone: str, incoming_text: str, thread: dict) -> str:
    append_message(phone, "user", incoming_text)
    history = get_history(phone)
    while history and history[0]["role"] != "user":
        history.pop(0)  # the API needs the first turn to be the user's
    history = history[-30:]
    while history and history[0]["role"] != "user":
        history.pop(0)

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
                    # Run setup a few seconds AFTER this reply is delivered, so the customer reads
                    # "next text will have your last step" before the setup texts arrive, not after.
                    _defer_complete_onboarding(phone, thread, dict(block.input))
            reply_text = reply_text.strip() or "Got it, thanks!"
        except APIError as exc:
            log_event("onboarding_ai_reply_failed", phone=phone, error=str(exc))
            alert_owner_of_system_failure(
                "anthropic_api_failure", "AI onboarding for a paying customer is failing."
            )
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
            alert_owner_of_system_failure("anthropic_api_failure", "The missed-call AI text-back is failing.")
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
    if not (to_number and get_client_row(to_number)) and get_demo_override(caller):
        # A preloaded/registered prospect trying the demo line. Ringing
        # Krish's cell first just makes them wait (and if he picks up by
        # habit, the demo never happens), so text right away.
        call_sid = request.values.get("CallSid", "")
        if consented and (not call_sid or claim_request_once(f"voice_status:{call_sid}")):
            send_missed_call_text(caller, client)
            vr.say("Thanks. Check your phone, we just texted you. Reply to see how it works.")
        else:
            vr.say("No problem. To try the demo, call back and press 1 when asked.")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    if client.get("routing_mode") == ROUTING_CARRIER_FORWARD:
        # The business's own line already rang out before the carrier sent
        # this call here, so don't ring anyone again — text now (if they
        # pressed 1) and take a voicemail, since forwarding unanswered calls
        # here means their old voicemail never gets them.
        call_sid = request.values.get("CallSid", "")
        if not call_sid or not _request_key_seen(f"missed_call_event:{call_sid}"):
            if call_sid:
                claim_request_once(f"missed_call_event:{call_sid}")
            record_event(client["twilio_number"], "missed_call", caller)
        if consented and (not call_sid or claim_request_once(f"voice_status:{call_sid}")):
            send_missed_call_text(caller, client)
        lead_in = "We just sent you a text. " if consented else ""
        vr.say(f"{lead_in}To leave a voicemail instead, speak after the tone, then hang up.")
        vr.record(
            max_length=120,
            play_beep=True,
            action=f"{PUBLIC_BASE_URL}/voice/voicemail-done",
            method="POST",
            recording_status_callback=f"{PUBLIC_BASE_URL}/voice/voicemail",
            recording_status_callback_method="POST",
        )
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

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
def send_missed_call_text(caller: str, client: dict):
    body = (
        f"Hey! Sorry we missed your call at {client['business_name']}. "
        f"What can we help with? (Reply here and we'll get right back to you. Reply STOP to opt out.)"
    )
    try:
        send_sms(caller, body, client["twilio_number"])
        log_event("missed_call_text_sent", caller=caller)
        record_event(client["twilio_number"], "missed_call_texted", caller)
    except TwilioException as exc:
        log_event("missed_call_text_failed", caller=caller, error=str(exc))


@app.route("/voice/voicemail-done", methods=["POST"])
@validate_twilio_request
def voice_voicemail_done():
    # <Record>'s action: without it Twilio re-requests the same TwiML and
    # the caller hears the prompt again in a loop.
    vr = VoiceResponse()
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/voice/voicemail", methods=["POST"])
@validate_twilio_request
def voice_voicemail():
    """Recording finished: text the owner the voicemail link."""
    caller = request.values.get("From", "unknown")
    to_number = request.values.get("To", "")
    recording_sid = request.values.get("RecordingSid", "")
    recording_url = request.values.get("RecordingUrl", "")
    try:
        duration = int(request.values.get("RecordingDuration", "0") or 0)
    except ValueError:
        duration = 0
    if recording_sid and not claim_request_once(f"voicemail:{recording_sid}"):
        return Response("", status=204)
    client = resolve_client(to_number, caller)
    if duration < 2 or not recording_url or not client["forward_to"]:
        log_event("voicemail_skipped", caller=caller, duration=duration)
        return Response("", status=204)
    try:
        send_sms(
            client["forward_to"],
            f"📞 New voicemail ({duration}s) for {client['business_name']}\n"
            f"From: {_format_phone_for_display(caller)}\n"
            f"Listen: {recording_url}.mp3",
            client["twilio_number"],
        )
        log_event("voicemail_owner_notified", caller=caller, duration=duration)
        record_event(client["twilio_number"], "voicemail", caller, str(duration))
    except TwilioException as exc:
        log_event("voicemail_owner_notify_failed", caller=caller, error=str(exc))
    return Response("", status=204)


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
    if call_status in ("no-answer", "busy", "failed", "canceled") and (not call_sid or not _request_key_seen(f"voice_status:{call_sid}")):
        record_event(client["twilio_number"], "missed_call", caller)
    if call_status in ("no-answer", "busy", "failed", "canceled"):
        # Twilio retries this webhook if it doesn't get a timely response
        # (cold-start delay on the free tier is a real way to trigger that).
        # Without this check, a retry would text the caller a second time.
        if call_sid and not claim_request_once(f"voice_status:{call_sid}"):
            log_event("voice_status_duplicate_retry_skipped", caller=caller, call_sid=call_sid)
        elif has_consent(caller):
            send_missed_call_text(caller, client)
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

    # The business owner texting their OWN ColdFront number from their cell
    # is a command ("done 4045551234 John"), not a customer conversation.
    # Checked first: an owner also has an onboarding thread from signup.
    owner_client = get_client_row(to_number) if to_number else None
    if owner_client and _same_phone(caller, owner_client["forward_to"]):
        resp = MessagingResponse()
        resp.message(handle_owner_command(owner_client, body))
        log_event("owner_command", client=to_number, command=body[:40])
        return Response(str(resp), mimetype="text/xml")

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
        try:
            reply_text = ai_onboarding_reply(caller, body, onboarding_thread)
        except Exception as exc:  # noqa: BLE001 - a paying customer must always get an answer
            log_event("onboarding_reply_crashed", caller=caller, error=f"{exc.__class__.__name__}: {exc}")
            reply_text = "Thanks! Got your message. Krish will text you back shortly."
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

    is_demo_line = not (to_number and get_client_row(to_number))

    if is_demo_line and normalized in DEMO_SETUP_KEYWORDS and _request_key_seen(f"demo_wrapup:{caller}"):
        reply_text = start_sales_from_demo(caller)
        resp.message(reply_text)
        return Response(str(resp), mimetype="text/xml")

    if not is_demo_line:
        client_row = get_client_row(to_number)
        direct = handle_customer_reply(client_row, caller, body, normalized) if client_row else None
        if direct:
            resp.message(direct)
            return Response(str(resp), mimetype="text/xml")

    state: dict = {}
    reply_text = ai_reply(caller, body, client, state)
    log_event("ai_reply_sent", caller=caller, reply=reply_text)

    resp.message(reply_text)
    if is_demo_line:
        wrapup = maybe_demo_wrapup(caller, state.get("captured", False))
        if wrapup:
            resp.message(wrapup)
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

        if action == "release":
            if twilio_number:
                row = get_client_row(twilio_number)
                release_client_number(twilio_number, (row or {}).get("business_name", ""))
                message = f"Removed {twilio_number} and released it back to Twilio (stops its monthly charge)."
        elif action == "delete":
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
            routing_mode = request.form.get("routing_mode", "")
            if routing_mode not in (ROUTING_RING_FIRST, ROUTING_CARRIER_FORWARD):
                routing_mode = None  # keep whatever the client already has
            growth_fields = {k: request.form.get(k, "").strip() for k in ("plan", "timezone", "review_link", "place_id")}
            growth_fields = {k: v for k, v in growth_fields.items() if v}
            if growth_fields.get("timezone"):
                try:
                    ZoneInfo(growth_fields["timezone"])
                except Exception:  # noqa: BLE001
                    growth_fields.pop("timezone")

            if not (twilio_number and business_name and business_trade and forward_to):
                message = "Twilio number, business name, trade, and forward-to number are all required."
            else:
                upsert_client(
                    twilio_number, business_name, business_trade, forward_to,
                    emergency_phone, ring_timeout_seconds, routing_mode=routing_mode,
                )
                update_client_fields(twilio_number, **growth_fields)
                message = f"Saved {business_name} ({twilio_number}). Point that number's " \
                          f"Twilio webhooks at this app and it's live — no deploy needed."

    clients = list_clients()
    if clients:
        rows_html = "".join(
            f"""<tr>
                <td>{c['business_name']}</td><td>{c['twilio_number']}</td>
                <td>{c['business_trade']}</td><td>{c['forward_to']}</td>
                <td>{c['emergency_phone'] or '—'}</td>
                <td>{'Keeps own number' if c.get('routing_mode') == ROUTING_CARRIER_FORWARD else 'Rings first'}<br>
                    {PLAN_LABELS.get(c.get('plan') or '', 'Plan not set')} ·
                    <a href="/admin/pipeline?client={c['twilio_number'].replace('+', '%2B')}">pipeline</a></td>
                <td><form method="POST" style="display:inline" onsubmit="return confirm('Remove {c['business_name']}?')">
                    <input type="hidden" name="action" value="delete">
                    <input type="hidden" name="twilio_number" value="{c['twilio_number']}">
                    <button type="submit">Remove</button></form>
                    <form method="POST" style="display:inline" onsubmit="return confirm('Remove {c['business_name']} AND release {c['twilio_number']} back to Twilio? This cannot be undone.')">
                    <input type="hidden" name="action" value="release">
                    <input type="hidden" name="twilio_number" value="{c['twilio_number']}">
                    <button type="submit">Remove + release number</button></form></td>
            </tr>"""
            for c in clients
        )
    else:
        rows_html = (
            f"<tr><td colspan='7'>No per-client rows yet — "
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
<nav><a href="/admin/users">Manage admin logins &rarr;</a> &nbsp; <a href="/admin/pipeline">Pipelines</a> &nbsp; <a href="/admin/reviews">Google reviews</a> &nbsp; <a href="/admin/content">Monthly content</a></nav>
<h1>Clients</h1>
{'<p class="msg">' + message + '</p>' if message else ''}
<table><tr><th>Business</th><th>Twilio #</th><th>Trade</th><th>Forward to</th><th>Emergency #</th><th>Calls</th><th></th></tr>
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
<label>How calls reach this number</label>
<select name="routing_mode" style="width:100%;padding:.5rem;margin-top:.2rem">
<option value="">(keep current setting)</option>
<option value="{ROUTING_CARRIER_FORWARD}">Keeps own number: their line forwards UNANSWERED calls here (text right away + voicemail)</option>
<option value="{ROUTING_RING_FIRST}">Customers call this number: ring the forward-to phone first, text if no answer</option>
</select>
<label>Plan</label>
<select name="plan" style="width:100%;padding:.5rem;margin-top:.2rem">
<option value="">(keep current)</option><option value="starter">Starter</option><option value="growth">Growth</option><option value="full">Full-Service</option>
</select>
<label>Timezone (optional, e.g. America/Chicago; defaults to {DEFAULT_TIMEZONE})</label>
<input name="timezone">
<label>Google review link (optional — the owner can also text "reviewlink ...")</label>
<input name="review_link">
<label>Google place ID (optional — turns on daily review monitoring)</label>
<input name="place_id">
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


MAX_BULK_DEMO_OVERRIDES = 5000


@app.route("/admin/demo-overrides", methods=["POST"])
@require_admin_auth
def admin_bulk_demo_overrides():
    """Bulk version of /register-demo-caller, for cold email batches.

    The outreach page only registers a prospect when they open their link,
    but the email tells them to call the demo line straight away — so anyone
    who calls without clicking first got the generic demo. Uploading every
    lead in a batch before it's sent means the demo answers as their business
    whether or not they ever open the page.

    Body: {"leads": [{"business": "...", "trade": "...", "phone": "..."}, ...]}
    """
    payload = request.get_json(silent=True) or {}
    leads = payload.get("leads")
    if not isinstance(leads, list):
        return {"error": 'expected JSON body {"leads": [...]}'}, 400
    if len(leads) > MAX_BULK_DEMO_OVERRIDES:
        return {"error": f"max {MAX_BULK_DEMO_OVERRIDES} leads per request"}, 400

    now = datetime.datetime.utcnow().isoformat()
    rows, skipped = {}, 0
    for lead in leads:
        if not isinstance(lead, dict):
            skipped += 1
            continue
        business = _clean_text(str(lead.get("business", "")), 120)
        trade = _clean_text(str(lead.get("trade", "")), 60)
        phone = _normalize_us_phone(str(lead.get("phone", "")))
        if business and trade and phone:
            rows[phone] = (phone, business, trade, now)  # last one wins on dupes
        else:
            skipped += 1

    if rows:
        with closing(get_db()) as conn:
            conn.executemany(
                "INSERT INTO demo_overrides (phone, business, trade, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(phone) DO UPDATE SET business=excluded.business, trade=excluded.trade, "
                "created_at=excluded.created_at",
                list(rows.values()),
            )
            conn.commit()

    log_event("demo_overrides_bulk_loaded", loaded=len(rows), skipped=skipped)
    return {"loaded": len(rows), "skipped": skipped}


@app.route("/email-unsubscribe", methods=["POST", "OPTIONS"])
def email_unsubscribe():
    """The coldfront.studio/unsubscribe page posts here, so opt-outs land in
    a list the email scripts pull automatically (suppress.py sync) instead
    of depending on someone reading an inbox."""
    if request.method == "OPTIONS":
        resp = Response("", status=204)
    elif _rate_limited(f"email-unsub:{request.remote_addr}"):
        resp = Response("", status=429)
    else:
        email = _clean_text(request.form.get("email", ""), 200).lower()
        if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            with closing(get_db()) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS email_unsubscribes (email TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
                conn.execute("INSERT OR IGNORE INTO email_unsubscribes (email, created_at) VALUES (?, ?)",
                             (email, datetime.datetime.utcnow().isoformat()))
                conn.commit()
            log_event("email_unsubscribed")
            resp = Response("", status=204)
        else:
            resp = Response("", status=400)
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_SITE_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/admin/email-unsubscribes.csv", methods=["GET"])
@require_admin_auth
def admin_email_unsubscribes():
    with closing(get_db()) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS email_unsubscribes (email TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        rows = conn.execute("SELECT email, created_at FROM email_unsubscribes ORDER BY created_at").fetchall()
    body = "email,date\n" + "".join(f"{r['email']},{r['created_at'][:10]}\n" for r in rows)
    return Response(body, mimetype="text/csv")


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

    elif event["type"] == "customer.subscription.deleted":
        # Fires when a subscription is actually canceled (not just past
        # due) — the signal that a client has genuinely churned. Without
        # this, a churned client's real Twilio number keeps billing ~$1.15/mo
        # forever with nobody using it. Only acts if we can match the
        # subscription back to a client row; an unmatched subscription
        # (e.g. one from before this was tracked) is logged, not guessed at.
        subscription = event["data"]["object"]
        client = get_client_by_subscription(subscription.get("id", ""))
        if client:
            release_client_number(client["twilio_number"], client["business_name"])
            try:
                send_sms(
                    FORWARD_TO,
                    f"🔌 {client['business_name']} canceled — released their number "
                    f"({_format_phone_for_display(client['twilio_number'])}) and removed them from active clients.",
                    TWILIO_NUMBER,
                )
            except TwilioException as exc:
                log_event("churn_release_notify_failed", business=client["business_name"], error=str(exc))
        else:
            log_event("subscription_deleted_no_matching_client", subscription_id=subscription.get("id", ""))

    return Response("", status=204)


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "business": BUSINESS_NAME, "time": str(datetime.datetime.now())}


@app.route("/health/deep", methods=["GET"])
def health_deep():
    """Unlike `/`, this actually exercises the DB and Twilio credentials —
    the two things that are free to check on every call. Anthropic is
    deliberately NOT checked here: a real completion call costs money on
    every single poll, which would quietly burn the exact API balance this
    is meant to protect. Anthropic failures are instead caught and alerted
    on on the real customer-facing call path (see alert_owner_of_system_failure).
    """
    checks = {}
    ok = True

    try:
        with closing(get_db()) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        ok = False

    if twilio_client:
        try:
            twilio_client.api.accounts(TWILIO_ACCOUNT_SID).fetch()
            checks["twilio"] = "ok"
        except TwilioException as exc:
            checks["twilio"] = f"error: {exc}"
            ok = False
    else:
        checks["twilio"] = "not configured"
        ok = False

    checks["anthropic_key_present"] = bool(ai_client)
    checks["stripe_key_present"] = bool(STRIPE_SECRET_KEY)

    return {"status": "ok" if ok else "degraded", "checks": checks}, (200 if ok else 503)


# =====================================================================
# GROWTH / FULL-SERVICE FEATURES
#
# Everything here is driven by the business owner texting commands to
# their own ColdFront number from their cell (the client's forward_to),
# e.g. "done 4045551234 John" -> ask that customer for a Google review.
# A background thread sends anything scheduled (quote follow-ups,
# appointment reminders, review requests held for quiet hours) and runs
# the monthly report + daily review check.
# =====================================================================
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/New_York")
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
PARSE_MODEL = os.environ.get("PARSE_MODEL", "claude-haiku-5-5")

# Texts to a client's customers only go out between these local hours
# (recipient's business timezone). "On our way" is the one exception: it's
# sent the moment the owner asks, since they're literally driving over.
SEND_WINDOW_START_HOUR = 9
SEND_WINDOW_END_HOUR = 20
QUOTE_FOLLOWUP_DAYS = (2, 5, 10)
QUOTE_FOLLOWUP_HOUR = 10
REVIEW_REQUEST_REPEAT_DAYS = 90  # don't ask the same customer twice in this window

PLAN_STARTER, PLAN_GROWTH, PLAN_FULL = "starter", "growth", "full"
PLAN_LABELS = {PLAN_STARTER: "Starter", PLAN_GROWTH: "Growth", PLAN_FULL: "Full-Service"}
CONFIRM_KEYWORDS = {"C", "CONFIRM", "CONFIRMED", "CONFIRMING"}

PIPELINE_STAGES = [
    ("missed_call", "Missed call"),
    ("missed_call_texted", "Texted back"),
    ("customer_text", "Replied"),
    ("lead_captured", "Lead captured"),
    ("quote_started", "Quoted"),
    ("quote_replied", "Replied to quote"),
    ("appt_scheduled", "Booked"),
    ("appt_rescheduled", "Booked"),
    ("appt_confirmed", "Confirmed"),
    ("review_request_sent", "Review requested"),
]
STAGE_RANK = {k: i for i, (k, _) in enumerate(PIPELINE_STAGES)}


def init_growth_db():
    with closing(get_db()) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS client_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_number TEXT NOT NULL,
                kind TEXT NOT NULL,
                contact TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_client_events ON client_events (client_number, created_at)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_number TEXT NOT NULL,
                to_phone TEXT NOT NULL,
                kind TEXT NOT NULL,
                body TEXT NOT NULL,
                send_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                sent_at TEXT,
                error TEXT
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_messages (status, send_at)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS seen_reviews (
                client_number TEXT NOT NULL,
                review_key TEXT NOT NULL,
                rating INTEGER,
                author TEXT,
                text TEXT,
                published TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (client_number, review_key)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS review_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_number TEXT NOT NULL,
                rating REAL,
                review_count INTEGER,
                checked_at TEXT NOT NULL
            )"""
        )
        for col in ("plan", "timezone", "review_link", "place_id"):
            try:
                conn.execute(f"ALTER TABLE clients ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.commit()


init_growth_db()


# ---------------------------------------------------------------- helpers

def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def client_tz(client: dict) -> ZoneInfo:
    try:
        return ZoneInfo(client.get("timezone") or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - bad timezone typed into admin
        return ZoneInfo(DEFAULT_TIMEZONE)


def _in_send_window(local: datetime.datetime) -> bool:
    return SEND_WINDOW_START_HOUR <= local.hour < SEND_WINDOW_END_HOUR


def _next_send_window(local: datetime.datetime) -> datetime.datetime:
    if _in_send_window(local):
        return local
    start = local.replace(hour=SEND_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    return start if local.hour < SEND_WINDOW_START_HOUR else start + datetime.timedelta(days=1)


def _fmt_when(local: datetime.datetime) -> str:
    return local.strftime("%a, %b %-d at %-I:%M %p")


def _same_phone(a: str, b: str) -> bool:
    na, nb = _normalize_us_phone(a or ""), _normalize_us_phone(b or "")
    return bool(na and nb and na == nb)


def plan_has_growth(client: dict) -> bool:
    # Clients added by hand before plans existed have no plan set: allow.
    return (client.get("plan") or PLAN_GROWTH) in (PLAN_GROWTH, PLAN_FULL)


def plan_from_tier(tier: str) -> str:
    t = (tier or "").lower()
    if "growth" in t:
        return PLAN_GROWTH
    if "full" in t:
        return PLAN_FULL
    return PLAN_STARTER


def update_client_fields(twilio_number: str, **fields):
    allowed = {k: v for k, v in fields.items() if k in ("plan", "timezone", "review_link", "place_id")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with closing(get_db()) as conn:
        conn.execute(
            f"UPDATE clients SET {sets}, updated_at = ? WHERE twilio_number = ?",
            (*allowed.values(), datetime.datetime.utcnow().isoformat(), twilio_number),
        )
        conn.commit()


def record_event(client_number: str, kind: str, contact: str = "", detail: str = ""):
    if not client_number:
        return
    try:
        with closing(get_db()) as conn:
            conn.execute(
                "INSERT INTO client_events (client_number, kind, contact, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (client_number, kind, contact or "", detail or "", _iso(_utcnow())),
            )
            conn.commit()
    except sqlite3.Error as exc:  # reporting must never break a call or text
        log_event("record_event_failed", kind=kind, error=str(exc))


def is_opted_out(phone: str) -> bool:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT consented FROM sms_consent WHERE phone = ?", (phone,)).fetchone()
        return bool(row) and not row["consented"]


def schedule_message(client_number: str, to_phone: str, kind: str, body: str, send_at: datetime.datetime) -> int:
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_messages (client_number, to_phone, kind, body, send_at, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (client_number, to_phone, kind, body, _iso(send_at), _iso(_utcnow())),
        )
        conn.commit()
        return cur.lastrowid


def cancel_scheduled(client_number: str, to_phone: str, kind_prefix: str = "") -> int:
    with closing(get_db()) as conn:
        cur = conn.execute(
            "UPDATE scheduled_messages SET status = 'canceled' WHERE client_number = ? AND to_phone = ? "
            "AND status = 'pending' AND kind LIKE ?",
            (client_number, to_phone, f"{kind_prefix}%"),
        )
        conn.commit()
        return cur.rowcount


def pending_count(client_number: str, to_phone: str, kind_prefix: str) -> int:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_messages WHERE client_number = ? AND to_phone = ? "
            "AND status = 'pending' AND kind LIKE ?",
            (client_number, to_phone, f"{kind_prefix}%"),
        ).fetchone()
        return row["n"]


def last_event(client_number: str, contact: str, kind: str) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM client_events WHERE client_number = ? AND contact = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1",
            (client_number, contact, kind),
        ).fetchone()
        return dict(row) if row else None


def upcoming_appointment(client_number: str, contact: str) -> datetime.datetime | None:
    evs = [e for e in (last_event(client_number, contact, "appt_scheduled"),
                       last_event(client_number, contact, "appt_rescheduled")) if e]
    ev = max(evs, key=lambda e: e["id"]) if evs else None
    if not ev or not ev["detail"]:
        return None
    try:
        when = _parse_iso(ev["detail"])
    except ValueError:
        return None
    return when if when > _utcnow() - datetime.timedelta(hours=2) else None


# ---------------------------------------------------------------- owner commands

def owner_help_text(client: dict) -> str:
    return (
        f"ColdFront commands (text them to {_format_phone_for_display(client['twilio_number'])} from this phone):\n"
        "done 4045551234 John - ask for a Google review\n"
        "quote 4045551234 water heater $2,400 - follow up on a quote\n"
        "appt 4045551234 tue 2pm John - confirm + remind them\n"
        "omw 4045551234 20 - on our way (minutes optional)\n"
        "stop 4045551234 - cancel their follow-ups/reminders\n"
        "report - this month's numbers\n"
        "reviewlink https://... - your Google review link"
    )


_PHONE_IN_TEXT = re.compile(r"(\+?1?[\s\-.(]*\d{3}[\s\-.)]*\d{3}[\s\-.]*\d{4})")


def _split_command(body: str):
    text = (body or "").strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower().strip(":,.")
    phone, m = None, _PHONE_IN_TEXT.search(rest)
    if m:
        phone = _normalize_us_phone(m.group(1))
        if phone:
            rest = (rest[: m.start()] + " " + rest[m.end():])
    return cmd, phone, " ".join(rest.split())


def handle_owner_command(client: dict, body: str) -> str:
    cmd, phone, rest = _split_command(body)
    number = client["twilio_number"]
    biz = client["business_name"]
    growth_cmds = {"done", "review", "quote", "appt", "appointment", "omw", "report"}

    if cmd in growth_cmds and not plan_has_growth(client):
        return (
            "That's part of the Growth plan (review requests, quote follow-ups, reminders, monthly report). "
            "Text Krish at ColdFront if you'd like to upgrade."
        )

    if cmd == "reviewlink":
        link = rest.strip()
        if not link.lower().startswith("http"):
            return ("Send it like: reviewlink https://g.page/r/... (find it in your Google Business Profile "
                    "under 'Ask for reviews').")
        update_client_fields(number, review_link=link)
        record_event(number, "review_link_set", "", link)
        return "Saved. Review requests will use that link."

    if cmd == "report":
        return build_report(client, *month_bounds(client, _utcnow()), month_to_date=True)

    if cmd in ("done", "review", "quote", "appt", "appointment", "omw", "stop") and not phone:
        return f"I need the customer's number, e.g. '{cmd} 4045551234 ...'"

    if phone and _same_phone(phone, number):
        return "That's your ColdFront number. Use the customer's number."

    if cmd in ("done", "review"):
        return owner_review_request(client, phone, rest)
    if cmd == "quote":
        return owner_quote(client, phone, rest)
    if cmd in ("appt", "appointment"):
        return owner_appointment(client, phone, rest)
    if cmd == "omw":
        return owner_on_my_way(client, phone, rest)
    if cmd == "stop":
        n = cancel_scheduled(number, phone)
        record_event(number, "followups_canceled", phone, str(n))
        return (f"Canceled {n} scheduled text{'s' if n != 1 else ''} to {_format_phone_for_display(phone)}."
                if n else f"Nothing scheduled for {_format_phone_for_display(phone)}.")
    return owner_help_text(client)


def _queue_or_send_note(local_send: datetime.datetime, now_local: datetime.datetime) -> str:
    if local_send <= now_local + datetime.timedelta(minutes=1):
        return "now"
    return f"at {local_send.strftime('%-I:%M %p')} {'tomorrow' if local_send.date() > now_local.date() else 'today'} (quiet hours)"


def review_link_from_place(place_id: str | None) -> str:
    place_id = (place_id or "").strip()
    return f"https://search.google.com/local/writereview?placeid={urllib.parse.quote(place_id)}" if place_id else ""


def find_place_id(client: dict) -> str | None:
    """Look the business up on Google by name and match on a phone number we
    have for it. Only accepts an exact phone match, so it never guesses."""
    if not GOOGLE_PLACES_API_KEY:
        return None
    phones = {p for p in (_normalize_us_phone(client.get(k) or "") for k in ("emergency_phone", "twilio_number", "forward_to")) if p}
    body = json.dumps({"textQuery": client["business_name"], "maxResultCount": 10}).encode()
    req = urllib.request.Request(
        "https://places.googleapis.com/v1/places:searchText", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                 "X-Goog-FieldMask": "places.id,places.nationalPhoneNumber,places.internationalPhoneNumber"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            places = json.loads(r.read().decode()).get("places") or []
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        log_event("place_lookup_failed", client=client["twilio_number"], error=str(exc))
        return None
    for pl in places:
        for key in ("nationalPhoneNumber", "internationalPhoneNumber"):
            if _normalize_us_phone(pl.get(key) or "") in phones:
                return pl.get("id")
    return None


def owner_review_request(client: dict, phone: str, name: str) -> str:
    number, display = client["twilio_number"], _format_phone_for_display(phone)
    link = (client.get("review_link") or "").strip() or review_link_from_place(client.get("place_id"))
    if not link:
        return ("Set your Google review link first: text 'reviewlink https://g.page/r/...' "
                "(Google Business Profile > Ask for reviews > copy link).")
    if is_opted_out(phone):
        return f"{display} opted out of texts, so I can't message them."
    prev = last_event(number, phone, "review_request_sent")
    if prev and _parse_iso(prev["created_at"]) > _utcnow() - datetime.timedelta(days=REVIEW_REQUEST_REPEAT_DAYS):
        return f"Already asked {display} for a review on {_parse_iso(prev['created_at']).astimezone(client_tz(client)):%b %-d}, so I'll skip it."
    if pending_count(number, phone, "review_request"):
        return f"A review request to {display} is already queued."
    first = name.split()[0].title() if name else ""
    body = (
        f"Hi{' ' + first if first else ''}, thanks for choosing {client['business_name']}! "
        f"If you have a minute, a quick Google review would mean a lot to us: {link} "
        "(Reply STOP to opt out)"
    )
    tz = client_tz(client)
    now_local = _utcnow().astimezone(tz)
    when = _next_send_window(now_local)
    schedule_message(number, phone, "review_request", body, when)
    record_event(number, "review_request_queued", phone, first)
    return f"👍 Review request to {display} going out {_queue_or_send_note(when, now_local)}."


def owner_quote(client: dict, phone: str, desc: str) -> str:
    number, display = client["twilio_number"], _format_phone_for_display(phone)
    if is_opted_out(phone):
        return f"{display} opted out of texts, so I can't message them."
    cancel_scheduled(number, phone, "quote_followup")
    what = desc or "your project"
    biz = client["business_name"]
    bodies = [
        f"Hi, it's {biz}. Just checking you got our quote for {what}. Any questions? Reply here anytime. (Reply STOP to opt out)",
        f"Hi again from {biz}. Still happy to help with {what} whenever you're ready. Want us to get you on the schedule?",
        f"Last check-in from {biz} about {what}. If now's not the time, no worries. Just reply here if you'd like to go ahead.",
    ]
    tz = client_tz(client)
    today = _utcnow().astimezone(tz)
    days = []
    for d, body in zip(QUOTE_FOLLOWUP_DAYS, bodies):
        local = (today + datetime.timedelta(days=d)).replace(hour=QUOTE_FOLLOWUP_HOUR, minute=0, second=0, microsecond=0)
        schedule_message(number, phone, f"quote_followup_{d}", body, local)
        days.append(local.strftime("%a %b %-d"))
    record_event(number, "quote_started", phone, what)
    return (f"👍 I'll follow up with {display} about {what} on {', '.join(days)} at "
            f"{QUOTE_FOLLOWUP_HOUR}am, and stop as soon as they reply. Text 'stop {re.sub(r'[^0-9]', '', phone)[-10:]}' to cancel.")


APPT_PARSE_PROMPT = """Now is {now} in the {tz} timezone. A business owner texted this about a customer appointment:
"{text}"

Extract the appointment's local date and time, and the customer's first name if one is given.
Reply with ONLY JSON: {{"datetime": "YYYY-MM-DDTHH:MM" or null, "name": "First" or null}}
Use the next upcoming occurrence for weekday names. If no clear time is given, use null for datetime."""


def parse_appointment(text: str, tz: ZoneInfo) -> tuple[datetime.datetime | None, str]:
    now_local = _utcnow().astimezone(tz)
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})", text)
    if m:
        try:
            dt = datetime.datetime.fromisoformat(f"{m.group(1)}T{m.group(2).zfill(5)}").replace(tzinfo=tz)
            rest = (text[: m.start()] + text[m.end():]).strip()
            return dt, (rest.split()[0].title() if rest else "")
        except ValueError:
            pass
    if not ai_client:
        return None, ""
    try:
        resp = ai_client.messages.create(
            model=PARSE_MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": APPT_PARSE_PROMPT.format(
                now=now_local.strftime("%A %Y-%m-%d %H:%M"), tz=str(tz), text=text[:300])}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except (APIError, AttributeError, ValueError) as exc:
        log_event("appt_parse_failed", error=str(exc))
        return None, ""
    name = (data.get("name") or "").strip().split(" ")[0].title() if data.get("name") else ""
    if not data.get("datetime"):
        return None, name
    try:
        return datetime.datetime.fromisoformat(data["datetime"]).replace(tzinfo=tz), name
    except ValueError:
        return None, name


def owner_appointment(client: dict, phone: str, rest: str) -> str:
    number, display = client["twilio_number"], _format_phone_for_display(phone)
    if is_opted_out(phone):
        return f"{display} opted out of texts, so I can't message them."
    tz = client_tz(client)
    when, name = parse_appointment(rest, tz)
    now_local = _utcnow().astimezone(tz)
    if not when:
        return "I couldn't tell the time. Try: appt 4045551234 tue 2pm John (or 2026-10-28 14:00)."
    if when <= now_local or when > now_local + datetime.timedelta(days=120):
        return f"That reads as {_fmt_when(when)}, which isn't in the next 4 months. Double-check the date?"
    biz = client["business_name"]
    hi = f"Hi {name}" if name else "Hi"
    replaced = cancel_scheduled(number, phone, "appt_") > 0 or upcoming_appointment(number, phone) is not None
    confirm_at = _next_send_window(now_local)
    if confirm_at < when:
        schedule_message(number, phone, "appt_confirm",
                         f"{hi}, you're booked with {biz} for {_fmt_when(when)}. Reply C to confirm, or reply "
                         "here if you need to change it. (Reply STOP to opt out)", confirm_at)
    sent = ["confirmation"]
    day_before = when - datetime.timedelta(hours=24)
    if day_before > now_local + datetime.timedelta(hours=1) and _in_send_window(day_before):
        schedule_message(number, phone, "appt_reminder_24h",
                         f"Reminder: {biz} is scheduled for tomorrow, {_fmt_when(when)}. Reply C to confirm or "
                         "reply here to reschedule.", day_before)
        sent.append("day-before reminder")
    two_before = when - datetime.timedelta(hours=2)
    if two_before > now_local + datetime.timedelta(minutes=30) and _in_send_window(two_before):
        schedule_message(number, phone, "appt_reminder_2h",
                         f"Reminder: {biz} will see you today at {when.strftime('%-I:%M %p')}. Reply here if "
                         "anything changes.", two_before)
        sent.append("2-hour reminder")
    # A re-booking of the same customer isn't a new appointment for the report.
    record_event(number, "appt_rescheduled" if replaced else "appt_scheduled", phone, _iso(when))
    return f"👍 Booked {display} for {_fmt_when(when)}. They'll get: {', '.join(sent)}. Text 'stop {re.sub(r'[^0-9]', '', phone)[-10:]}' to cancel."


def owner_on_my_way(client: dict, phone: str, rest: str) -> str:
    number, display = client["twilio_number"], _format_phone_for_display(phone)
    if is_opted_out(phone):
        return f"{display} opted out of texts, so I can't message them."
    m = re.search(r"\d{1,3}", rest or "")
    eta = f" and should be there in about {int(m.group(0))} minutes" if m else ""
    body = f"Hi, it's {client['business_name']}. We're on our way{eta}. See you soon! (Reply STOP to opt out)"
    try:
        send_sms(phone, body, number)
        append_message(phone, "assistant", body)
        record_event(number, "omw_sent", phone, m.group(0) if m else "")
        return f"👍 Told {display} you're on the way."
    except TwilioException as exc:
        log_event("omw_send_failed", phone=phone, error=str(exc))
        return f"That text to {display} didn't go through ({exc.__class__.__name__})."


# ---------------------------------------------------------------- customer replies

def handle_customer_reply(client: dict, caller: str, body: str, normalized: str) -> str | None:
    """Returns a direct reply if this text was fully handled here (an
    appointment confirmation); otherwise None and the AI replies as usual."""
    number = client["twilio_number"]
    record_event(number, "customer_text", caller)
    appt = upcoming_appointment(number, caller)
    if appt and normalized in CONFIRM_KEYWORDS:
        record_event(number, "appt_confirmed", caller, _iso(appt))
        _owner_ping(client, f"✅ {_format_phone_for_display(caller)} confirmed their appointment "
                            f"({_fmt_when(appt.astimezone(client_tz(client)))}).")
        reply = "Thanks, you're confirmed! See you then."
        append_message(caller, "user", body)
        append_message(caller, "assistant", reply)
        return reply
    if pending_count(number, caller, "quote_followup"):
        cancel_scheduled(number, caller, "quote_followup")
        record_event(number, "quote_replied", caller)
        _owner_ping(client, f"💬 {_format_phone_for_display(caller)} replied to your quote follow-up: \"{body[:300]}\"")
    elif appt:
        _owner_ping(client, f"💬 {_format_phone_for_display(caller)} (appointment {_fmt_when(appt.astimezone(client_tz(client)))}) "
                            f"texted: \"{body[:300]}\"")
    return None


def _owner_ping(client: dict, text: str):
    if not client.get("forward_to"):
        return
    try:
        send_sms(client["forward_to"], text, client["twilio_number"])
    except TwilioException as exc:
        log_event("owner_ping_failed", error=str(exc))


# ---------------------------------------------------------------- monthly report

def month_bounds(client: dict, when_utc: datetime.datetime, previous: bool = False):
    local = when_utc.astimezone(client_tz(client))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if previous:
        end = start
        start = (start - datetime.timedelta(days=1)).replace(day=1)
        return start, end
    nxt = (start + datetime.timedelta(days=32)).replace(day=1)
    return start, nxt


def _count(conn, number, kind, start, end, distinct=False):
    sel = "COUNT(DISTINCT contact)" if distinct else "COUNT(*)"
    return conn.execute(
        f"SELECT {sel} AS n FROM client_events WHERE client_number = ? AND kind = ? AND created_at >= ? AND created_at < ?",
        (number, kind, _iso(start), _iso(end)),
    ).fetchone()["n"]


def build_report(client: dict, start: datetime.datetime, end: datetime.datetime, month_to_date: bool = False) -> str:
    number = client["twilio_number"]
    with closing(get_db()) as conn:
        c = lambda kind, distinct=False: _count(conn, number, kind, start, end, distinct)  # noqa: E731
        missed, texted = c("missed_call"), c("missed_call_texted")
        convos, leads, vms = c("customer_text", True), c("lead_captured"), c("voicemail")
        quotes, quote_replies = c("quote_started"), c("quote_replied")
        appts, confirmed = c("appt_scheduled"), c("appt_confirmed")
        reviews_asked = c("review_request_sent")
        snaps = conn.execute(
            "SELECT rating, review_count, checked_at FROM review_snapshots WHERE client_number = ? "
            "AND checked_at >= ? AND checked_at < ? ORDER BY id",
            (number, _iso(start), _iso(end)),
        ).fetchall()
    label = start.strftime("%B %Y") + (" so far" if month_to_date else "")
    lines = [
        f"📊 {client['business_name']}, {label}",
        f"Missed calls caught: {missed} (texted back: {texted})",
        f"Callers who texted back: {convos}",
        f"Leads captured: {leads}",
        f"Voicemails: {vms}",
    ]
    if plan_has_growth(client):
        lines += [
            f"Quotes followed up: {quotes} (replied: {quote_replies})",
            f"Appointments booked: {appts} (confirmed: {confirmed})",
            f"Review requests sent: {reviews_asked}",
        ]
    if snaps:
        first, last = snaps[0], snaps[-1]
        gained = (last["review_count"] or 0) - (first["review_count"] or 0)
        lines.append(f"Google: {last['rating']} stars, {last['review_count']} reviews"
                     + (f" (+{gained} this month)" if gained > 0 else ""))
    return "\n".join(lines)


def send_monthly_report(client: dict, now_utc: datetime.datetime):
    start, end = month_bounds(client, now_utc, previous=True)
    _owner_ping(client, build_report(client, start, end) + f"\nDetails: {PORTAL_URL}")
    record_event(client["twilio_number"], "monthly_report_sent", "", start.strftime("%Y-%m"))


# ---------------------------------------------------------------- review monitoring
# Stopgap until Google approves Business Profile API access: the Places API
# returns the overall rating/count plus up to 5 reviews Google picks as most
# relevant (not necessarily the newest), so a new review can be missed.

def fetch_place_reviews(place_id: str) -> dict:
    req = urllib.request.Request(
        f"https://places.googleapis.com/v1/places/{urllib.parse.quote(place_id)}",
        headers={"X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                 "X-Goog-FieldMask": "rating,userRatingCount,reviews,googleMapsUri"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def check_reviews(client: dict, fetch=None) -> int:
    """Store a rating snapshot, alert the owner about new reviews of 3 stars
    or less. The first check for a client seeds silently. Returns the
    number of new reviews seen."""
    number, place_id = client["twilio_number"], (client.get("place_id") or "").strip()
    if not place_id:
        return 0
    try:
        data = (fetch or fetch_place_reviews)(place_id)
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        log_event("review_check_failed", client=number, error=str(exc))
        return 0
    now = _iso(_utcnow())
    new = 0
    with closing(get_db()) as conn:
        first_run = conn.execute(
            "SELECT COUNT(*) AS n FROM seen_reviews WHERE client_number = ?", (number,)
        ).fetchone()["n"] == 0
        conn.execute(
            "INSERT INTO review_snapshots (client_number, rating, review_count, checked_at) VALUES (?, ?, ?, ?)",
            (number, data.get("rating"), data.get("userRatingCount"), now),
        )
        alerts = []
        for rv in data.get("reviews") or []:
            key = rv.get("name") or f"{(rv.get('authorAttribution') or {}).get('displayName')}|{rv.get('publishTime')}"
            text = ((rv.get("text") or rv.get("originalText") or {}).get("text") or "").strip()
            author = (rv.get("authorAttribution") or {}).get("displayName") or "Someone"
            cur = conn.execute(
                "INSERT OR IGNORE INTO seen_reviews (client_number, review_key, rating, author, text, published, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (number, key, rv.get("rating"), author, text[:1000], rv.get("publishTime", ""), now),
            )
            if cur.rowcount:
                new += 1
                if not first_run and (rv.get("rating") or 5) <= 3:
                    alerts.append((rv.get("rating"), author, text))
        conn.commit()
    for rating, author, text in alerts:
        _owner_ping(client, f"⚠️ New {rating}-star Google review for {client['business_name']} from {author}: "
                            f"\"{text[:220]}\"\nReplying quickly and politely helps. "
                            + (data.get("googleMapsUri") or ""))
        record_event(number, "negative_review_alert", "", str(rating))
    return new


# ---------------------------------------------------------------- background sender

def run_due_messages(now: datetime.datetime | None = None) -> int:
    now = now or _utcnow()
    with closing(get_db()) as conn:
        due = [dict(r) for r in conn.execute(
            "SELECT * FROM scheduled_messages WHERE status = 'pending' AND send_at <= ? ORDER BY send_at LIMIT 50",
            (_iso(now),),
        ).fetchall()]
    sent = 0
    for msg in due:
        with closing(get_db()) as conn:
            claimed = conn.execute(
                "UPDATE scheduled_messages SET status = 'sending' WHERE id = ? AND status = 'pending'", (msg["id"],)
            ).rowcount
            conn.commit()
        if not claimed:
            continue
        status, error = "sent", None
        client = get_client_row(msg["client_number"])
        if not client:
            status = "canceled"
        elif is_opted_out(msg["to_phone"]):
            status = "skipped_opt_out"
        else:
            local = now.astimezone(client_tz(client))
            if not _in_send_window(local):
                if msg["kind"].startswith("appt_reminder"):
                    status = "skipped_quiet_hours"  # late (e.g. after downtime); don't send a stale reminder at night
                else:
                    with closing(get_db()) as conn:
                        conn.execute("UPDATE scheduled_messages SET status = 'pending', send_at = ? WHERE id = ?",
                                     (_iso(_next_send_window(local)), msg["id"]))
                        conn.commit()
                    continue
            else:
                try:
                    send_sms(msg["to_phone"], msg["body"], client["twilio_number"])
                    append_message(msg["to_phone"], "assistant", msg["body"])
                    kind = "review_request" if msg["kind"] == "review_request" else msg["kind"]
                    record_event(client["twilio_number"], f"{kind}_sent", msg["to_phone"])
                    sent += 1
                except TwilioException as exc:
                    status, error = "failed", str(exc)[:500]
        with closing(get_db()) as conn:
            conn.execute("UPDATE scheduled_messages SET status = ?, sent_at = ?, error = ? WHERE id = ?",
                         (status, _iso(_utcnow()), error, msg["id"]))
            conn.commit()
    return sent


def run_periodic_jobs(now: datetime.datetime | None = None):
    now = now or _utcnow()
    try:
        run_monthly_content(now)
    except Exception as exc:  # noqa: BLE001 - never block reports/reviews
        log_event("monthly_content_error", error=str(exc))
    for client in list_clients():
        # Every plan gets the monthly numbers (it's what keeps Starter
        # clients seeing the value); review monitoring is Growth+.
        local = now.astimezone(client_tz(client))
        if local.day == 1 and local.hour >= 9:
            prev = month_bounds(client, now, previous=True)[0].strftime("%Y-%m")
            if claim_request_once(f"monthly_report:{client['twilio_number']}:{prev}"):
                send_monthly_report(client, now)
        if (GOOGLE_PLACES_API_KEY and not client.get("place_id")
                and claim_request_once(f"place_lookup:{client['twilio_number']}:{local.date().isoformat()}")):
            found = find_place_id(client)
            if found:
                update_client_fields(client["twilio_number"], place_id=found)
                client = get_client_row(client["twilio_number"])
                log_event("place_id_found", client=client["twilio_number"])
        if (plan_has_growth(client) and client.get("place_id") and GOOGLE_PLACES_API_KEY
                and local.hour >= 10
                and claim_request_once(f"review_check:{client['twilio_number']}:{local.date().isoformat()}")):
            check_reviews(client)


_scheduler_started = False
_scheduler_lock = threading.Lock()


def _scheduler_loop():
    last_periodic = 0.0
    time.sleep(20)  # let the rest of the module finish loading before the first run
    while True:
        try:
            run_due_messages()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
            log_event("scheduler_send_error", error=str(exc))
        if time.time() - last_periodic > 300:
            last_periodic = time.time()
            try:
                run_periodic_jobs()
            except Exception as exc:  # noqa: BLE001
                log_event("scheduler_periodic_error", error=str(exc))
        time.sleep(30)


def start_scheduler():
    global _scheduler_started
    if os.environ.get("SCHEDULER_ENABLED", "1") != "1":
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    threading.Thread(target=_scheduler_loop, name="coldfront-scheduler", daemon=True).start()
    log_event("scheduler_started")


start_scheduler()


# ---------------------------------------------------------------- admin pages

def _esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


_ADMIN_CSS = """<style>
body{font-family:-apple-system,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}
table{width:100%;border-collapse:collapse;margin-bottom:2rem;font-size:.85rem}
td,th{border:1px solid #ddd;padding:.45rem .6rem;text-align:left;vertical-align:top}
th{background:#f5f5f5}nav a{font-size:.85rem;color:#555;margin-right:1rem}.muted{color:#777}
button{padding:.35rem .8rem;cursor:pointer}
</style>"""


def pipeline_table_html(client: dict, limit: int = 200) -> tuple[str, str]:
    """Each customer's furthest stage, newest activity first. Returns
    (table html, one-line summary)."""
    number = client["twilio_number"]
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT contact, kind, detail, created_at FROM client_events WHERE client_number = ? AND contact != '' "
            "ORDER BY id", (number,),
        ).fetchall()
    tz = client_tz(client)
    contacts: dict[str, dict] = {}
    for r in rows:
        c = contacts.setdefault(r["contact"], {"stage": None, "last": r["created_at"], "notes": []})
        if r["kind"] in STAGE_RANK and (c["stage"] is None or STAGE_RANK[r["kind"]] >= STAGE_RANK[c["stage"]]):
            c["stage"] = r["kind"]
        c["last"] = r["created_at"]
        if r["kind"] == "quote_started" and r["detail"]:
            c["notes"].append(f"Quote: {r['detail']}")
        if r["kind"] in ("appt_scheduled", "appt_rescheduled") and r["detail"]:
            c["notes"].append("Appt " + _fmt_when(_parse_iso(r["detail"]).astimezone(tz)))
    stage_names = dict(PIPELINE_STAGES)
    ordered = sorted(contacts.items(), key=lambda kv: kv[1]["last"], reverse=True)
    counts: dict = {}
    for _, c in ordered:
        counts[stage_names.get(c["stage"], "Other")] = counts.get(stage_names.get(c["stage"], "Other"), 0) + 1
    summary = " · ".join(f"{k}: {v}" for k, v in counts.items())
    body = "".join(
        f"<tr><td>{_esc(_format_phone_for_display(p))}</td><td>{_esc(stage_names.get(c['stage'], '—'))}</td>"
        f"<td>{_esc(' · '.join(c['notes'][-2:]))}</td>"
        f"<td class='muted'>{_parse_iso(c['last']).astimezone(tz):%b %-d, %-I:%M %p}</td></tr>"
        for p, c in ordered[:limit]
    ) or "<tr><td colspan='4'>No activity yet.</td></tr>"
    table = ("<table><tr><th>Customer</th><th>Furthest stage</th><th>Notes</th><th>Last activity</th></tr>"
             f"{body}</table>")
    return table, summary


def reviews_html(client: dict) -> str:
    number = client["twilio_number"]
    with closing(get_db()) as conn:
        snap = conn.execute("SELECT * FROM review_snapshots WHERE client_number = ? ORDER BY id DESC LIMIT 1",
                            (number,)).fetchone()
        recent = conn.execute("SELECT * FROM seen_reviews WHERE client_number = ? ORDER BY published DESC LIMIT 5",
                              (number,)).fetchall()
    head = (f"{snap['rating']} stars, {snap['review_count']} reviews · checked "
            f"{_parse_iso(snap['checked_at']).astimezone(client_tz(client)):%b %-d %-I:%M %p}") if snap else "Not checked yet"
    items = "".join(
        f"<tr><td>{'★' * int(r['rating'] or 0)}</td><td>{_esc(r['author'])}</td><td>{_esc((r['text'] or '')[:300])}</td>"
        f"<td class='muted'>{_esc((r['published'] or '')[:10])}</td></tr>" for r in recent
    )
    return (f"<p class='muted'>{_esc(head)}</p><table><tr><th>Rating</th><th>Author</th><th>Review</th><th>Date</th></tr>"
            f"{items or '<tr><td colspan=4>None stored yet.</td></tr>'}</table>")


@app.route("/admin/pipeline", methods=["GET"])
@require_admin_auth
def admin_pipeline():
    number = request.args.get("client", "")
    client = get_client_row(number) if number else None
    if not client:
        links = "".join(
            f"<li><a href='/admin/pipeline?client={urllib.parse.quote(c['twilio_number'])}'>{_esc(c['business_name'])}</a></li>"
            for c in list_clients()
        ) or "<li>No clients yet.</li>"
        return f"<html><head><title>Pipeline</title>{_ADMIN_CSS}</head><body><nav><a href='/admin/clients'>&larr; Clients</a></nav><h1>Pipeline</h1><ul>{links}</ul></body></html>"
    table, summary = pipeline_table_html(client)
    return f"""<html><head><title>Pipeline — {_esc(client['business_name'])}</title>{_ADMIN_CSS}</head><body>
<nav><a href='/admin/clients'>&larr; Clients</a><a href='/admin/pipeline'>All pipelines</a></nav>
<h1>{_esc(client['business_name'])} pipeline</h1><p class='muted'>{_esc(summary) or 'No activity yet.'}</p>
{table}
<h2>This month</h2><pre>{_esc(build_report(client, *month_bounds(client, _utcnow()), month_to_date=True))}</pre>
</body></html>"""


@app.route("/admin/reviews", methods=["GET", "POST"])
@require_admin_auth
def admin_reviews():
    message = ""
    if request.method == "POST":
        client = get_client_row(request.form.get("client", ""))
        if client and not GOOGLE_PLACES_API_KEY:
            message = "Add GOOGLE_PLACES_API_KEY on Render first."
        elif client:
            n = check_reviews(client)
            message = f"Checked {client['business_name']}: {n} review(s) not seen before."
    sections = []
    for client in list_clients():
        if not client.get("place_id"):
            continue
        number = client["twilio_number"]
        sections.append(
            f"<h2>{_esc(client['business_name'])}</h2>"
            f"<form method='POST'><input type='hidden' name='client' value='{_esc(number)}'><button>Check now</button></form>"
            + reviews_html(client)
        )
    note = ("" if GOOGLE_PLACES_API_KEY else
            "<p><b>GOOGLE_PLACES_API_KEY isn't set on Render, so automatic checks are off.</b></p>")
    return f"""<html><head><title>Reviews</title>{_ADMIN_CSS}</head><body>
<nav><a href='/admin/clients'>&larr; Clients</a></nav><h1>Google reviews</h1>{note}
{'<p>' + _esc(message) + '</p>' if message else ''}
<p class='muted'>Checked daily. Google only shares up to 5 reviews per business through this method, so this is a
partial view until Business Profile API access is approved. Owners get a text for new reviews of 3 stars or less.</p>
{''.join(sections) or '<p>No clients have a Google place ID set. Add one on the Clients page.</p>'}
</body></html>"""


# =====================================================================
# OWNER PORTAL — a business owner signs in with a code texted to the
# cell phone on file for them (their forward_to), no password, and sees
# their own pipeline, Google reviews, this month's numbers and commands.
# =====================================================================
import hmac
import secrets
from itsdangerous import BadSignature, URLSafeTimedSerializer

PORTAL_COOKIE = "cf_portal"
PORTAL_SESSION_DAYS = 30
PORTAL_CODE_MINUTES = 10
PORTAL_CODE_MAX_ATTEMPTS = 5


def _portal_secret() -> str:
    """Signing key for portal cookies, generated once and kept on the
    persistent disk so sign-ins survive redeploys. No env var to manage."""
    with closing(get_db()) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'portal_secret'").fetchone()
        if row:
            return row["value"]
        value = secrets.token_hex(32)
        conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('portal_secret', ?)", (value,))
        conn.commit()
        return conn.execute("SELECT value FROM app_settings WHERE key = 'portal_secret'").fetchone()["value"]


def init_portal_db():
    with closing(get_db()) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS portal_codes (
                phone TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.commit()


init_portal_db()
_portal_signer = URLSafeTimedSerializer(_portal_secret(), salt="coldfront-owner-portal")


def clients_for_owner(phone: str) -> list[dict]:
    return [c for c in list_clients() if _same_phone(c.get("forward_to", ""), phone)]


def _hash_code(phone: str, code: str) -> str:
    return hmac.new(_portal_secret().encode(), f"{phone}:{code}".encode(), "sha256").hexdigest()


def portal_owner_phone() -> str | None:
    token = request.cookies.get(PORTAL_COOKIE, "")
    if not token:
        return None
    try:
        phone = _portal_signer.loads(token, max_age=PORTAL_SESSION_DAYS * 86400)
    except BadSignature:
        return None
    return phone if clients_for_owner(phone) else None


_PORTAL_CSS = """<style>
:root{color-scheme:light}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:860px;margin:0 auto;padding:1.25rem 1rem 3rem;color:#16161d;background:#fafafa}
h1{font-size:1.4rem;margin:.2rem 0 1rem}h2{font-size:1.1rem;margin:2rem 0 .6rem}
.card{background:#fff;border:1px solid #e6e6ea;border-radius:10px;padding:1rem 1.1rem;margin-bottom:1rem}
table{width:100%;border-collapse:collapse;font-size:.85rem}td,th{border-bottom:1px solid #eee;padding:.5rem .4rem;text-align:left;vertical-align:top}
th{color:#666;font-weight:600}.muted{color:#777;font-size:.85rem}pre{white-space:pre-wrap;font:inherit;margin:0}
input{font-size:1rem;padding:.65rem;border:1px solid #ccc;border-radius:8px;width:100%;box-sizing:border-box;margin:.4rem 0 .8rem}
button{font-size:1rem;padding:.65rem 1.2rem;border:0;border-radius:8px;background:#2f4cff;color:#fff;cursor:pointer}
.top{display:flex;justify-content:space-between;align-items:center}.top a{color:#555;font-size:.9rem}
.wrap{overflow-x:auto}code{background:#f1f1f4;padding:.1rem .3rem;border-radius:4px}
</style>"""


def _portal_page(title: str, inner: str) -> Response:
    html = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_esc(title)} · ColdFront</title>{_PORTAL_CSS}</head><body>{inner}</body></html>")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _portal_login_form(message: str = "", phone: str = "", code_step: bool = False) -> Response:
    if code_step:
        form = (f"<form method='POST' action='/portal/verify'><input type='hidden' name='phone' value='{_esc(phone)}'>"
                "<label>6-digit code</label><input name='code' inputmode='numeric' autocomplete='one-time-code' maxlength='6' autofocus>"
                "<button>Sign in</button></form>")
    else:
        form = ("<form method='POST' action='/portal/login'><label>Your cell number (the one ColdFront texts your leads to)</label>"
                "<input name='phone' type='tel' autocomplete='tel' autofocus><button>Text me a code</button></form>")
    return _portal_page("Sign in", f"<h1>ColdFront: sign in</h1><div class='card'>"
                        f"{'<p>' + _esc(message) + '</p>' if message else ''}{form}</div>")


@app.route("/portal", methods=["GET"])
def portal_home():
    phone = portal_owner_phone()
    if not phone:
        return _portal_login_form()
    sections = []
    for client in clients_for_owner(phone):
        table, summary = pipeline_table_html(client, limit=100)
        report = build_report(client, *month_bounds(client, _utcnow()), month_to_date=True)
        review_block = (f"<h2>Google reviews</h2><div class='card wrap'>{reviews_html(client)}"
                        "<p class='muted'>You'll get a text for any new review of 3 stars or less.</p></div>"
                        if client.get("place_id") else "")
        commands = (f"<h2>Text commands</h2><div class='card'><pre>{_esc(owner_help_text(client))}</pre></div>"
                    if plan_has_growth(client) else "")
        sections.append(
            f"<h1>{_esc(client['business_name'])}</h1>"
            f"<div class='card'><pre>{_esc(report)}</pre></div>"
            f"<h2>Your customers</h2><p class='muted'>{_esc(summary) or 'No activity yet.'}</p>"
            f"<div class='card wrap'>{table}</div>{review_block}{commands}"
        )
    top = "<div class='top'><span class='muted'>ColdFront</span><a href='/portal/logout'>Sign out</a></div>"
    return _portal_page("Your dashboard", top + "".join(sections))


@app.route("/portal/login", methods=["POST"])
def portal_login():
    phone = _normalize_us_phone(request.form.get("phone", "")) or ""
    generic = "If that number is on file, we just texted it a 6-digit code."
    if not phone:
        return _portal_login_form("Enter a 10-digit US cell number.")
    if _rate_limited(f"portal-login-ip:{request.remote_addr}") or _rate_limited(f"portal-login-phone:{phone}"):
        return _portal_login_form("Too many tries. Wait 10 minutes and try again.")
    if clients_for_owner(phone):
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = _iso(_utcnow() + datetime.timedelta(minutes=PORTAL_CODE_MINUTES))
        with closing(get_db()) as conn:
            conn.execute(
                "INSERT INTO portal_codes (phone, code_hash, expires_at, attempts) VALUES (?, ?, ?, 0) "
                "ON CONFLICT(phone) DO UPDATE SET code_hash=excluded.code_hash, expires_at=excluded.expires_at, attempts=0",
                (phone, _hash_code(phone, code), expires),
            )
            conn.commit()
        try:
            send_sms(phone, f"Your ColdFront sign-in code is {code}. It expires in {PORTAL_CODE_MINUTES} minutes.", TWILIO_NUMBER)
            log_event("portal_code_sent", phone=phone)
        except TwilioException as exc:
            log_event("portal_code_send_failed", phone=phone, error=str(exc))
    else:
        log_event("portal_login_unknown_phone")
    return _portal_login_form(generic, phone, code_step=True)


@app.route("/portal/verify", methods=["POST"])
def portal_verify():
    phone = _normalize_us_phone(request.form.get("phone", "")) or ""
    code = re.sub(r"\D", "", request.form.get("code", ""))
    bad = _portal_login_form("That code didn't work. Check it, or go back and request a new one.", phone, code_step=True)
    if not phone or len(code) != 6:
        return bad
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM portal_codes WHERE phone = ?", (phone,)).fetchone()
        if not row or row["attempts"] >= PORTAL_CODE_MAX_ATTEMPTS or _parse_iso(row["expires_at"]) < _utcnow():
            return bad
        conn.execute("UPDATE portal_codes SET attempts = attempts + 1 WHERE phone = ?", (phone,))
        conn.commit()
        if not hmac.compare_digest(row["code_hash"], _hash_code(phone, code)):
            return bad
        conn.execute("DELETE FROM portal_codes WHERE phone = ?", (phone,))
        conn.commit()
    resp = Response("", status=303, headers={"Location": "/portal"})
    resp.set_cookie(PORTAL_COOKIE, _portal_signer.dumps(phone), max_age=PORTAL_SESSION_DAYS * 86400,
                    httponly=True, secure=PUBLIC_BASE_URL.startswith("https"), samesite="Lax")
    log_event("portal_login", phone=phone)
    return resp


@app.route("/portal/logout", methods=["GET"])
def portal_logout():
    resp = Response("", status=303, headers={"Location": "/portal"})
    resp.delete_cookie(PORTAL_COOKIE)
    return resp


PORTAL_URL = f"{PUBLIC_BASE_URL.rstrip('/')}/portal"


# =====================================================================
# MONTHLY CONTENT PACK — cuts the manual part of Starter's "2-4 Google
# Business Profile posts a month" and Full-Service's monthly check-in.
# On the 1st, Claude drafts each client's posts (and, for Full-Service, a
# check-in agenda from last month's numbers). Krish reviews and pastes
# them at /admin/content. Posting itself stays manual until Google
# approves Business Profile API access.
# =====================================================================
CONTENT_MODEL = os.environ.get("CONTENT_MODEL", "claude-sonnet-5")
POSTS_PER_MONTH = 4

LOCAL_SEO_CHECKLIST = [
    "Business name, address and phone match exactly on Google, website, Facebook, Yelp, BBB, Angi, Nextdoor",
    "Primary Google category is the main trade; secondary categories cover each service offered",
    "Service areas list every city/neighborhood they actually serve",
    "At least 10 recent job photos uploaded this quarter (before/after, truck, team)",
    "Every review from the last month has an owner reply",
    "Q&A section answers the 3-5 questions customers ask most (hours, emergencies, service area, financing)",
    "Website has one page per main service and mentions the service area cities",
    "Hours (including holiday hours) are current",
]


def init_content_db():
    with closing(get_db()) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS content_packs (
                client_number TEXT NOT NULL,
                month TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (client_number, month, kind)
            )"""
        )
        conn.commit()


init_content_db()

GBP_POSTS_PROMPT = """Write {n} Google Business Profile posts for {business}, a {trade} business, for {month}.

Rules:
- Each post: a short title (max 8 words) and a body of 60-120 words, plain text, no hashtags, no emojis.
- Seasonal and practical: what homeowners need from a {trade} business in {month} (maintenance tips, warning signs, prep for the season), ending with a simple call to action to call or book.
- Never invent facts about the business: no prices, discounts, years in business, awards, guarantees, license numbers, staff names or claims like "#1" or "best in town".
- Vary the angle across posts (tip, warning sign, seasonal checklist, "when to call a pro").

Reply with ONLY JSON: [{{"title": "...", "body": "..."}}, ...]"""

CHECKIN_PROMPT = """You're preparing a 20-minute monthly check-in call agenda for {business} ({trade}), a client of a
missed-call text-back and local marketing service. Last month's numbers:

{report}

Write a short agenda in plain text:
1. Three bullets summarizing what happened, using ONLY the numbers above (don't invent any).
2. Two or three questions to ask the owner (e.g. how leads converted, busy/slow seasons, services to push).
3. Two concrete recommendations for next month based on the numbers (e.g. more review requests if few were sent).
Keep it under 180 words. No preamble."""


def _ai_text(prompt: str, max_tokens: int = 1500) -> str | None:
    if not ai_client:
        return None
    try:
        resp = ai_client.messages.create(model=CONTENT_MODEL, max_tokens=max_tokens,
                                         messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except APIError as exc:
        log_event("content_ai_failed", error=str(exc))
        alert_owner_of_system_failure("anthropic_api_failure", "Monthly content drafting is failing.")
        return None


def _save_content(number: str, month: str, kind: str, content: str):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO content_packs (client_number, month, kind, content, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_number, month, kind) DO UPDATE SET content=excluded.content, created_at=excluded.created_at",
            (number, month, kind, content, _iso(_utcnow())),
        )
        conn.commit()


def get_content(number: str, month: str, kind: str) -> str | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT content FROM content_packs WHERE client_number = ? AND month = ? AND kind = ?",
                           (number, month, kind)).fetchone()
        return row["content"] if row else None


def generate_gbp_posts(client: dict, month_start: datetime.datetime) -> list[dict]:
    raw = _ai_text(GBP_POSTS_PROMPT.format(n=POSTS_PER_MONTH, business=client["business_name"],
                                           trade=client["business_trade"], month=month_start.strftime("%B")))
    try:
        posts = json.loads(re.search(r"\[.*\]", raw or "", re.S).group(0))
        posts = [{"title": str(p["title"])[:80], "body": str(p["body"])[:1500]} for p in posts][:POSTS_PER_MONTH]
    except (AttributeError, ValueError, KeyError, TypeError):
        return []
    if posts:
        _save_content(client["twilio_number"], month_start.strftime("%Y-%m"), "gbp_posts", json.dumps(posts))
    return posts


def generate_checkin(client: dict, now_utc: datetime.datetime) -> str:
    start, end = month_bounds(client, now_utc, previous=True)
    text = _ai_text(CHECKIN_PROMPT.format(business=client["business_name"], trade=client["business_trade"],
                                          report=build_report(client, start, end)), max_tokens=600) or ""
    if text:
        _save_content(client["twilio_number"], month_bounds(client, now_utc)[0].strftime("%Y-%m"), "checkin", text)
    return text


def run_monthly_content(now_utc: datetime.datetime | None = None) -> int:
    """Called from the periodic jobs. Drafts each client's pack once per
    month (from the 1st, 8am local) and texts Krish when any are ready."""
    now_utc = now_utc or _utcnow()
    made = 0
    for client in list_clients():
        local = now_utc.astimezone(client_tz(client))
        if local.day > 7 or (local.day == 1 and local.hour < 8):
            continue  # only in the first week; a later deploy won't backfill old months
        month_start = month_bounds(client, now_utc)[0]
        key = f"content_pack:{client['twilio_number']}:{month_start:%Y-%m}"
        if _request_key_seen(key):
            continue
        # If drafting fails (API down, bad output), try again at most hourly,
        # not on every 5-minute loop for a week.
        if not claim_request_once(f"content_attempt:{client['twilio_number']}:{local:%Y-%m-%d-%H}"):
            continue
        posts = generate_gbp_posts(client, month_start)
        if (client.get("plan") or "") == PLAN_FULL:
            generate_checkin(client, now_utc)
        if posts:
            claim_request_once(key)
            made += 1
    if made and FORWARD_TO:
        try:
            send_sms(FORWARD_TO, f"📝 This month's Google posts are drafted for {made} client"
                                 f"{'s' if made != 1 else ''}. Review and post: {PUBLIC_BASE_URL.rstrip('/')}/admin/content",
                     TWILIO_NUMBER)
        except TwilioException as exc:
            log_event("content_ready_notify_failed", error=str(exc))
    return made


@app.route("/admin/content", methods=["GET", "POST"])
@require_admin_auth
def admin_content():
    now = _utcnow()
    message = ""
    if request.method == "POST":
        client = get_client_row(request.form.get("client", ""))
        if client:
            what = request.form.get("what", "posts")
            if what == "checkin":
                ok = bool(generate_checkin(client, now))
            else:
                ok = bool(generate_gbp_posts(client, month_bounds(client, now)[0]))
            message = f"{'Drafted' if ok else 'Could not draft'} {what} for {client['business_name']}."
    sections = []
    for client in list_clients():
        month = month_bounds(client, now)[0]
        number = client["twilio_number"]
        posts = json.loads(get_content(number, f"{month:%Y-%m}", "gbp_posts") or "[]")
        post_html = "".join(
            f"<div style='margin:.6rem 0'><b>{_esc(p['title'])}</b>"
            f"<textarea readonly rows='5' style='width:100%;font:inherit;margin-top:.3rem' onclick='this.select()'>"
            f"{_esc(p['body'])}</textarea></div>" for p in posts
        ) or "<p class='muted'>No drafts for this month yet.</p>"
        form = lambda what, label: (f"<form method='POST' style='display:inline'><input type='hidden' name='client' value='{_esc(number)}'>"  # noqa: E731
                                    f"<input type='hidden' name='what' value='{what}'><button>{label}</button></form> ")
        extra = ""
        if (client.get("plan") or "") == PLAN_FULL:
            agenda = get_content(number, f"{month:%Y-%m}", "checkin")
            extra = ("<h3>Monthly check-in agenda</h3>"
                     + (f"<pre style='white-space:pre-wrap'>{_esc(agenda)}</pre>" if agenda else "<p class='muted'>Not drafted yet.</p>")
                     + form("checkin", "Draft agenda")
                     + "<h3>Local SEO checklist</h3><ul>" + "".join(f"<li>{_esc(i)}</li>" for i in LOCAL_SEO_CHECKLIST) + "</ul>")
        sections.append(
            f"<h2>{_esc(client['business_name'])} <span class='muted'>({PLAN_LABELS.get(client.get('plan') or '', 'no plan set')}, {month:%B})</span></h2>"
            f"<h3>Google posts</h3>{post_html}{form('posts', 'Redraft posts')}{extra}"
        )
    return f"""<html><head><title>Monthly content</title>{_ADMIN_CSS}</head><body>
<nav><a href='/admin/clients'>&larr; Clients</a></nav><h1>Monthly content</h1>
{'<p>' + _esc(message) + '</p>' if message else ''}
<p class='muted'>Drafted automatically on the 1st. Click a post to select it, then paste it into the client's Google
Business Profile (Add update). Read each one first: the AI is told not to invent prices or claims, but check anyway.</p>
{''.join(sections) or '<p>No clients yet.</p>'}
</body></html>"""


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
