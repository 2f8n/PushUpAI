#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp-based academic tutor using Flask, Firestore, and Google Gemini.
Includes:
- Onboarding (name collection)
- Welcome-back greetings
- Free vs. Premium credit management (20/day limit)
- Academic-only Q&A with “Understood” / “Explain more” buttons
- Robust error handling, logging, and environment configuration
"""

# 1. Load environment variables from .env
from dotenv import load_dotenv
load_dotenv()

import os
import re
import logging
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

# 2. Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s:%(lineno)d — %(message)s"
)
logger = logging.getLogger("StudyMate")

# 3. Flask app setup
app = Flask(__name__)

# 4. Environment variables
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))

# 5. Validate required env vars
missing = [k for k in ("VERIFY_TOKEN","ACCESS_TOKEN","PHONE_NUMBER_ID","GEMINI_API_KEY") if not os.getenv(k)]
if missing:
    logger.error(f"Missing environment variables: {', '.join(missing)}")
    raise SystemExit("Please set all required environment variables before starting.")

# 6. Initialize Google Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# 7. Initialize Firestore
KEY_FILENAME = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH  = f"/etc/secrets/{KEY_FILENAME}"
cred_path    = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILENAME
cred         = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# 8. Load the concise system prompt
with open("studymate_prompt.txt", "r") as f:
    BASE_PROMPT = f.read().strip()

# 9. Helper functions

def safe_post(url: str, payload: dict):
    """POST to WhatsApp API with error logging."""
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
            logger.error(f"WhatsApp API returned {resp.status_code}: {resp.text}")
    except Exception:
        logger.exception("Exception when sending WhatsApp message")

def send_whatsapp_message(phone: str, text: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", payload)

def send_interactive_buttons(phone: str):
    interactive = {
        "type": "button",
        "body": {"text": "Did that make sense to you?"},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": "understood",   "title": "Understood"}},
            {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}}
        ]}
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": interactive
    }
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", payload)

def strip_greeting(text: str) -> str:
    """Remove leading 'hi/hello/hey' lines from model output."""
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()

def get_gemini_reply(prompt: str) -> str:
    """Generate and return a cleaned response from Gemini."""
    try:
        resp = model.generate_content(prompt)
        return strip_greeting(resp.text.strip())
    except Exception:
        logger.exception("Gemini generate_content failed")
        return "Sorry, I encountered an internal error. Please try again."

def get_or_create_user(phone: str) -> dict:
    """Fetch existing user or create a new Firestore document."""
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
            "credit_reset": datetime.utcnow() + timedelta(days=1)
        }
        ref.set(user)
        logger.info(f"Created user record for {phone}")
        return user
    return doc.to_dict()

def update_user(phone: str, **fields):
    """Update specified fields of a user document."""
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated user {phone}: {fields}")

# 10. Webhook endpoint

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Verification handshake
    if request.method == "GET":
        mode  = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    # Handle incoming messages
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

    # 1) Welcome-back greeting
    if user.get("name") and text.lower() in ("hi", "hello", "hey"):
        first = user["name"].split()[0]
        send_whatsapp_message(phone, f"Welcome back, {first}! 🎓 What would you like to study today?")
        return "OK", 200

    # 2) Onboarding (collect full name)
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            send_whatsapp_message(phone, f"Nice to meet you, {text}! 🎓 What topic shall we study?")
        else:
            send_whatsapp_message(phone, "Please share your full name (first and last). 📖")
        return "OK", 200

    # 3) Reject non-academic or empty
    if not text or text.lower().startswith("who am i"):
        send_whatsapp_message(phone, "I only answer academic study questions. What topic are you curious about?")
        return "OK", 200

    # 4) Interactive button replies
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            btn = ir["button_reply"]["id"]
            if btn == "understood":
                send_whatsapp_message(phone, "Great! 🎉 What's next on your study list?")
                return "OK", 200
            if btn == "explain_more" and user.get("last_prompt"):
                detail = get_gemini_reply(user["last_prompt"] + "\n\nPlease explain in more detail.")
                send_whatsapp_message(phone, detail)
                send_interactive_buttons(phone)
                return "OK", 200

    # 5) Credit management for free accounts
    if user.get("account_type") == "free":
        rt = user.get("credit_reset")
        if hasattr(rt, "to_datetime"):
            rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo:
            rt = rt.replace(tzinfo=None)
        now = datetime.utcnow()
        if isinstance(rt, datetime) and now >= rt:
            update_user(phone,
                        credit_remaining=20,
                        credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user.get("credit_remaining", 0) <= 0:
            send_whatsapp_message(phone, "Free limit reached (20/day). Upgrade to Premium for unlimited prompts.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # 6) Academic Q&A
    prompt = f"{BASE_PROMPT}\n\nQuestion: {text}"
    update_user(phone, last_prompt=prompt)
    send_whatsapp_message(phone, "🤖 Thinking...")
    answer = get_gemini_reply(prompt)
    send_whatsapp_message(phone, answer)
    send_interactive_buttons(phone)
    return "OK", 200

# 11. Run the Flask app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
