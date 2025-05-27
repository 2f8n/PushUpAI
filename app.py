#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp tutor with Google Vision + Gemini image understanding.
"""

import os
import re
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, request

# === Firebase Admin SDK ===
import firebase_admin
from firebase_admin import credentials, firestore

cred = credentials.Certificate(os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
))
firebase_admin.initialize_app(cred, {"projectId": os.getenv("PROJECT_ID")})
db = firestore.client()

# === Google Vision Client ===
from google.cloud import vision
vision_client = vision.ImageAnnotatorClient()

# === Flask & WhatsApp setup ===
app = Flask(__name__)
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN    = os.getenv("ACCESS_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("PHONE_NUMBER_ID")

# === Gemini (GenAI) setup ===
import google.generativeai as genai
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-pro-002")

def log(label, obj=None):
    print(f"[DEBUG] {label}: {json.dumps(obj, ensure_ascii=False) if obj else ''}")

def send_whatsapp_message(phone: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
    log("→ WhatsApp API REQUEST", {"url":url, "payload":payload})
    resp = requests.post(url, headers=headers, json=payload)
    try: body = resp.json()
    except: body = resp.text
    log("← WhatsApp API RESPONSE", {"status":resp.status_code, "body":body})
    return resp

def send_interactive_buttons(phone: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    interactive = {
        "type":"button",
        "body":{"text":"Did that make sense to you?"},
        "action":{"buttons":[
            {"type":"reply","reply":{"id":"understood","title":"Understood"}},
            {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
        ]}
    }
    payload = {"messaging_product":"whatsapp","to":phone,"type":"interactive","interactive":interactive}
    log("→ BUTTONS REQUEST", {"url":url, "payload":interactive})
    resp = requests.post(url, headers=headers, json=payload)
    log("← BUTTONS RESPONSE", {"status":resp.status_code, "body":resp.text})
    return resp

# === Firestore helpers ===
def get_or_create_user(phone: str) -> dict:
    doc = db.collection("users").document(phone).get()
    if not doc.exists:
        user = {
            "phone":phone,
            "name":None,
            "date_joined":firestore.SERVER_TIMESTAMP,
            "last_prompt":None,
            "account_type":"free",
            "credit_remaining":20,
            "credit_reset":datetime.utcnow()+timedelta(days=1)
        }
        db.collection("users").document(phone).set(user)
        return user
    return doc.to_dict()

def update_user(phone: str, **fields):
    db.collection("users").document(phone).update(fields)

# === Gemini helpers ===
def strip_greeting(text: str) -> str:
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()

def get_gemini_reply(prompt: str) -> str:
    log("→ Gemini prompt", {"prompt":prompt})
    resp = model.generate_content(prompt)
    text = strip_greeting(resp.text.strip())
    log("← Gemini reply", {"reply":text})
    return text

# === Webhook endpoint ===
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method=="GET":
        if (request.args.get("hub.mode")=="subscribe" and
            request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"),200
        return "Verification failed",403

    data = request.get_json(force=True)
    log("← Incoming webhook", data)

    entry = data["entry"][0]["changes"][0]["value"]
    if "messages" not in entry:
        return "OK",200

    msg = entry["messages"][0]
    phone = msg["from"]
    user = get_or_create_user(phone)
    msg_type = msg.get("type")

    # --- IMAGE HANDLING ---
    if msg_type=="image":
        media_id = msg["image"]["id"]
        # 1) Fetch media URL
        meta = requests.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            params={"access_token":WHATSAPP_TOKEN}
        ).json()
        media_url = meta.get("url")
        # 2) Download bytes
        img_bytes = requests.get(media_url, headers={
            "Authorization":f"Bearer {WHATSAPP_TOKEN}"
        }).content
        # 3) Google Vision label detection
        image = vision.Image(content=img_bytes)
        labels = vision_client.label_detection(image=image).label_annotations
        top_labels = [l.description for l in labels[:5]]
        vision_desc = "I see: " + ", ".join(top_labels)
        log("← Vision labels", vision_desc)
        # 4) Ask Gemini to elaborate
        prompt = f"Here are labels from an image: {vision_desc}. Please describe the scene in detail."
        answer = get_gemini_reply(prompt)
        send_whatsapp_message(phone, answer)
        send_interactive_buttons(phone)
        return "OK",200

    # --- TEXT HANDLING ---
    text = msg.get("text",{}).get("body","").strip()

    # a) Welcome back
    if user.get("name") and text.lower() in ("hi","hello","hey"):
        first = user["name"].split()[0]
        send_whatsapp_message(phone, f"Welcome back, {first}! 🎓 What would you like to study today?")
        return "OK",200

    # b) Onboard name
    if not user.get("name"):
        if len(text.split())>=2:
            update_user(phone, name=text)
            send_whatsapp_message(phone, f"Nice to meet you, {text}! 🎓 What topic shall we study?")
        else:
            send_whatsapp_message(phone, "Please share your full name (first and last). 📖")
        return "OK",200

    # c) Interactive buttons
    if msg_type=="interactive":
        ir=msg.get("interactive",{})
        if ir.get("type")=="button_reply":
            pid=ir["button_reply"]["id"]
            if pid=="understood":
                send_whatsapp_message(phone,"Great! 🎉 What's next on your study list?")
                return "OK",200
            if pid=="explain_more" and user.get("last_prompt"):
                detail = get_gemini_reply(user["last_prompt"]+"\n\nPlease explain more.")
                send_whatsapp_message(phone, detail)
                send_interactive_buttons(phone)
                return "OK",200

    # d) Credit reset for free users
    if user.get("account_type")=="free":
        rt=user.get("credit_reset")
        if hasattr(rt,"to_datetime"):
            rt=rt.to_datetime().replace(tzinfo=None)
        if isinstance(rt,datetime) and datetime.utcnow()>=rt:
            update_user(phone, credit_remaining=20, credit_reset=datetime.utcnow()+timedelta(days=1))
            user["credit_remaining"]=20
        if user.get("credit_remaining",0)<=0:
            send_whatsapp_message(phone,"Free limit reached (20/day). Upgrade to Premium for unlimited prompts.")
            return "OK",200
        update_user(phone, credit_remaining=user["credit_remaining"]-1)

    # e) Academic Q&A
    prompt = (
        f"You are StudyMate AI, an academic tutor by ByteWave Media. "
        f"Answer the question below with clear, step-by-step academic explanations only. "
        f"Do NOT include any summaries or conclusions. "
        f"Question: {text}"
    )
    update_user(phone, last_prompt=prompt)
    send_whatsapp_message(phone,"🤖 Thinking...")
    answer = get_gemini_reply(prompt)
    send_whatsapp_message(phone, answer)
    send_interactive_buttons(phone)
    return "OK",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",10000)))
