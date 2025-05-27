import os
import json
import logging
from collections import deque
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Required environment variables
for v in ("VERIFY_TOKEN", "ACCESS_TOKEN", "PHONE_NUMBER_ID", "GEMINI_API_KEY"):
    if not os.getenv(v):
        logger.error(f"Missing environment variable: {v}")
        raise SystemExit(f"Please set environment variable {v}")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

# Configure Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Firebase Admin SDK
cred = credentials.Certificate("studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Load AI system prompt from file
with open("studymate_prompt.txt", "r") as f:
    SYSTEM_PROMPT = f.read().strip()

app = Flask(__name__)
sessions = {}  # phone -> {"history": deque(maxlen=5)}

def ensure_session(phone):
    if phone not in sessions:
        sessions[phone] = {"history": deque(maxlen=5)}
    return sessions[phone]

def safe_post(url, payload):
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code not in (200, 201):
            logger.error(f"WhatsApp API {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed WhatsApp send")

def send_text(phone, text):
    safe_post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": text},
        },
    )

def send_buttons(phone):
    safe_post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "Did that make sense to you?"},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                        {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}},
                    ]
                },
            },
        },
    )

def strip_fences(t):
    t = t.strip()
    if t.startswith("```") and t.endswith("```"):
        t = t[3:-3].strip()
    return t

def get_gemini(prompt):
    try:
        return model.generate_content(prompt).text.strip()
    except Exception:
        logger.exception("Gemini error")
        return json.dumps({"type": "clarification", "content": "Sorry, I encountered an error. Please try again."})

def get_or_create_user(phone):
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {
            "phone": phone,
            "name": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1),
            "last_prompt": None,
        }
        ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone, **fields):
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated {phone}: {fields}")

def build_prompt(user, history, message):
    parts = [SYSTEM_PROMPT]
    if user.get("name"):
        first_name = user["name"].split()[0]
        parts.append(f'User name: "{first_name}"')
    if history:
        parts.append("Recent messages:")
        parts.extend(f"- {h}" for h in history)
    parts.append(f'Current message: "{message}"')
    parts.append("JSON:")
    return "\n".join(parts)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json or {}
    entry = data.get("entry", [])
    if not entry or not entry[0].get("changes"):
        return "OK", 200

    msg = entry[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")
    if not phone:
        return "OK", 200

    text = msg.get("text", {}).get("body", "").strip()
    if not text:
        return "OK", 200

    user = get_or_create_user(phone)
    now = datetime.utcnow()

    # Onboard user name if unknown
    if user["name"] is None:
        if len(text.split()) >= 2:
            first_name = text.split()[0]
            update_user(phone, name=text)
            send_text(phone, f"What would you like to study today, {first_name}?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # Credits reset and usage decrement for free users
    if user["account_type"] == "free":
        rt = user["credit_reset"]
        if hasattr(rt, "to_datetime"):
            rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo:
            rt = rt.replace(tzinfo=None)
        if now >= rt:
            update_user(phone, credit_remaining=20, credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user["credit_remaining"] <= 0:
            send_text(phone, "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # Handle interactive button replies
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great—what’s next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more = get_gemini(user["last_prompt"] + "\n\nPlease explain in more detail.")
                clean_more = strip_fences(more)
                send_text(phone, clean_more)
                send_buttons(phone)
        return "OK", 200

    # Normal text message handling
    sess = ensure_session(phone)
    history = list(sess["history"])
    sess["history"].append(text)

    prompt = build_prompt(user, history, text)
    raw = get_gemini(prompt)
    clean = strip_fences(raw)

    try:
        j = json.loads(clean)
        rtype = j.get("type")
        content = j.get("content", "")
    except Exception:
        rtype = "answer"
        content = clean

    if not isinstance(content, str):
        content = str(content)

    send_text(phone, content)
    if rtype == "answer":
        send_buttons(phone)

    update_user(phone, last_prompt=prompt)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
