#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp-based academic tutor using Flask, Firestore, and Gemini.
"""
from flask import Flask, request, jsonify
import os
import re
import requests
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from datetime import datetime, timedelta
import logging

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)

# Environment variables (local `.env` or Render dashboard)
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")

if not ACCESS_TOKEN or not PHONE_NUMBER_ID or not GEMINI_API_KEY:
    logging.warning("One or more required environment variables are missing.")

# Configure Gemini model
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Firestore setup
KEY_FILENAME = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH = f"/etc/secrets/{KEY_FILENAME}"
cred_path = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILENAME

cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Firestore user helpers
def get_or_create_user(phone: str) -> dict:
    doc_ref = db.collection("users").document(phone)
    doc = doc_ref.get()
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
        doc_ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone: str, **fields):
    doc_ref = db.collection("users").document(phone)
    doc_ref.update(fields)

# WhatsApp helpers
def send_whatsapp_message(phone: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        logging.error(f"WhatsApp send message failed: {resp.status_code} {resp.text}")

def send_interactive_buttons(phone: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    interactive = {
        "type": "button",
        "body": {"text": "Did that make sense to you?"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}}
            ]
        }
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": interactive
    }
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        logging.error(f"WhatsApp send interactive buttons failed: {resp.status_code} {resp.text}")

# Helper to remove greetings from prompt text
def strip_greeting(text: str) -> str:
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()

# Generate Gemini reply to prompt
def get_gemini_reply(prompt: str) -> str:
    try:
        response = model.generate_content(prompt)
        return strip_greeting(response.text.strip())
    except Exception as e:
        logging.error(f"Error calling Gemini API: {e}")
        return "Sorry, I am having trouble answering that right now."

# Main webhook endpoint
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Verification handshake on GET
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        else:
            return "Verification failed", 403

    # POST: incoming WhatsApp message processing
    data = request.get_json()
    logging.debug(f"Webhook POST data: {data}")

    entry = data.get("entry", [{}])[0]
    changes = entry.get("changes", [{}])[0]
    value = changes.get("value", {})
    messages = value.get("messages", [])

    if not messages:
        return jsonify({}), 200

    msg = messages[0]
    phone = msg.get("from")
    if not phone:
        return jsonify({}), 200

    user = get_or_create_user(phone)
    text = msg.get("text", {}).get("body", "").strip()

    # 1) Welcome-back on greeting
    if user.get("name") and text.lower() in ("hi", "hello", "hey"):
        first_name = user["name"].split()[0] if user.get("name") else ""
        send_whatsapp_message(phone, f"Welcome back, {first_name}! 🎓 What would you like to study today?")
        return jsonify({}), 200

    # 2) Onboard new user (ask full name)
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            send_whatsapp_message(phone, f"Nice to meet you, {text}! 🎓 What topic shall we study?")
        else:
            send_whatsapp_message(phone, "Please share your full name (first and last). 📖")
        return jsonify({}), 200

    # 3) Reject non-academic or identity queries
    if not text or text.lower().startswith("who am i"):
        send_whatsapp_message(phone, "I only answer academic study questions. What topic are you curious about?")
        return jsonify({}), 200

    # 4) Handle interactive replies
    if msg.get("type") == "interactive":
        interactive = msg.get("interactive")
        if interactive and interactive.get("type") == "button_reply":
            button_id = interactive["button_reply"]["id"]
            if button_id == "understood":
                send_whatsapp_message(phone, "Great! 🎉 What's next on your study list?")
                return jsonify({}), 200
            elif button_id == "explain_more" and user.get("last_prompt"):
                detail = get_gemini_reply(user["last_prompt"] + "\n\nPlease explain more.")
                send_whatsapp_message(phone, detail)
                send_interactive_buttons(phone)
                return jsonify({}), 200

    # 5) Credit management for free users
    if user.get("account_type") == "free":
        credit_reset = user.get("credit_reset")
        if hasattr(credit_reset, "to_datetime"):
            credit_reset = credit_reset.to_datetime().replace(tzinfo=None)
        if isinstance(credit_reset, datetime) and datetime.utcnow() >= credit_reset:
            # Reset daily credits
            update_user(phone, credit_remaining=20, credit_reset=datetime.utcnow() + timedelta(days=1))
            user["credit_remaining"] = 20

        if user.get("credit_remaining", 0) <= 0:
            send_whatsapp_message(phone, "Free limit reached (20/day). Upgrade to Premium for unlimited prompts.")
            return jsonify({}), 200

        # Deduct one credit
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # 6) Academic Q&A prompt
    prompt = (
        f"You are StudyMate AI, an academic tutor by ByteWave Media. "
        f"Answer the question below with clear, step-by-step academic explanations only. "
        f"Do NOT include any summaries or conclusions. "
        f"Question: {text}"
    )
    update_user(phone, last_prompt=prompt)

    # Tell user we are thinking
    send_whatsapp_message(phone, "🤖 Thinking...")

    # Get Gemini reply
    answer = get_gemini_reply(prompt)

    # Send answer + interactive buttons
    send_whatsapp_message(phone, answer)
    send_interactive_buttons(phone)

    # Store message to Firestore (log)
    try:
        db.collection("messages").add({
            "phone": phone,
            "message": text,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        logging.error(f"Error saving message log: {e}")

    return jsonify({}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
