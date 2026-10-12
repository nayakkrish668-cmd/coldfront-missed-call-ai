# ColdFront Studio's own missed-call line

This is the same missed-call AI responder you sell to clients, deployed for
ColdFront Studio itself. When someone calls your own business number and you
miss it, they get auto-texted — and the AI qualifies the lead (business name,
trade) and tells them a personalized demo is coming, instead of generic
"we'll call you back" copy.

It's a separate deployment from the client template at the repo root, so
tweaking this one's sales pitch never touches client deployments.

## Deploy (Render, free tier)
1. On render.com, **New → Web Service**, connect this GitHub repo
   (`coldfront-missed-call-ai`).
2. Set **Root Directory** to `coldfront-studio-own`.
3. Build command: `pip install -r requirements.txt`
   Start command: `gunicorn app:app`
4. Add environment variables:
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` — from your Twilio console
   - `TWILIO_NUMBER` — the number you buy for ColdFront Studio, e.g. `+15551234567`
   - `FORWARD_TO` — your real cell number
   - `ANTHROPIC_API_KEY` — your Anthropic API key
   - `PUBLIC_BASE_URL` — fill in after first deploy (step 6)
   - `BUSINESS_NAME` — `ColdFront Studio` (already the default, optional to set)
5. Deploy. Copy the live URL Render gives you (e.g. `https://coldfront-studio-line.onrender.com`).
6. Set `PUBLIC_BASE_URL` to that URL in the env vars, redeploy.

## Point your new Twilio number at it
Twilio console → Phone Numbers → your number:
- **Voice & Fax** → "A call comes in" → Webhook → `https://<your-render-url>/voice` → HTTP POST
- **Messaging** → "A message comes in" → Webhook → `https://<your-render-url>/sms` → HTTP POST

## Test
Call the new number from your cell, let it ring out without answering on
`FORWARD_TO` — you should get a text within a few seconds. Reply and the AI
should ask for your business name/trade like a real prospect would see.
