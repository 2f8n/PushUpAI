import os
import json
import logging
import tempfile
import threading
from collections import deque
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

from pydub import AudioSegment
import numpy as np
from google.cloud import speech_v1p1beta1 as speech

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Environment variables check
for v in ("VERIFY_TOKEN","ACCESS_TOKEN","PHONE_NUMBER_ID","GEMINI_API_KEY","GOOGLE_APPLICATION_CREDENTIALS"):
    if not os.getenv(v):
        logger.error(f"Missing environment variable: {v}")
        raise SystemExit("Please set all required environment variables.")

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT",10000))

# Initialize Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Firebase with secret file path from Render secrets folder
cred_path = "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Initialize Google Cloud Speech Client
speech_client = speech.SpeechClient()

# System prompt load
with open("studymate_prompt.txt","r") as f:
    SYSTEM_PROMPT = f.read().strip()

app = Flask(__name__)
sessions = {}  # phone -> {"history": deque(maxlen=5), "voice_cache": {}, "voice_profile": {}}

def ensure_session(phone):
    if phone not in sessions:
        sessions[phone] = {
            "history": deque(maxlen=5),
            "voice_cache": {},      # Cache audio transcription by media_id
            "voice_profile": {}     # Placeholder for voice profile data
        }
    return sessions[phone]

def safe_post(url, payload):
    try:
        r = requests.post(url,
            headers={"Authorization":f"Bearer {ACCESS_TOKEN}",
                     "Content-Type":"application/json"},
            json=payload)
        if r.status_code not in (200,201):
            logger.error(f"WhatsApp API {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed WhatsApp send")

def send_text(phone, text):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",{
        "messaging_product":"whatsapp","to":phone,
        "type":"text","text":{"body":text}
    })

def send_buttons(phone):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",{
        "messaging_product":"whatsapp","to":phone,
        "type":"interactive","interactive":{
            "type":"button","body":{"text":"Did that make sense to you?"},
            "action":{"buttons":[
                {"type":"reply","reply":{"id":"understood","title":"Understood"}},
                {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
            ]}
        }
    })

def strip_fences(t):
    t = t.strip()
    if t.startswith("```") and t.endswith("```"):
        t = t[3:-3].strip()
    return t

def preprocess_audio(input_file_path):
    # Load audio with pydub
    audio = AudioSegment.from_file(input_file_path)
    # Normalize audio loudness
    change_in_dBFS = -20.0 - audio.dBFS
    normalized_audio = audio.apply_gain(change_in_dBFS)
    # Remove leading/trailing silence (threshold -50dBFS)
    trimmed_audio = normalized_audio.strip_silence(silence_thresh=-50)
    # Export to wav 16kHz mono (Google Speech API requirement)
    temp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    trimmed_audio.set_frame_rate(16000).set_channels(1).export(temp_wav.name, format="wav")
    return temp_wav.name

def transcribe_audio_google(file_path, language_codes=None):
    # Prepare recognition config
    if language_codes is None:
        language_codes = ["en-US"]  # Default to English; you can extend with others

    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code=language_codes[0],
        alternative_language_codes=language_codes[1:] if len(language_codes)>1 else None,
        enable_automatic_punctuation=True,
        model="default",
        use_enhanced=True
    )

    with open(file_path, "rb") as audio_file:
        content = audio_file.read()

    audio = speech.RecognitionAudio(content=content)

    response = speech_client.recognize(config=config, audio=audio)

    # Combine transcriptions with confidence scores
    transcriptions = []
    for result in response.results:
        transcriptions.append(result.alternatives[0].transcript)

    full_transcript = " ".join(transcriptions).strip()
    confidence = max((alt.confidence for result in response.results for alt in result.alternatives), default=0.0)

    return full_transcript, confidence

def process_voice_message(phone, media_id, user):
    sess = ensure_session(phone)
    voice_cache = sess["voice_cache"]

    # Check cache
    if media_id in voice_cache:
        logger.info(f"Using cached transcription for media_id {media_id}")
        transcript = voice_cache[media_id]
        process_user_message(phone, transcript, user)
        return

    # Download audio file from WhatsApp
    media_url = f"https://graph.facebook.com/v15.0/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        media_response = requests.get(media_url, headers=headers)
        media_response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_file.write(media_response.content)
            tmp_file_path = tmp_file.name
        # Preprocess audio: normalize, trim silence, convert wav
        wav_path = preprocess_audio(tmp_file_path)

        # Transcribe audio with confidence threshold & multi-lang support
        transcript, confidence = transcribe_audio_google(wav_path, language_codes=["en-US","ar-LB","fr-FR"])

        # Cache transcription
        voice_cache[media_id] = transcript

        logger.info(f"Transcription result: '{transcript}' with confidence {confidence}")

        # Threshold confidence to re-ask if needed
        if confidence < 0.6 or not transcript.strip():
            send_text(phone, "Sorry, I couldn’t clearly understand the audio. Could you please repeat it or try typing?")
            return

        # Process transcription as user message
        process_user_message(phone, transcript, user)

    except Exception as e:
        logger.exception("Error processing voice message")
        send_text(phone, "Sorry, something went wrong while processing your voice message. Please try again.")

def get_gemini(prompt):
    try:
        return model.generate_content(prompt).text.strip()
    except Exception:
        logger.exception("Gemini error")
        return json.dumps({"type":"clarification","content":"Sorry, I encountered an error. Please try again."})

def get_or_create_user(phone):
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {"phone": phone, "name": None,
                "account_type": "free", "credit_remaining": 20,
                "credit_reset": datetime.utcnow() + timedelta(days=1),
                "last_prompt": None}
        ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone, **fields):
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated user {phone} with {fields}")

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

def process_user_message(phone, text, user):
    sess = ensure_session(phone)
    sess["history"].append(text)

    # Reset conversation context if subject/topic changed (simple heuristic)
    if "switch subject" in text.lower() or "new topic" in text.lower():
        sess["history"].clear()
        sess["history"].append(text)
        logger.info(f"Context reset due to topic switch for {phone}")

    # Build prompt for Gemini
    prompt = build_prompt(user, list(sess["history"])[:-1], text)
    raw_response = get_gemini(prompt)

    # Clean raw Gemini response, remove markdown fences
    clean_response = strip_fences(raw_response)

    # Parse JSON response safely
    try:
        j = json.loads(clean_response)
        rtype = j.get("type")
        content = j.get("content", "")
    except Exception:
        rtype = "answer"
        content = clean_response

    # Avoid sending raw JSON, only send the 'content' text
    if not isinstance(content, str):
        content = str(content)

    send_text(phone, content)

    # Send buttons ONLY on academic answer types (per mission rule)
    if rtype == "answer":
        send_buttons(phone)

    # Update user with last prompt and credits management
    update_user(phone, last_prompt=prompt)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if (request.args.get("hub.mode") == "subscribe" and
            request.args.get("hub.verify_token") == VERIFY_TOKEN):
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
    now = datetime.utcnow()

    # Name onboarding
    if user["name"] is None:
        text = msg.get("text", {}).get("body", "")
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
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great—what’s next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more = get_gemini(user["last_prompt"] + "\n\nPlease explain in more detail.")
                more_clean = strip_fences(more)
                send_text(phone, more_clean)
                send_buttons(phone)
        return "OK", 200

    # Handle voice message type
    if msg.get("type") == "audio" or msg.get("type") == "voice":
        media = msg.get("audio") or msg.get("voice")
        if media:
            media_id = media.get("id")
            if media_id:
                threading.Thread(target=process_voice_message, args=(phone, media_id, user)).start()
                # Acknowledge quickly
                send_text(phone, "Processing your voice message...")
                return "OK", 200

    # Handle text messages
    text = msg.get("text", {}).get("body", "").strip()
    if text:
        process_user_message(phone, text, user)

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
