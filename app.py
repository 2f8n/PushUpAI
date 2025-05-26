#!/usr/bin/env python3
"""

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
KEY_FILENAME = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH = f"/etc/secrets/{KEY_FILENAME}"
cred_path = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILENAME
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
ddb = firestore.client()

def get_or_create_user(phone: str) -> dict:
    """
    Retrieve user document by phone or create a new one with defaults.
    """
    ref = ddb.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {
            "phone": phone,
            "name": None,
            "greeted": False,
            "date_joined": firestore.SERVER_TIMESTAMP,
            "last_prompt": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1)
        }
        ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone: str, **fields):
    """Update specified fields for a user."""
    ddb.collection("users").document(phone).update(fields)

# ─── WhatsApp Messaging ─────────────────────────────────────────────────────────
def send_whatsapp_message(phone: str, text: str):
    """Send a text message via WhatsApp Cloud API."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def send_interactive_buttons(phone: str):
    """Send 'Understood'/'Explain more' buttons."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    hdr = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    btns = {"type":"interactive","interactive": {"type":"button","body":{"text":"Did that make sense to you?"},
            "action":{"buttons":[{"type":"reply","reply":{"id":"understood","title":"Understood"}},
                                    {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}]}}}
    payload = {"messaging_product":"whatsapp","to":phone,"type":btns["type"],"interactive":btns["interactive"]}
    requests.post(url, headers=hdr, json=payload)

# ─── Gemini Helpers ──────────────────────────────────────────────────────────────
def strip_greeting(text: str) -> str:
    """Remove leading greetings."""
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()

def get_gemini_reply(prompt: str) -> str:
    """Call Gemini and clean response."""
    try:
        resp = model.generate_content(prompt)
        return strip_greeting(resp.text.strip())
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I encountered an error."

# ─── Main Webhook ─────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        mode, token = request.args.get("hub.mode"), request.args.get("hub.verify_token")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json
    entry = data.get("entry", [])[0].get("changes", [])[0].get("value", {})
    if "messages" not in entry:
        return "OK",200

    msg = entry["messages"][0]
    phone = msg["from"]
    user = get_or_create_user(phone)
    text = msg.get("text",{}).get("body","").strip()

    # Welcome back
    if user.get("name") and not user.get("greeted"):
        first = user["name"].split()[0]
        send_whatsapp_message(phone, f"Welcome back, {first}! 🎓 What would you like to study today?")
        update_user(phone, greeted=True)
        return "OK",200

    # Onboard
    if not user.get("name"):
        if len(text.split())>=2:
            update_user(phone, name=text, greeted=True)
            send_whatsapp_message(phone, f"Nice to meet you, {text}! 🎓 What topic? ")
        else:
            send_whatsapp_message(phone, "Please send your full name.")
        return "OK",200

    # Reject identity or empty
    if text.lower().startswith("who am i") or not text:
        send_whatsapp_message(phone, "I only answer academic study questions.")
        return "OK",200

    # Button logic
    if msg.get("type")=="button":
        p = msg["button"]["payload"]
        if p=="understood":
            send_whatsapp_message(phone, "Great! 🎉 What's next?")
        elif p=="explain_more" and user.get("last_prompt"):
            detail = get_gemini_reply(user["last_prompt"]+"\n\nPlease explain more.")
            send_whatsapp_message(phone, detail)
            send_interactive_buttons(phone)
        return "OK",200

    # Credit management
    if user.get("account_type")=="free":
        rt = user.get("credit_reset")
        if hasattr(rt,'to_datetime'):
            rt = rt.to_datetime()
        if hasattr(rt,'tzinfo') and rt.tzinfo:
            rt = rt.replace(tzinfo=None)
        if datetime.utcnow() >= rt:
            update_user(phone, credit_remaining=20, credit_reset=datetime.utcnow()+timedelta(days=1))
            user["credit_remaining"]=20
        if user.get("credit_remaining",0)<=0:
            send_whatsapp_message(phone, "Free limit reached. Upgrade for unlimited.")
            return "OK",200
        newc = user.get("credit_remaining",1)-1
        update_user(phone, credit_remaining=newc)
        user["credit_remaining"]=newc

    # Academic query
    prompt = (f"You are StudyMate AI, academic tutor. Answer step-by-step, no summaries. Question: {text}")
    update_user(phone, last_prompt=prompt)
    send_whatsapp_message(phone, "🤖 Thinking...")
    ans = get_gemini_reply(prompt)
    send_whatsapp_message(phone, ans)
    send_interactive_buttons(phone)

    return "OK",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",10000)))
