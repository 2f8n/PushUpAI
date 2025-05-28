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

# Load environment variables
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

if not all([VERIFY_TOKEN, ACCESS_TOKEN, PHONE_NUMBER_ID, GEMINI_API_KEY]):
    logger.error("Missing required environment variables")
    raise SystemExit("Missing required environment variables")

# Initialize Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Firebase
cred = credentials.Certificate("/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Initialize Google Vision client
vision_client = vision.ImageAnnotatorClient()

# Initialize Google Speech client
speech_client = speech.SpeechClient()

# Load system prompt from file
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
            logger.error(f"WhatsApp API error {r.status_code}: {r.text}")
        return r
    except Exception:
        logger.exception("Failed WhatsApp send")
        return None

def send_text(phone, text):
    return safe_post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": text},
        },
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
    if t.startswith("```") and t.endswith("```"):
        t = t[3:-3].strip()
    return t

def get_gemini(prompt):
    try:
        return model.generate_content(prompt).text.strip()
    except Exception:
        logger.exception("Gemini API error")
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
    logger.info(f"Updated user {phone} with {fields}")

def build_prompt(user, history, message, user_first_name):
    parts = [SYSTEM_PROMPT]
    if user_first_name:
        parts.append(f'User name: "{user_first_name}"')
    if history:
        parts.append("Recent messages:")
        parts.extend(f"- {h}" for h in history)
    parts.append(f'Current message: "{message}"')
    parts.append("JSON:")
    return "\n".join(parts)

def get_whatsapp_media_url(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json().get("url")

def download_media(url):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.content

def analyze_image_with_vision(image_bytes):
    image = vision.Image(content=image_bytes)
    response = vision_client.text_detection(image=image)
    texts = response.text_annotations
    if texts:
        return texts[0].description.strip()
    return ""

def transcribe_audio_with_speech(audio_bytes):
    try:
        audio = speech.RecognitionAudio(content=audio_bytes)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED,
            language_code="en-US",
            audio_channel_count=1,
        )
        response = speech_client.recognize(config=config, audio=audio)
        transcript = ""
        for result in response.results:
            transcript += result.alternatives[0].transcript
        return transcript.strip()
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

    user = get_or_create_user(phone)
    sess = ensure_session(phone)
    history = list(sess["history"])
    now = datetime.utcnow()

    # Onboarding
    if user["name"] is None:
        text = msg.get("text", {}).get("body", "").strip()
        if text and len(text.split()) >= 2:
            first_name = text.split()[0]
            update_user(phone, name=text)
            send_text(phone, f"What would you like to study today, {first_name}?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # Credit reset & decrement
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

    # Interactive buttons reply
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great—what’s next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more = get_gemini(user["last_prompt"] + "\n\nPlease explain in more detail.")
                clean_more = strip_fences(more)
                send_text(phone, clean_more)
                send_buttons(phone)
        return "OK", 200

    # Handle message content
    gemini_input = ""
    text = msg.get("text", {}).get("body", "").strip()

    if text:
        gemini_input = text

    elif "image" in msg:
        media_id = msg["image"]["id"]
        try:
            media_url = get_whatsapp_media_url(media_id)
            image_bytes = download_media(media_url)
            extracted_text = analyze_image_with_vision(image_bytes)
            if extracted_text:
                # Clean up newlines/spaces for prompt
                extracted_text = "\n".join(line.strip() for line in extracted_text.splitlines() if line.strip())
                gemini_input = f"I received this text from an image you sent:\n{extracted_text}"
            else:
                gemini_input = "I received an image but couldn't extract readable text. Please describe it."
        except Exception as e:
            logger.error(f"Image processing error: {e}")
            gemini_input = "Sorry, I had trouble processing your image. Please try again."

    elif "audio" in msg:
        media_id = msg["audio"]["id"]
        try:
            media_url = get_whatsapp_media_url(media_id)
            audio_bytes = download_media(media_url)
            transcript = transcribe_audio_with_speech(audio_bytes)
            if transcript:
                gemini_input = transcript
            else:
                gemini_input = "Sorry, I couldn't understand the audio. Please try again."
        except Exception as e:
            logger.error(f"Audio processing error: {e}")
            gemini_input = "Sorry, I had trouble processing your audio. Please try again."

    else:
        # Unsupported type
        return "OK", 200

    # Append to history
    sess["history"].append(gemini_input)

    def clean_text(t):
        # Remove empty lines and strip spaces, unify newlines
        return '\n'.join(line.strip() for line in t.splitlines() if line.strip())

    gemini_input_clean = clean_text(gemini_input)

    user_first_name = user["name"].split()[0] if user["name"] else None
    prompt = build_prompt(user, history, gemini_input_clean, user_first_name)

    raw_response = get_gemini(prompt)
    logger.info(f"Gemini raw response:\n{raw_response}")

    clean_response = strip_fences(raw_response)

    try:
        response_json = json.loads(clean_response)
        rtype = response_json.get("type", "answer")
        content = response_json.get("content", "")
    except Exception:
        rtype = "answer"
        content = clean_response

    content = clean_text(content)

    send_text(phone, content)

    academic_keywords = ["step-by-step", "essay", "project", "exam", "solution", "problem", "question"]
    if rtype == "answer" and any(k in content.lower() for k in academic_keywords):
        send_buttons(phone)

    update_user(phone, last_prompt=prompt)
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
