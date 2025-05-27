#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp tutor with fully dynamic NLU and ephemeral session context.
Features:
 - Onboarding (name collection)
 - Dynamic subject/topic extraction via Gemini with JSON cleaning + fallback
 - In-memory session store (per‐user, auto‐expires, no persistence across runs)
 - Free vs. Premium credit management
 - Interactive “Understood” / “Explain more” buttons
 - Robust error handling, logging
"""

# 1) Load environment (locally via .env or from Render dashboard)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os
import re
import json
import logging
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

# 2) Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("StudyMate")

# 3) Flask setup
app = Flask(__name__)

# 4) Required env vars
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))

missing = [k for k in ("VERIFY_TOKEN","ACCESS_TOKEN","PHONE_NUMBER_ID","GEMINI_API_KEY") if not os.getenv(k)]
if missing:
    logger.error(f"Missing environment variables: {', '.join(missing)}")
    raise SystemExit("Please set all required environment variables.")

# 5) Gemini init
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# 6) Firestore init (only for name & credits & last_prompt)
KEY_FILENAME = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH  = f"/etc/secrets/{KEY_FILENAME}"
cred_path    = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILENAME
cred         = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# 7) Load system prompt
with open("studymate_prompt.txt", "r") as f:
    BASE_PROMPT = f.read().strip()

# 8) In-memory session store
#    sessions[phone] = {"subject": str|None, "topic": str|None, "expiry": datetime}
sessions = {}

# — Helpers —

def safe_post(url: str, payload: dict):
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            json=payload
        )
        if resp.status_code != 200:
            logger.error(f"WhatsApp API {resp.status_code}: {resp.text}")
    except Exception:
        logger.exception("Failed to send WhatsApp message")

def send_whatsapp_message(phone: str, text: str):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    })

def send_interactive_buttons(phone: str):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense to you?"},
            "action": {"buttons": [
                {"type": "reply","reply": {"id":"understood","title":"Understood"}},
                {"type": "reply","reply": {"id":"explain_more","title":"Explain more"}}
            ]}
        }
    })

def strip_greeting(text: str) -> str:
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text,
                  flags=re.IGNORECASE).strip()

def get_gemini_reply(prompt: str) -> str:
    try:
        resp = model.generate_content(prompt)
        return strip_greeting(resp.text.strip())
    except Exception:
        logger.exception("Gemini error")
        return "Sorry, I encountered an internal error. Please try again."

def parse_subject_topic(text: str):
    """
    1. Ask Gemini to output JSON {subject, topic}.
    2. Strip code fences and extract {...}.
    3. On JSONParseError: fallback to separate subject-only and topic-only calls.
    """
    parser_prompt = (
        "Extract the academic 'subject' and 'topic' from this message.\n"
        "Respond with JSON ONLY, keys: 'subject', 'topic' (use null if missing).\n"
        f"User message: \"{text}\"\nJSON:"
    )
    raw = get_gemini_reply(parser_prompt)

    # clean markdown fences
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[\w]*|```$", "", cleaned).strip()

    # extract first {...}
    match = re.search(r"\{[\s\S]*\}", cleaned)
    json_str = match.group(0) if match else cleaned

    try:
        data = json.loads(json_str)
        subj = data.get("subject")
        top  = data.get("topic")
    except json.JSONDecodeError:
        logger.warning(f"parse_subject_topic JSON decode failed: {raw}")
        # fallback to subject-only
        subj_resp = get_gemini_reply(
            f"Identify the academic subject from: \"{text}\". Reply with the subject or null."
        )
        top_resp = get_gemini_reply(
            f"Identify the specific topic/task from: \"{text}\". Reply with the topic or null."
        )
        subj = subj_resp.splitlines()[0].strip().strip('"')
        top  = top_resp.splitlines()[0].strip().strip('"')
        subj = None if subj.lower()=="null" else subj
        top  = None if top.lower()=="null" else top

    return subj or None, top or None

def get_or_create_user(phone: str) -> dict:
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {
            "phone": phone,
            "name": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1),
            "last_prompt": None
        }
        ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone: str, **fields):
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated {phone}: {fields}")

# — Webhook —

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    # Verification handshake
    if request.method == "GET":
        if (request.args.get("hub.mode")=="subscribe" and
            request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # Incoming message
    data = request.json or {}
    entries = data.get("entry", [])
    if not entries or not entries[0].get("changes"):
        return "OK", 200

    entry = entries[0]["changes"][0].get("value", {})
    if "messages" not in entry:
        return "OK", 200

    msg   = entry["messages"][0]
    phone = msg.get("from")
    text  = msg.get("text", {}).get("body", "").strip()
    now   = datetime.utcnow()

    # Expire old session
    sess = sessions.get(phone)
    if sess and now > sess["expiry"]:
        del sessions[phone]
        sess = None

    user = get_or_create_user(phone)

    # 1) Onboarding: collect name
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            sessions[phone] = {"subject":None, "topic":None, "expiry": now + timedelta(hours=2)}
            send_whatsapp_message(phone,
                f"Nice to meet you, {text}! 🎓 What would you like to study today?")
        else:
            send_whatsapp_message(phone,
                "Please share your full name (first and last).")
        return "OK", 200

    # 2) Greeting starts a new session
    if text.lower() in ("hi","hello","hey"):
        first = user["name"].split()[0]
        sessions[phone] = {"subject":None, "topic":None, "expiry": now + timedelta(hours=2)}
        send_whatsapp_message(phone,
            f"Welcome back, {first}! 🎓 What would you like to study today?")
        return "OK", 200

    # Ensure session exists
    if phone not in sessions:
        sessions[phone] = {"subject":None, "topic":None, "expiry": now + timedelta(hours=2)}

    # 3) If session missing subject/topic, parse dynamically
    subj = sessions[phone]["subject"]
    top  = sessions[phone]["topic"]
    if subj is None or top is None:
        parsed_subj, parsed_top = parse_subject_topic(text)
        sessions[phone].update({
            "subject": subj or parsed_subj,
            "topic":   top  or parsed_top,
            "expiry":  now + timedelta(hours=2)
        })
        # ask next clarifying question or confirm both
        if sessions[phone]["subject"] and sessions[phone]["topic"]:
            send_whatsapp_message(phone,
                f"Great—*{sessions[phone]['topic']}* in *{sessions[phone]['subject']}*. How can I help with that?")
        elif sessions[phone]["subject"]:
            send_whatsapp_message(phone,
                f"Subject set to *{sessions[phone]['subject']}*. What specific topic or task? (e.g., essay, vocab)")
        else:
            send_whatsapp_message(phone,
                f"Topic set to *{sessions[phone]['topic']}*. Which broader subject is that under? (e.g., English, Math)")
        return "OK", 200

    # 4) Interactive button replies
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            btn = ir["button_reply"]["id"]
            if btn == "understood":
                send_whatsapp_message(phone, "Fantastic! 🎉 What’s next?")
            elif btn == "explain_more" and user.get("last_prompt"):
                detail = get_gemini_reply(user["last_prompt"] + "\n\nPlease explain in more detail.")
                send_whatsapp_message(phone, detail)
                send_interactive_buttons(phone)
            return "OK", 200

    # 5) Credit reset & usage for free users
    if user.get("account_type") == "free":
        rt = user.get("credit_reset")
        if hasattr(rt, "to_datetime"):
            rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo:
            rt = rt.replace(tzinfo=None)
        if isinstance(rt, datetime) and now >= rt:
            update_user(phone,
                        credit_remaining=20,
                        credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user.get("credit_remaining", 0) <= 0:
            send_whatsapp_message(phone,
                "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone,
                    credit_remaining=user["credit_remaining"] - 1)

    # 6) Academic Q&A: send everything to Gemini
    subj = sessions[phone]["subject"]
    top  = sessions[phone]["topic"]
    context = ""
    if subj:
        context += f"Subject: {subj}. "
    if top:
        context += f"Topic: {top}. "

    prompt = f"{BASE_PROMPT}\n{context}\nQuestion: {text}"
    update_user(phone, last_prompt=prompt)
    send_whatsapp_message(phone, "🤖 Thinking...")
    answer = get_gemini_reply(prompt)
    send_whatsapp_message(phone, answer)
    send_interactive_buttons(phone)

    return "OK", 200

# 11) Run server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
