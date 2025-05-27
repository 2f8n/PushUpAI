#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp tutor with fully dynamic NLU and ephemeral session context.
Features:
 - Onboarding (name collection)
 - Dynamic subject/topic extraction via Gemini for every message
 - In-memory session store (no persistent subject/topic)
 - Credit management (free vs. premium)
 - Interactive “Understood” / “Explain more” buttons
 - Robust error handling and logging
"""

# 1) Load environment (from .env locally or Render dashboard)
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
    raise SystemExit("Set all required environment variables.")

# 5) Gemini init
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# 6) Firestore init (only for name & credits)
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

# 9) Helper functions

def safe_post(url: str, payload: dict):
    try:
        resp = requests.post(url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload)
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
    Ask Gemini to extract subject & topic.
    Returns (subject:str|None, topic:str|None).
    """
    parser_prompt = (
        "Extract the ACADEMIC subject and SPECIFIC topic from this message.\n"
        "Respond with JSON: {\"subject\":..., \"topic\":...} (null if missing).\n"
        f"User: \"{text}\"\nJSON:"
    )
    resp = get_gemini_reply(parser_prompt)
    try:
        data = json.loads(resp)
        return data.get("subject"), data.get("topic")
    except Exception:
        logger.error(f"Parser JSON failed: {resp}")
        return None, None

def get_or_create_user(phone: str) -> dict:
    # Only name & credits are persistent
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {
            "phone": phone,
            "name": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1)
        }
        ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone: str, **fields):
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated {phone}: {fields}")

# 10) Webhook endpoint

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    # — Verification —
    if request.method == "GET":
        if (request.args.get("hub.mode")=="subscribe" and
            request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # — Handle incoming message —
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

    # Clear expired session
    sess = sessions.get(phone)
    if sess and now > sess["expiry"]:
        del sessions[phone]
        sess = None

    user = get_or_create_user(phone)

    # — Onboarding name —
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            # start new session
            sessions[phone] = {"subject":None, "topic":None, "expiry": now + timedelta(hours=2)}
            send_whatsapp_message(phone,
                f"Nice to meet you, {text}! 🎓 What would you like to study today?")
        else:
            send_whatsapp_message(phone,
                "Please share your full name (first and last).")
        return "OK", 200

    # — Greeting starts a new session —
    if text.lower() in ("hi","hello","hey"):
        first = user["name"].split()[0]
        sessions[phone] = {"subject":None, "topic":None, "expiry": now + timedelta(hours=2)}
        send_whatsapp_message(phone,
            f"Welcome back, {first}! 🎓 What would you like to study today?")
        return "OK", 200

    # ensure a session is active
    if phone not in sessions:
        sessions[phone] = {"subject":None, "topic":None, "expiry": now + timedelta(hours=2)}

    # — Dynamic subject/topic parsing —
    subj, top = parse_subject_topic(text)

    # If Gemini extracted both
    if subj and top:
        sessions[phone]["subject"] = subj
        sessions[phone]["topic"]   = top
        sessions[phone]["expiry"]  = now + timedelta(hours=2)
        send_whatsapp_message(phone,
            f"Great! We'll focus on *{top}* in *{subj}*. How can I help you with that?")
        return "OK", 200

    # If only subject
    if subj and not sessions[phone]["topic"]:
        sessions[phone]["subject"] = subj
        sessions[phone]["expiry"]  = now + timedelta(hours=2)
        send_whatsapp_message(phone,
            f"Your subject is *{subj}*. What specific topic or task are you working on? "
            "(e.g., essay, vocabulary, equations)")
        return "OK", 200

    # If only topic
    if top and not sessions[phone]["subject"]:
        sessions[phone]["topic"]   = top
        sessions[phone]["expiry"]  = now + timedelta(hours=2)
        send_whatsapp_message(phone,
            f"You're working on *{top}*. Which broader subject is this under? "
            "(e.g., English, Math, Chemistry)")
        return "OK", 200

    # — Interactive buttons —
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            btn = ir["button_reply"]["id"]
            if btn == "understood":
                send_whatsapp_message(phone,
                    "Fantastic! 🎉 What’s next on your study list?")
                return "OK", 200
            if btn == "explain_more" and user.get("last_prompt"):
                detail = get_gemini_reply(
                    user["last_prompt"] + "\n\nPlease explain in more detail.")
                send_whatsapp_message(phone, detail)
                send_interactive_buttons(phone)
                return "OK", 200

    # — Credit reset & usage for free users —
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
                "Free limit reached (20/day). Upgrade for unlimited access.")
            return "OK", 200
        update_user(phone,
                    credit_remaining=user["credit_remaining"] - 1)

    # — Academic Q&A (with in-session context) —
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
    ans = get_gemini_reply(prompt)
    send_whatsapp_message(phone, ans)
    send_interactive_buttons(phone)
    return "OK", 200

# 11) Run server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
