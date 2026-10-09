# Missed-Call AI Responder — starter template

A working demo of the "never miss a lead" offer: incoming calls get forwarded
to the real business phone; if unanswered, the caller gets an instant text;
replies are handled by an AI assistant that tries to capture their need and
get them to a booked appointment.

## What's in here
- `app.py` — the Flask server (3 webhooks: `/voice`, `/voice/status`, `/sms`)
- `requirements.txt` — Python dependencies
- `.env.example` — the environment variables you need to set
- `Procfile` — tells Render/Railway how to run it
- `events.log` — created automatically; every call/text gets logged here,
  useful for screenshotting proof when you demo this to a prospect

## 1. Get the accounts you need
- **Twilio**: sign up at twilio.com, buy a phone number with Voice + SMS.
  Free trial credit covers testing.
- **Anthropic API key**: console.anthropic.com → API Keys. (You can swap in
  OpenAI instead if you prefer — just change the `ai_reply` function.)

## 2. Run it locally first
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
export $(cat .env | xargs)   # loads env vars into your shell (mac/linux)
python app.py
```
It'll start on `http://localhost:5000`. Twilio can't reach localhost
directly, so for local testing use a tunnel tool like `ngrok http 5000` and
use the ngrok URL as your `PUBLIC_BASE_URL` and in the Twilio webhook config
below.

## 3. Deploy it somewhere public (so Twilio can reach it)
Easiest free options: **Render.com** or **Railway.app**.
- Push this folder to a GitHub repo
- Create a new "Web Service" on Render, connect the repo
- Add all the variables from `.env.example` as environment variables in
  Render's dashboard (use your real values)
- Once deployed, copy the live URL (e.g. `https://missed-call-demo.onrender.com`)
  and set it as `PUBLIC_BASE_URL` in the environment variables too, then
  redeploy

## 4. Point your Twilio number at it
In the Twilio console → Phone Numbers → your number → **Voice & Fax**:
- "A call comes in" → Webhook → `https://your-app-url/voice` → HTTP POST

Under **Messaging**:
- "A message comes in" → Webhook → `https://your-app-url/sms` → HTTP POST

## 5. Test it
- Call your Twilio number from your own cell phone, let it ring out (don't
  answer on the `FORWARD_TO` phone) — you should get a text within seconds.
- Reply to that text — the AI should respond and start asking what you need.
- Check `events.log` on the server to see everything that happened.

## What to harden before using this on a real client's number
This is a demo-grade template — enough to prove the concept and get your
first pilot client, not a production system yet. Before relying on it:
- **Persistence**: conversation history is stored in memory (`CONVERSATIONS`
  dict) and resets if the server restarts — move to a real database
  (SQLite/Postgres) once you have a paying client.
- **Calendar booking**: the AI currently just *talks about* booking, it
  doesn't create a real calendar event. Add Google Calendar API (or a
  Zapier/Make webhook) in `ai_reply`/`sms` once the AI detects a confirmed
  time, to actually create the appointment.
- **Security**: verify Twilio's request signature on incoming webhooks
  (`twilio.request_validator`) so random people can't hit your endpoints
  and fake calls/texts.
- **Error handling**: wrap the Anthropic/Twilio calls in try/except so a
  single API hiccup doesn't break the flow for a real customer.
- **Opt-out handling**: add STOP/unsubscribe handling for SMS compliance
  before texting real customers at any volume.

## Customizing for a specific business
Change `BUSINESS_NAME`, `BUSINESS_TRADE`, and the `SYSTEM_PROMPT` text in
`app.py` to match whoever you're pitching — that's the whole "demo" step:
same code, different business framing.
