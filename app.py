#!/usr/bin/env python3
import os, re, json, logging
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

# — Logging —
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# — Load env —
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

REQUIRED = ("VERIFY_TOKEN","ACCESS_TOKEN","PHONE_NUMBER_ID","GEMINI_API_KEY")
for var in REQUIRED:
    if not os.getenv(var):
        logger.error(f"Missing environment variable: {var}")
        raise SystemExit("Please set all required environment variables.")

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))

# — Initialize Gemini —
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# — Initialize Firestore (only for name, credits, last_prompt) —
KEY_FILENAME = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH  = f"/etc/secrets/{KEY_FILENAME}"
cred_path    = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILENAME
cred         = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# — Base system prompt for Gemini —
BASE_PROMPT = """
You are a friendly academic tutor.
Always respond with JSON ONLY of the form:
  {
    "type": "clarification" | "answer",
    "content": "…"
  }
Rules:
- If the user's name is unknown, type=clarification and ask for their full name.
- If you need more context (subject, topic, task), type=clarification and ask exactly one direct question.
- Otherwise type=answer, and provide step-by-step academic help (≤3 sentences per step).
- Do NOT include buttons in "content."
"""

app = Flask(__name__)

def safe_post(url, payload):
    try:
        r = requests.post(url,
            headers={"Authorization":f"Bearer {ACCESS_TOKEN}",
                     "Content-Type":"application/json"},
            json=payload)
        if r.status_code != 200:
            logger.error(f"WhatsApp API {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed to send WhatsApp message")

def send_text(phone, text):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp","to":phone,"type":"text","text":{"body":text}
    })

def send_buttons(phone):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp","to":phone,"type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":"Did that make sense to you?"},
            "action":{"buttons":[
              {"type":"reply","reply":{"id":"understood","title":"Understood"}},
              {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
            ]}
        }
    })

def strip_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[\w]*|```$", "", s).strip()
    return s

def get_gemini(prompt):
    try:
        return model.generate_content(prompt).text.strip()
    except Exception:
        logger.exception("Gemini error")
        # fallback clarification
        return json.dumps({
            "type": "clarification",
            "content": "Sorry, I hit an error. Please try again."
        })

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
            "last_prompt": None
        }
        ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone, **fields):
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated {phone}: {fields}")

def build_prompt(user, text):
    name_line = f'User name: "{user["name"]}"\n' if user.get("name") else ""
    return (
        BASE_PROMPT + "\n" +
        name_line +
        f'User: "{text}"\nJSON:'
    )

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    # Verification
    if request.method == "GET":
        if request.args.get("hub.mode")=="subscribe" and request.args.get("hub.verify_token")==VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json or {}
    entries = data.get("entry", [])
    if not entries or not entries[0].get("changes"):
        return "OK", 200

    msg = entries[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")
    text  = msg.get("text", {}).get("body", "").strip()
    if not phone or not text:
        return "OK", 200

    user = get_or_create_user(phone)
    now  = datetime.utcnow()

    # Credit reset & usage
    if user["account_type"] == "free":
        rt = user["credit_reset"]
        if hasattr(rt, "to_datetime"):
            rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo:
            rt = rt.replace(tzinfo=None)
        if now >= rt:
            update_user(phone, credit_remaining=20, credit_reset=now+timedelta(days=1))
            user["credit_remaining"] = 20
        if user["credit_remaining"] <= 0:
            send_text(phone, "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # Build and send to Gemini
    prompt = build_prompt(user, text)
    raw = get_gemini(prompt)
    cleaned = strip_fences(raw)

    try:
        j = json.loads(cleaned)
        typ = j.get("type")
        content = j.get("content", "")
    except Exception:
        logger.error(f"JSON parse error: {cleaned}")
        typ = "answer"
        content = cleaned

    # Reply
    send_text(phone, content)
    if typ == "answer":
        send_buttons(phone)

    # Save last_prompt for “explain_more”
    update_user(phone, last_prompt=prompt)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
