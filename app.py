#!/usr/bin/env python3
"""
StudyMate AI: WhatsApp tutor with dynamic NLU, ephemeral 5-message history, and interactive Q&A.
Features:
 - Onboarding (name collection stored in Firestore)
 - Free vs. Premium credit management (stored in Firestore)
 - In-memory last-5-message history per user (never persisted)
 - Every non-interactive message sent to Gemini with JSON contract:
     { "type": "clarification" | "answer", "content": "…" }
 - Buttons only after “answer” replies
"""

import os, re, json, logging
from collections import deque
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

# — Logging —
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# — Load environment variables —
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# — Required env vars —
REQUIRED = ("VERIFY_TOKEN","ACCESS_TOKEN","PHONE_NUMBER_ID","GEMINI_API_KEY")
for v in REQUIRED:
    if not os.getenv(v):
        logger.error(f"Missing environment variable: {v}")
        raise SystemExit("Please set all required environment variables.")

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))

# — Initialize Gemini —
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# — Initialize Firestore (name, credits, last_prompt) —
KEY_FILE    = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH = f"/etc/secrets/{KEY_FILE}"
cred_path   = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILE
cred        = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# — Base system prompt for Gemini —
BASE_PROMPT = """
You are a friendly academic tutor.
Always respond with JSON ONLY:
  { "type": "clarification" | "answer", "content": "…" }
Rules:
- If name is unknown, type=clarification and ask for full name.
- If you need context (subject/topic/task), type=clarification and ask exactly one question.
- Otherwise type=answer with step-by-step academic help (≤3 sentences/step).
"""

app = Flask(__name__)

# — In-memory per-user session history (last 5 user messages) —
sessions = {}  # phone → {"history": deque(maxlen=5)}

def ensure_session(phone):
    if phone not in sessions:
        sessions[phone] = {"history": deque(maxlen=5)}
    return sessions[phone]

# — HTTP helpers —

def safe_post(url, payload):
    try:
        r = requests.post(url,
                          headers={"Authorization":f"Bearer {ACCESS_TOKEN}",
                                   "Content-Type":"application/json"},
                          json=payload)
        if r.status_code != 200:
            logger.error(f"WhatsApp API {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed WhatsApp send")

def send_text(phone, text):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    })

def send_buttons(phone):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense to you?"},
            "action": {"buttons": [
                {"type":"reply","reply":{"id":"understood","title":"Understood"}},
                {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
            ]}
        }
    })

# — Gemini & parsing —

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
        return json.dumps({
            "type":"clarification",
            "content":"Sorry, I encountered an error. Please try again."
        })

# — Firestore user helpers —

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

# — Build the dynamic prompt including last 5 messages —

def build_prompt(user, history, text):
    """
    Inject user's name (if known), recent history, and current message.
    """
    name_line = f'User name: "{user["name"]}"\n' if user.get("name") else ""
    hist_block = ""
    if history:
        hist_block = "Recent messages:\n" + "\n".join(f"- {m}" for m in history) + "\n"
    return (
        BASE_PROMPT.strip() + "\n" +
        name_line +
        hist_block +
        f'Current message: "{text}"\nJSON:'
    )

# — Webhook endpoint —

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    # Verification handshake
    if request.method == "GET":
        if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json or {}
    entry = data.get("entry", [])
    if not entry or not entry[0].get("changes"):
        return "OK", 200

    msg = entry[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")
    text  = msg.get("text", {}).get("body", "").strip()
    if not phone or not text:
        return "OK", 200

    user = get_or_create_user(phone)
    now  = datetime.utcnow()

    # — Onboarding: collect full name first —
    if user.get("name") is None:
        if len(text.split()) >= 2:
            update_user(phone, name=text)
            send_text(phone, f"Nice to meet you, {text}! What would you like to study today?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # — Free vs. Premium credit logic —
    if user["account_type"] == "free":
        rt = user["credit_reset"]
        if hasattr(rt, "to_datetime"): rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo: rt = rt.replace(tzinfo=None)
        if now >= rt:
            update_user(phone,
                        credit_remaining=20,
                        credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user["credit_remaining"] <= 0:
            send_text(phone, "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # — Interactive button replies —
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            btn = ir["button_reply"]["id"]
            if btn == "understood":
                send_text(phone, "Fantastic! 🎉 What’s next?")
            elif btn == "explain_more" and user.get("last_prompt"):
                detail = get_gemini(user["last_prompt"] + "\n\nPlease explain in more detail.")
                send_text(phone, detail)
                send_buttons(phone)
            return "OK", 200

    # — Ensure session history exists and capture history —
    sess = ensure_session(phone)
    history = list(sess["history"])  # last up to 5 messages, excluding current
    sess["history"].append(text)     # now include current for future prompts

    # — Build and send to Gemini —
    prompt = build_prompt(user, history, text)
    raw    = get_gemini(prompt)
    clean  = strip_fences(raw)

    # — Parse Gemini’s JSON reply —
    try:
        j = json.loads(clean)
        typ = j.get("type")
        content = j.get("content", "")
    except Exception:
        logger.error(f"JSON parse failed: {clean}")
        typ = "answer"
        content = clean

    # — Reply —
    send_text(phone, content)
    if typ == "answer":
        send_buttons(phone)

    # — Save last_prompt for “explain_more” follow-up —
    update_user(phone, last_prompt=prompt)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
