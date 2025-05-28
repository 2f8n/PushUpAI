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

# Setup logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Required environment variables
REQUIRED_ENV_VARS = ("VERIFY_TOKEN", "ACCESS_TOKEN", "PHONE_NUMBER_ID", "GEMINI_API_KEY")
for v in REQUIRED_ENV_VARS:
    if not os.getenv(v):
        logger.error(f"Missing environment variable: {v}")
        raise SystemExit("Please set all required environment variables.")

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))

# Initialize Google Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Firebase with secret file from Render
FIREBASE_SECRET_PATH = "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
cred = credentials.Certificate(FIREBASE_SECRET_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Load system prompt
with open("studymate_prompt.txt", "r") as f:
    SYSTEM_PROMPT = f.read().strip()

app = Flask(__name__)

# User sessions memory: phone -> {"history": deque(maxlen=5)}
sessions = {}

def ensure_session(phone):
    if phone not in sessions:
        sessions[phone] = {"history": deque(maxlen=5)}
    return sessions[phone]

def safe_post(url, payload):
    try:
        r = requests.post(url,
                          headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                                   "Content-Type": "application/json"},
                          json=payload)
        if r.status_code not in (200, 201):
            logger.error(f"WhatsApp API error {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed sending message via WhatsApp API")

def send_text(phone, text):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp", "to": phone,
        "type": "text", "text": {"body": text}
    })

def send_buttons(phone):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp", "to": phone,
        "type": "interactive", "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense to you?"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}}
            ]}
        }
    })

def strip_fences(text):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    return text

def get_gemini_response(prompt):
    try:
        response = model.generate_content(prompt)
        logger.info(f"Gemini raw response:\n{response.text}")
        return response.text.strip()
    except Exception:
        logger.exception("Gemini call error")
        fallback = json.dumps({"type": "clarification", "content": "Sorry, I encountered an error. Please try again."})
        return fallback

def get_or_create_user(phone):
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user_data = {
            "phone": phone,
            "name": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1),
            "last_prompt": None
        }
        ref.set(user_data)
        return user_data
    return doc.to_dict()

def update_user(phone, **kwargs):
    db.collection("users").document(phone).update(kwargs)
    logger.info(f"Updated user {phone} with {kwargs}")

def build_prompt(user, history, message):
    parts = [SYSTEM_PROMPT]
    if user.get("name"):
        first_name = user["name"].split()[0]
        parts.append(f'User name: "{first_name}"')
    if history:
        parts.append("Recent messages:")
        parts.extend(f"- {msg}" for msg in history)
    parts.append(f'Current message: "{message}"')
    parts.append("JSON:")
    return "\n".join(parts)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    data = request.json or {}
    entry = data.get("entry", [])
    if not entry or not entry[0].get("changes"):
        return "OK", 200

    msg = entry[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")

    if not phone:
        return "OK", 200

    # Handle text only for now
    text = msg.get("text", {}).get("body", "").strip()
    if not text:
        return "OK", 200

    user = get_or_create_user(phone)
    now = datetime.utcnow()

    # Onboard user name if missing
    if user["name"] is None:
        if len(text.split()) >= 2:
            first_name = text.split()[0]
            update_user(phone, name=text)
            send_text(phone, f"What would you like to study today, {first_name}?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # Handle free tier credit reset/check
    if user["account_type"] == "free":
        credit_reset_time = user["credit_reset"]
        if hasattr(credit_reset_time, "to_datetime"):
            credit_reset_time = credit_reset_time.to_datetime()
        if isinstance(credit_reset_time, datetime) and credit_reset_time.tzinfo:
            credit_reset_time = credit_reset_time.replace(tzinfo=None)
        if now >= credit_reset_time:
            update_user(phone,
                        credit_remaining=20,
                        credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20

        if user["credit_remaining"] <= 0:
            send_text(phone, "You have reached your free usage limit (20 messages per day). Please consider upgrading for unlimited access.")
            return "OK", 200

        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # Handle interactive buttons from user
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great! What would you like to learn next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more_resp = get_gemini_response(user["last_prompt"] + "\n\nPlease explain in more detail.")
                more_clean = strip_fences(more_resp)
                try:
                    more_json = json.loads(more_clean)
                    more_content = more_json.get("content", more_clean)
                except Exception:
                    more_content = more_clean
                send_text(phone, more_content)
                send_buttons(phone)
        return "OK", 200

    # Process normal text conversation
    sess = ensure_session(phone)
    history = list(sess["history"])
    sess["history"].append(text)

    prompt = build_prompt(user, history, text)
    raw_response = get_gemini_response(prompt)
    clean_response = strip_fences(raw_response)

    try:
        j = json.loads(clean_response)
        rtype = j.get("type", "answer")
        content = j.get("content", "")
    except Exception:
        rtype = "answer"
        content = clean_response

    if not isinstance(content, str):
        content = str(content)

    send_text(phone, content)

    # Send buttons ONLY for academic answers (type=="answer")
    if rtype == "answer":
        send_buttons(phone)

    update_user(phone, last_prompt=prompt)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
