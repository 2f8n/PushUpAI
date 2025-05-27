#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp-based academic tutor using Flask, Firebase Admin & Gemini.
"""

import os
import re
import requests
from datetime import datetime, timedelta
from flask import Flask, request

# === Firebase Admin SDK setup ===
import firebase_admin
from firebase_admin import credentials, firestore

# Load service-account JSON from the path Render mounts
cred_path = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
)
# Read your Firebase Project ID
project_id = os.getenv("PROJECT_ID")

# Initialize Admin SDK with both credentials and project ID
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred, {"projectId": project_id})
db = firestore.client()

# === Flask & WhatsApp setup ===
app = Flask(__name__)

# These names match the vars you set in Render’s dashboard
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN", "pushupai_verify_token")
WHATSAPP_TOKEN    = os.getenv("ACCESS_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("PHONE_NUMBER_ID", "")

# === Google Gemini (GenAI) setup ===
import google.generativeai as genai
GENAI_API_KEY = os.getenv("GEMINI_API_KEY", "")
genai.configure(api_key=GENAI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")


# === Firestore helper functions ===

def get_or_create_user(phone: str) -> dict:
    doc = db.collection("users").document(phone).get()
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
        db.collection("users").document(phone).set(user)
        return user
    return doc.to_dict()


def update_user(phone: str, **fields):
    db.collection("users").document(phone).update(fields)


# === WhatsApp messaging helpers ===

def send_whatsapp_message(phone: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(url, headers=headers, json=payload)


def send_interactive_buttons(phone: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
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
    requests.post(url, headers=headers, json=payload)


# === Gemini prompt helper ===

def strip_greeting(text: str) -> str:
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()


def get_gemini_reply(prompt: str) -> str:
    response = model.generate_content(prompt)
    return strip_greeting(response.text.strip())


# === Webhook endpoint ===

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # 1) Verify handshake
    if request.method == "GET":
        if (
            request.args.get("hub.mode") == "subscribe" and
            request.args.get("hub.verify_token") == VERIFY_TOKEN
        ):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # 2) Process incoming messages
    data = request.get_json(force=True)
    entry = data["entry"][0]["changes"][0]["value"]
    if "messages" not in entry:
        return "OK", 200

    msg = entry["messages"][0]
    phone = msg["from"]
    text = msg.get("text", {}).get("body", "").strip()
    user = get_or_create_user(phone)

    # a) Welcome back on simple greeting
    if user.get("name") and text.lower() in ("hi", "hello", "hey"):
        first = user["name"].split()[0]
        send_whatsapp_message(phone, f"Welcome back, {first}! 🎓 What would you like to study today?")
        return "OK", 200

    # b) Onboard new users (collect full name)
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            send_whatsapp_message(phone, f"Nice to meet you, {text}! 🎓 What topic shall we study?")
        else:
            send_whatsapp_message(phone, "Please share your full name (first and last). 📖")
        return "OK", 200

    # c) Reject non-academic / identity queries
    if not text or text.lower().startswith("who am i"):
        send_whatsapp_message(phone, "I only answer academic study questions. What topic are you curious about?")
        return "OK", 200

    # d) Handle interactive button replies
    if msg.get("type") == "interactive":
        ir = msg.get("interactive")
        if ir.get("type") == "button_reply":
            pid = ir["button_reply"]["id"]
            if pid == "understood":
                send_whatsapp_message(phone, "Great! 🎉 What's next on your study list?")
                return "OK", 200
            if pid == "explain_more" and user.get("last_prompt"):
                detail = get_gemini_reply(user["last_prompt"] + "\n\nPlease explain more.")
                send_whatsapp_message(phone, detail)
                send_interactive_buttons(phone)
                return "OK", 200

    # e) Credit management for free tier
    if user.get("account_type") == "free":
        rt = user.get("credit_reset")
        if hasattr(rt, "to_datetime"):
            rt = rt.to_datetime().replace(tzinfo=None)
        if isinstance(rt, datetime) and datetime.utcnow() >= rt:
            update_user(phone, credit_remaining=20, credit_reset=datetime.utcnow() + timedelta(days=1))
            user["credit_remaining"] = 20
        if user.get("credit_remaining", 0) <= 0:
            send_whatsapp_message(phone, "Free limit reached (20/day). Upgrade to Premium for unlimited prompts.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # f) Academic Q&A flow
    prompt = (
        f"You are StudyMate AI, an academic tutor by ByteWave Media. "
        f"Answer the question below with clear, step-by-step academic explanations only. "
        f"Do NOT include any summaries or conclusions. "
        f"Question: {text}"
    )
    update_user(phone, last_prompt=prompt)

    send_whatsapp_message(phone, "🤖 Thinking...")
    answer = get_gemini_reply(prompt)
    send_whatsapp_message(phone, answer)
    send_interactive_buttons(phone)

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
