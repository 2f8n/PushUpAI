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
from google.cloud import speech

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Environment variables check
for v in ("VERIFY_TOKEN", "ACCESS_TOKEN", "PHONE_NUMBER_ID", "GEMINI_API_KEY"):
    if not os.getenv(v):
        logger.error(f"Missing environment variable: {v}")
        raise SystemExit("Please set all required environment variables.")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

# Initialize Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Firebase (service account from mounted secret)
firebase_secret_path = "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
cred = credentials.Certificate(firebase_secret_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Initialize Google Speech client
speech_client = speech.SpeechClient()

# Load system prompt
with open("studymate_prompt.txt", "r") as f:
    SYSTEM_PROMPT = f.read().strip()

app = Flask(__name__)
sessions = {}  # phone -> {"history": deque(maxlen=5)}

def ensure_session(phone):
    if phone not in sessions:
        sessions[phone] = {"history": deque(maxlen=5)}
    return sessions[phone]

def safe_post(url, payload):
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code not in (200, 201):
            logger.error(f"WhatsApp API {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed WhatsApp send")

def send_text(phone, text):
    safe_post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}},
    )

def send_buttons(phone):
    safe_post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "Did that make sense to you?"},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                        {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}},
                    ]
                },
            },
        },
    )

def strip_fences(t):
    t = t.strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
    return t

def get_gemini(prompt):
    try:
        response = model.generate_content(prompt)
        logger.info(f"Gemini response raw:\n{response.text}")
        return response.text.strip()
    except Exception:
        logger.exception("Gemini error")
        return json.dumps({"type": "clarification", "content": "Sorry, I encountered an error. Please try again."})

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
    logger.info(f"Updated {phone}: {fields}")

def build_prompt(user, history, message):
    parts = [SYSTEM_PROMPT]
    if user.get("name"):
        parts.append(f'User name: "{user["name"].split()[0]}"')
    if history:
        parts.append("Recent messages:")
        parts.extend(f"- {h}" for h in history)
    parts.append(f'Current message: "{message}"')
    parts.append("JSON:")
    return "\n".join(parts)

def transcribe_audio_from_url(media_url):
    try:
        # Download the audio file content
        response = requests.get(media_url)
        response.raise_for_status()
        audio_content = response.content
    except Exception as e:
        logger.error(f"Failed to download media: {e}")
        return None

    audio = speech.RecognitionAudio(content=audio_content)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
        sample_rate_hertz=16000,
        language_code="en-US",
        audio_channel_count=1,
    )

    try:
        response = speech_client.recognize(config=config, audio=audio)
        if response.results:
            transcript = response.results[0].alternatives[0].transcript
            logger.info(f"Transcribed audio: {transcript}")
            return transcript
        else:
            return None
    except Exception as e:
        logger.error(f"Speech recognition error: {e}")
        return None

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json or {}
    entry = data.get("entry", [])
    if not entry or not entry[0].get("changes"):
        return "OK", 200

    msg = entry[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")
    if not phone:
        return "OK", 200

    msg_type = msg.get("type")
    text = ""

    # Handle voice/audio message transcription
    if msg_type == "audio" or msg_type == "voice":
        media_id = msg.get(msg_type, {}).get("id")
        if not media_id:
            send_text(phone, "Sorry, I couldn't find the audio to process.")
            return "OK", 200

        # Fetch media URL from WhatsApp
        media_url_resp = requests.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        if media_url_resp.status_code != 200:
            send_text(phone, "Sorry, I couldn't retrieve your audio. Please try again.")
            return "OK", 200

        media_url = media_url_resp.json().get("url")
        if not media_url:
            send_text(phone, "Sorry, I couldn't retrieve your audio URL. Please try again.")
            return "OK", 200

        transcript = transcribe_audio_from_url(media_url)
        if not transcript:
            send_text(phone, "Sorry, I couldn't understand the audio. Please try again.")
            return "OK", 200

        text = transcript
        logger.info(f"User {phone} audio transcribed to: {text}")

    # Handle text message
    elif msg_type == "text":
        text = msg.get("text", {}).get("body", "").strip()

    else:
        # Ignore unsupported message types for now
        return "OK", 200

    if not text:
        return "OK", 200

    user = get_or_create_user(phone)
    now = datetime.utcnow()

    # Name onboarding
    if user["name"] is None:
        if len(text.split()) >= 2:
            first_name = text.split()[0]
            update_user(phone, name=text)
            send_text(phone, f"What would you like to study today, {first_name}?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # Credits reset & check
    if user["account_type"] == "free":
        rt = user["credit_reset"]
        if hasattr(rt, "to_datetime"):
            rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo:
            rt = rt.replace(tzinfo=None)
        if now >= rt:
            update_user(phone, credit_remaining=20, credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user["credit_remaining"] <= 0:
            send_text(phone, "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # Interactive replies (buttons)
    if msg_type == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great—what’s next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more = get_gemini(user["last_prompt"] + "\n\nPlease explain in more detail.")
                content = strip_fences(more)
                send_text(phone, content)
                send_buttons(phone)
        return "OK", 200

    sess = ensure_session(phone)
    history = list(sess["history"])
    sess["history"].append(text)

    prompt = build_prompt(user, history, text)
    raw = get_gemini(prompt)
    clean = strip_fences(raw)

    try:
        j = json.loads(clean)
        rtype = j.get("type")
        content = j.get("content", "")
    except Exception:
        rtype = "answer"
        content = clean

    # Ensure content is string
    if not isinstance(content, str):
        content = str(content)

    # **FIX: Send only content text (no raw JSON)**
    send_text(phone, content)

    # Send buttons ONLY if AI gave academic answer (type == "answer")
    if rtype == "answer":
        send_buttons(phone)

    update_user(phone, last_prompt=prompt)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
