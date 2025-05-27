#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp-based academic tutor with dynamic NLU, context tracking, and interactive Q&A.
Features:
 - Onboarding (name collection)
 - Dynamic subject/topic extraction via Gemini
 - Persistent context in Firestore (subject, topic, project summary)
 - Training‐log for future NLU improvements
 - Free vs. Premium credit management
 - Interactive “Understood” / “Explain more” buttons
 - Robust error handling and logging
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
    format="%(asctime)s %(levelname)s %(name)s:%(lineno)d — %(message)s"
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

# 6) Firestore init
KEY_FILENAME = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH  = f"/etc/secrets/{KEY_FILENAME}"
cred_path    = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILENAME
cred         = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# 7) Load system prompt
with open("studymate_prompt.txt", "r") as f:
    BASE_PROMPT = f.read().strip()

# — Helpers —

def safe_post(url: str, payload: dict):
    """POST to WhatsApp API, log errors if any."""
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
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    })

def send_interactive_buttons(phone: str):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense to you?"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "understood",   "title": "Understood"}},
                {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}}
            ]}
        }
    })

def strip_greeting(text: str) -> str:
    """Remove leading greetings like 'hi' or 'hello'."""
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()

def get_gemini_reply(prompt: str) -> str:
    """Call Gemini and return cleaned-up response."""
    try:
        resp = model.generate_content(prompt)
        return strip_greeting(resp.text.strip())
    except Exception:
        logger.exception("Gemini error")
        return "Sorry, I encountered an internal error. Please try again."

def parse_subject_topic(text: str):
    """
    Use Gemini to extract 'subject' and 'topic' from free-form text.
    Returns (subject:str|None, topic:str|None).
    """
    parser_prompt = (
        "Extract subject and topic from this academic request.\n"
        "Respond JSON with keys 'subject' and 'topic' (null if missing).\n"
        f"User input: \"{text}\"\nJSON:"
    )
    resp = get_gemini_reply(parser_prompt)
    try:
        data = json.loads(resp)
        return data.get("subject"), data.get("topic")
    except json.JSONDecodeError:
        logger.error(f"Parser JSON decode failed: {resp}")
        return None, None

def log_training(phone: str, text: str, subject: str, topic: str):
    """Log raw inputs + parsed labels for ongoing NLU improvements."""
    db.collection("training_logs").add({
        "phone": phone,
        "input": text,
        "subject": subject,
        "topic": topic,
        "timestamp": firestore.SERVER_TIMESTAMP
    })

def get_or_create_user(phone: str) -> dict:
    """Fetch or initialize a Firestore doc for this phone."""
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {
            "phone": phone,
            "name": None,
            "date_joined": firestore.SERVER_TIMESTAMP,
            "last_prompt": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1),
            "current_subject": None,
            "current_topic": None
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
        if (request.args.get("hub.mode") == "subscribe" and
            request.args.get("hub.verify_token") == VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

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
    user  = get_or_create_user(phone)

    # 1) Onboard name
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            send_whatsapp_message(phone, f"Nice to meet you, {text}! 🎓 What would you like to study today?")
        else:
            send_whatsapp_message(phone, "Please share your full name (first and last).")
        return "OK", 200

    # 2) Ensure subject & topic are set
    if not user.get("current_subject") or not user.get("current_topic"):
        subject, topic = parse_subject_topic(text)
        log_training(phone, text, subject, topic)
        if subject and topic:
            update_user(phone, current_subject=subject, current_topic=topic)
            send_whatsapp_message(phone,
                f"Got it! We'll work on {topic} in {subject}. How can I help you with that?")
        elif subject:
            update_user(phone, current_subject=subject)
            send_whatsapp_message(phone,
                f"Your subject is {subject}. What specific topic or task can I help with? (e.g., essay, vocabulary)")
        elif topic:
            update_user(phone, current_topic=topic)
            send_whatsapp_message(phone,
                f"You're working on {topic}. Which subject does this belong to? (e.g., English, Chemistry)")
        else:
            send_whatsapp_message(phone,
                "I couldn't determine the subject or topic. Could you clarify what you're studying?")
        return "OK", 200

    # 3) Handle interactive buttons
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            btn = ir["button_reply"]["id"]
            if btn == "understood":
                send_whatsapp_message(phone, "Fantastic! 🎉 What’s next on your list?")
                return "OK", 200
            if btn == "explain_more" and user.get("last_prompt"):
                detail = get_gemini_reply(user["last_prompt"] + "\n\nPlease explain in more detail.")
                send_whatsapp_message(phone, detail)
                send_interactive_buttons(phone)
                return "OK", 200

    # 4) Free‐account credit logic
    if user.get("account_type") == "free":
        rt = user.get("credit_reset")
        if hasattr(rt, "to_datetime"):
            rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo:
            rt = rt.replace(tzinfo=None)
        now = datetime.utcnow()
        if isinstance(rt, datetime) and now >= rt:
            update_user(phone, credit_remaining=20, credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user.get("credit_remaining", 0) <= 0:
            send_whatsapp_message(phone, "Free limit reached (20/day). Upgrade for unlimited access.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # 5) Academic Q&A with context
    context = ""
    if user.get("current_subject"):
        context += f"Subject: {user['current_subject']}. "
    if user.get("current_topic"):
        context += f"Topic: {user['current_topic']}. "

    prompt = f"{BASE_PROMPT}\n{context}\nQuestion: {text}"
    update_user(phone, last_prompt=prompt)
    send_whatsapp_message(phone, "🤖 Thinking...")
    answer = get_gemini_reply(prompt)
    send_whatsapp_message(phone, answer)
    send_interactive_buttons(phone)

    return "OK", 200

# — Run server —
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
