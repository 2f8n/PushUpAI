```python
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
from google.cloud import vision
from google.cloud import speech_v1p1beta1 as speech

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# Load environment variables\VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

# Ensure all required env vars are set
if not all([VERIFY_TOKEN, ACCESS_TOKEN, PHONE_NUMBER_ID, GEMINI_API_KEY]):
    logger.error("Missing required environment variables")
    raise SystemExit("Missing required environment variables")

# Initialize services
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

cred = credentials.Certificate(
    "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
)
firebase_admin.initialize_app(cred)
db = firestore.client()

vision_client = vision.ImageAnnotatorClient()
speech_client = speech.SpeechClient()

# Load system prompt
with open("studymate_prompt.txt", "r") as f:
    SYSTEM_PROMPT = f.read().strip()

app = Flask(__name__)
sessions = {}  # phone number -> {'history': deque([...])}

# --- Helper Functions ---
def ensure_session(phone):
    return sessions.setdefault(phone, {"history": deque(maxlen=5)})

def safe_post(url, payload):
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code not in (200, 201):
            logger.error(f"WhatsApp API error {resp.status_code}: {resp.text}")
        return resp
    except Exception:
        logger.exception("Failed to send WhatsApp message")
        return None

def send_text(phone, text):
    return safe_post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}},
    )

def send_buttons(phone):
    return safe_post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "Did that make sense to you?"},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                    {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}},
                ]}
            }
        },
    )

def strip_fences_and_header(text):
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 3:
            t = parts[1]
    lines = t.splitlines()
    if lines and lines[0].strip().lower() == "json":
        lines = lines[1:]
    return "\n".join(lines).strip()

def get_gemini(prompt):
    try:
        return model.generate_content(prompt).text
    except Exception:
        logger.exception("Gemini API error")
        return json.dumps({"type": "clarification", "content": "Sorry, an error occurred. Please try again."})

def analyze_image_with_vision(image_bytes):
    image = vision.Image(content=image_bytes)
    response = vision_client.text_detection(image=image)
    texts = response.text_annotations
    return texts[0].description.strip() if texts else ""

def transcribe_audio_with_speech(audio_bytes):
    audio = speech.RecognitionAudio(content=audio_bytes)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
        language_code="en-US",
        audio_channel_count=1,
        enable_automatic_punctuation=True
    )
    resp = speech_client.recognize(config=config, audio=audio)
    return "".join(r.alternatives[0].transcript for r in resp.results).strip()

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
    logger.info(f"Updated user {phone} with {fields}")

def build_prompt(user, history, message, first_name):
    parts = [SYSTEM_PROMPT]
    if first_name:
        parts.append(f'User name: "{first_name}"')
    if history:
        parts.append("Recent messages:")
        parts.extend(f"- {h}" for h in history)
    parts.append(f'Current message: "{message}"')
    parts.append("JSON:")
    return "\n".join(parts)

# --- Webhook Endpoint ---
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json or {}
    entries = data.get("entry", [])
    if not entries or not entries[0].get("changes"):
        return "OK", 200

    msg = entries[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")
    if not phone:
        return "OK", 200

    user = get_or_create_user(phone)
    sess = ensure_session(phone)
    history = list(sess["history"])
    now = datetime.utcnow()
    first_name = user.get("name", "").split()[0] if user.get("name") else ""

    # Onboarding
    if user.get("name") is None:
        tb = msg.get("text", {}).get("body", "").strip()
        if tb and len(tb.split()) >= 2:
            update_user(phone, name=tb)
            send_text(phone, f"What would you like to study today, {tb.split()[0]}?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # Greetings
    text_body = msg.get("text", {}).get("body", "").strip().lower()
    if text_body in ["hi", "hello", "hey", "are you there"]:
        send_text(phone, f"Hi {first_name}! How can I help you study today?")
        return "OK", 200

    # Button replies
    if msg.get("type") == "interactive":
        br = msg.get("interactive", {}).get("button_reply", {})
        bid = br.get("id")
        if bid == "understood":
            send_text(phone, "Great—what’s next for your studies?")
        elif bid == "explain_more" and user.get("last_prompt"):
            more = get_gemini(user.get("last_prompt") + "\n\nPlease explain more.")
            clean = strip_fences_and_header(more)
            send_text(phone, clean)
            send_buttons(phone)
        return "OK", 200

    # Credit handling
    if user.get("account_type") == "free":
        rt = user.get("credit_reset")
        if hasattr(rt, "to_datetime"): rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo: rt = rt.replace(tzinfo=None)
        if now >= rt:
            update_user(phone, credit_remaining=20, credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user.get("credit_remaining", 0) <= 0:
            send_text(phone, "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone, credit_remaining=user.get("credit_remaining") - 1)

    # Determine input type
    if "image" in msg:
        try:
            mid = msg["image"]["id"]
            url = f"https://graph.facebook.com/v19.0/{mid}"
            meta = requests.get(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}).json()
            img_bytes = requests.get(meta.get("url")).content
            gemini_input = analyze_image_with_vision(img_bytes)
        except:
            gemini_input = "I received an image but couldn’t extract text. Please describe it."
    elif "audio" in msg:
        try:
            mid = msg["audio"]["id"]
            url = f"https://graph.facebook.com/v19.0/{mid}"
            meta = requests.get(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}).json()
            audio_bytes = requests.get(meta.get("url")).content
            gemini_input = transcribe_audio_with_speech(audio_bytes)
        except:
            send_text(phone, f"No worries, {first_name}! What can I help you with next?")
            return "OK", 200
    else:
        gemini_input = msg.get("text", {}).get("body", "").strip()

    # Append history & build prompt
    sess["history"].append(gemini_input)
    prompt = build_prompt(user, history, gemini_input, first_name)

    # Call Gemini
    raw = get_gemini(prompt)
    clean = strip_fences_and_header(raw)

    # Parse JSON
    try:
        out = json.loads(clean)
        rtype = out.get("type", "answer")
        content = out.get("content", "")
    except:
        rtype, content = "answer", clean

    # Normalize newlines
    content = content.replace("\\n", "\n").replace("/n/", "\n") if isinstance(content, str) else str(content)

    send_text(phone, content)

    # Academic buttons
    if rtype == "answer" and any(k in content.lower() for k in ["essay","problem","solution","question"]):
        send_buttons(phone)

    update_user(phone, last_prompt=prompt)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
```
