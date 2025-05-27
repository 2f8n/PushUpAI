#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp-based academic tutor using Flask, Firebase Admin & Gemini.
"""

import os
import re
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, request

# === Firebase Admin SDK setup ===
import firebase_admin
from firebase_admin import credentials, firestore

# 1) Path to your service-account JSON (mounted by Render)
cred_path = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
)

# 2) Your Firebase Project ID
project_id = os.getenv("PROJECT_ID")

# Initialize the Admin SDK with both credentials and project ID
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred, {"projectId": project_id})
db = firestore.client()


# === Flask & WhatsApp setup ===
app = Flask(__name__)

VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN    = os.getenv("ACCESS_TOKEN")       # Your permanent WhatsApp token
WHATSAPP_PHONE_ID = os.getenv("PHONE_NUMBER_ID")    # WhatsApp Cloud phone number ID


# === Gemini (Google Generative AI) setup ===
import google.generativeai as genai
GENAI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")


# === Simple debug printer ===
def log(label, obj=None):
    payload = json.dumps(obj, ensure_ascii=False) if obj is not None else ""
    print(f"[DEBUG] {label}: {payload}")


# === WhatsApp helpers ===
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
    log("→ WhatsApp API REQUEST", {"url": url, "payload": payload})
    resp = requests.post(url, headers=headers, json=payload)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    log("← WhatsApp API RESPONSE", {"status": resp.status_code, "body": body})
    return resp


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
    log("→ WhatsApp API BUTTONS REQUEST", {"url": url, "payload": payload})
    resp = requests.post(url, headers=headers, json=payload)
    log("← WhatsApp API BUTTONS RESPONSE", {"status": resp.status_code, "body": resp.text})
    return resp


# === Firestore helpers ===
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
    db.collection("users").document(phone).update(fields)


# === Gemini prompt helper ===
def strip_greeting(text: str) -> str:
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()


def get_gemini_reply(prompt: str) -> str:
    log("→ Gemini prompt", {"prompt": prompt})
    response = model.generate_content(prompt)
    text = strip_greeting(response.text.strip())
    log("← Gemini reply", {"reply": text})
    return text


# === Webhook endpoint ===
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Verification handshake
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # Process incoming WhatsApp messages
    data = request.get_json(force=True)
    log("← Incoming webhook payload", data)

    entry = data["entry"][0]["changes"][0]["value"]
    if "messages" not in entry:
        return "OK", 200

    msg   = entry["messages"][0]
    phone = msg["from"]
    text  = msg.get("text", {}).get("body", "").strip()
    user  = get_or_create_user(phone)

    # 1) Welcome-back
    if user.get("name") and text.lower() in ("hi", "hello", "hey"):
        first = user["name"].split()[0]
        send_whatsapp_message(phone, f"Welcome back, {first}! 🎓 What would you like to study today?")
        return "OK", 200

    # 2) Onboard new user (collect name)
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            reply = f"Nice to meet you, {text}! 🎓 What topic shall we study?"
        else:
            reply = "Please share your *full* name (first and last). 📖"
        send_whatsapp_message(phone, reply)
        return "OK", 200

    # 3) Reject non-academic / identity queries
    if not text or text.lower().startswith("who am i"):
        send_whatsapp_message(phone, "I only answer academic study questions. What topic are you curious about?")
        return "OK", 200

    # 4) Handle interactive button replies
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
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

    # 5) Credit management (free-tier)
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

    # 6) Academic Q&A flow
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
