#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp-based academic tutor using Flask, Firebase Firestore, and Google Gemini.
Features:
- User onboarding with full-name collection
- Welcome-back personalized greeting
- Free vs. Premium accounts with 20-prompts/day limit for Free
- Academic-only Q&A with "Understood"/"Explain more" buttons
- Context tracking for elaboration
"""

from flask import Flask, request
import os
import re
import requests
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from datetime import datetime, timedelta

# ─── Flask App Initialization ─────────────────────────────────────────────────
app = Flask(__name__)

# ─── Environment Variables ──────────────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN",    "pushupai_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN",    "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "")

# ─── Google Gemini Configuration ───────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-1.5-pro-002"
model = genai.GenerativeModel(MODEL_NAME)

# ─── Firebase (Firestore) Initialization ────────────────────────────────────────
# The service account JSON is mounted as a secret at /etc/secrets/<filename>
KEY_FILENAME = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH = f"/etc/secrets/{KEY_FILENAME}"
cred_path = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILENAME
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ─── Firestore Helper Functions ────────────────────────────────────────────────
def get_or_create_user(phone: str) -> dict:
    """
    Retrieve user document by phone or create a new one with default fields.
    """
    doc_ref = db.collection("users").document(phone)
    doc = doc_ref.get()
    if not doc.exists:
        user_data = {
            "phone": phone,
            "name": None,
            "greeted": False,
            "date_joined": firestore.SERVER_TIMESTAMP,
            "last_prompt": None,
            "account_type": "free",      # 'free' or 'premium'
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1)
        }
        doc_ref.set(user_data)
        return user_data
    return doc.to_dict()

def update_user(phone: str, **fields):
    """
    Update specified fields for a user document.
    """
    db.collection("users").document(phone).update(fields)

# ─── WhatsApp Messaging Functions ───────────────────────────────────────────────
def send_whatsapp_message(phone: str, text: str):
    """Send a text message via WhatsApp Cloud API."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(url, headers=headers, json=payload)

def send_interactive_buttons(phone: str):
    """Send 'Understood' and 'Explain more' interactive buttons."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    buttons_payload = {
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
    }
    requests.post(url, headers=headers, json=buttons_payload)

# ─── Gemini Utility Functions ──────────────────────────────────────────────────
def strip_greeting(text: str) -> str:
    """Remove leading greetings like 'hi', 'hello', 'hey'."""
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()

def get_gemini_reply(prompt: str) -> str:
    """Generate and clean a response from the Gemini model."""
    try:
        resp = model.generate_content(prompt)
        return strip_greeting(resp.text.strip())
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I encountered an error. Please try again soon."

# ─── Main Webhook Endpoint ─────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # ----- Verification for GET requests -----
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # ----- Process incoming message -----
    data = request.json
    entry = data.get("entry", [])[0].get("changes", [])[0].get("value", {})
    if "messages" not in entry:
        return "OK", 200

    msg = entry["messages"][0]
    phone = msg["from"]
    user = get_or_create_user(phone)

    # ----- Onboarding & Welcome Logic -----
    # If user has a name but hasn't been greeted this session
    if user.get("name") and not user.get("greeted"):
        first_name = user["name"].split()[0]
        send_whatsapp_message(phone, f"Welcome back, {first_name}! 🎓 What would you like to study today?")
        update_user(phone, greeted=True)
        return "OK", 200

    # If user has no name, prompt for full name
    text = msg.get("text", {}).get("body", "").strip()
    if not user.get("name"):
        if len(text.split()) >= 2:
            update_user(phone, name=text, greeted=True)
            send_whatsapp_message(phone, f"Nice to meet you, {text}! 🎓 What topic shall we study?")
        else:
            send_whatsapp_message(phone, "Please share your full name (first and last). 📖")
        return "OK", 200

    # ----- Reject Non-Academic Requests -----
    if text.lower().startswith("who am i") or not text:
        send_whatsapp_message(phone, "I only handle academic study questions. What would you like to learn?")
        return "OK", 200

    # ----- Handle Button Replies -----
    if msg.get("type") == "button":
        payload = msg["button"]["payload"]
        if payload == "understood":
            send_whatsapp_message(phone, "Great! 🎉 What's next on your study list?")
        elif payload == "explain_more" and user.get("last_prompt"):
            detail = get_gemini_reply(user["last_prompt"] + "\n\nPlease explain in more detail.")
            send_whatsapp_message(phone, detail)
            send_interactive_buttons(phone)
        return "OK", 200

    # ----- Credit Management for Free Users -----
    if user.get("account_type") == "free":
        # Reset credits if reset time has passed
        reset_time = user.get("credit_reset")
        if hasattr(reset_time, 'to_datetime'):
            reset_time = reset_time.to_datetime()
        if isinstance(reset_time, datetime) and datetime.utcnow() >= reset_time:
            update_user(phone, credit_remaining=20, credit_reset=datetime.utcnow() + timedelta(days=1))
            user["credit_remaining"] = 20

        # If out of credits, prompt upgrade
        if user.get("credit_remaining", 0) <= 0:
            send_whatsapp_message(phone, "Free limit reached (20/day). Upgrade to Premium for unlimited prompts.")
            return "OK", 200

        # Deduct one credit
        new_credits = user.get("credit_remaining", 1) - 1
        update_user(phone, credit_remaining=new_credits)
        user["credit_remaining"] = new_credits

    # ----- Academic Q&A Flow -----
    prompt = (
        f"You are StudyMate AI, an academic tutor by ByteWave Media for {user['name']}. "
        f"Answer the question below with clear, step-by-step academic explanations only. "
        f"Do NOT include summaries or conclusions. "
        f"Question: {text}"
    )
    update_user(phone, last_prompt=prompt)

    send_whatsapp_message(phone, "🤖 Thinking...")
    answer = get_gemini_reply(prompt)
    send_whatsapp_message(phone, answer)
    send_interactive_buttons(phone)

    return "OK", 200

# ─── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
