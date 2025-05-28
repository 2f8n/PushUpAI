import os
import re
import json
import logging
from flask import Flask, request
import requests
from google.cloud import speech_v1p1beta1 as speech
from google.cloud import vision
import google.generativeai as genai

app = Flask(__name__)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

# Configure Gemini AI client
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Google Cloud clients for speech and vision
speech_client = speech.SpeechClient()
vision_client = vision.ImageAnnotatorClient()

# Load prompt once at startup
with open("studymate_prompt.txt", "r", encoding="utf-8") as f:
    BASE_PROMPT = f.read()

def strip_fences(text):
    """Strip triple backticks and 'json' from Gemini response."""
    return re.sub(r"^```json\s*|```$", "", text.strip(), flags=re.MULTILINE)

def clean_text(text: str) -> str:
    """Clean and normalize text with proper line breaks."""
    text = re.sub(r'(\\n|/n/)+', '\n', text)    # fix escaped or weird newlines
    text = re.sub(r'\n+', '\n', text)           # collapse multiple newlines to one
    lines = [line.strip() for line in text.split('\n')]
    return '\n'.join(line for line in lines if line).strip()

def send_text(phone, message):
    """Send text message via WhatsApp."""
    logger.info(f"Sending message to {phone}")
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": message}
    }
    resp = requests.post(url, headers=headers, json=data)
    resp.raise_for_status()
    return resp.json()

def send_buttons(phone):
    """Send academic buttons only."""
    logger.info(f"Sending buttons to {phone}")
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "yes", "title": "Yes"}},
                    {"type": "reply", "reply": {"id": "no", "title": "No"}}
                ]
            }
        }
    }
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()

def transcribe_audio(audio_url):
    """Download and transcribe audio with Google Speech-to-Text."""
    audio_resp = requests.get(audio_url)
    audio_resp.raise_for_status()
    audio_content = audio_resp.content

    audio = speech.RecognitionAudio(content=audio_content)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
        language_code="en-US",
        audio_channel_count=1,
    )
    response = speech_client.recognize(config=config, audio=audio)
    if response.results:
        return response.results[0].alternatives[0].transcript
    return ""

def extract_text_from_image(image_url):
    """Download image and extract text using Google Vision OCR."""
    img_resp = requests.get(image_url)
    img_resp.raise_for_status()
    image_content = img_resp.content

    image = vision.Image(content=image_content)
    response = vision_client.text_detection(image=image)
    if response.text_annotations:
        return response.text_annotations[0].description
    return ""

def call_gemini(prompt):
    """Send prompt to Gemini and return raw response content."""
    response = model.generate_message(prompt=prompt, temperature=0.2, max_output_tokens=1024)
    return response.candidates[0].content

def build_prompt(user_message):
    """Compose final Gemini prompt by inserting user message into base prompt."""
    # Insert the user message safely into the prompt
    return BASE_PROMPT.replace("{user_message}", user_message.strip())

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # Verification handshake for webhook
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        else:
            return "Verification failed", 403

    data = request.get_json()
    logger.info(f"Webhook received: {json.dumps(data)}")

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        messages = value.get("messages", [])
        if not messages:
            return "No messages", 200

        msg = messages[0]
        phone = msg["from"]
        msg_type = msg["type"]

        if msg_type == "text":
            user_text = msg["text"]["body"]
            user_text_clean = user_text.strip()

        elif msg_type == "audio":
            audio_url = msg["audio"]["url"]
            try:
                user_text_clean = transcribe_audio(audio_url)
            except Exception as e:
                logger.error(f"Audio transcription error: {e}")
                user_text_clean = "Sorry, I had trouble processing your audio. Please try again."

        elif msg_type == "image":
            image_url = msg["image"]["url"]
            try:
                user_text_clean = extract_text_from_image(image_url)
                if not user_text_clean:
                    user_text_clean = "Sorry, I could not detect any text in your image."
            except Exception as e:
                logger.error(f"Image OCR error: {e}")
                user_text_clean = "Sorry, I had trouble processing your image. Please try again."

        else:
            send_text(phone, "Sorry, I can only process text, voice, or images right now.")
            return "OK", 200

        # Build Gemini prompt
        prompt = build_prompt(user_text_clean)
        logger.info(f"Prompt sent to Gemini:\n{prompt}")

        raw_response = call_gemini(prompt)
        logger.info(f"Raw Gemini response:\n{raw_response}")

        # Clean fences and parse JSON
        cleaned = strip_fences(raw_response)
        try:
            resp_json = json.loads(cleaned)
            rtype = resp_json.get("type", "answer")
            content = resp_json.get("content", "")
        except Exception:
            logger.warning("Failed to parse Gemini response as JSON. Sending raw cleaned text.")
            rtype = "answer"
            content = cleaned

        # Clean up content text
        content_cleaned = clean_text(content)
        send_text(phone, content_cleaned)

        # Send buttons ONLY for academic answers (heuristic)
        if rtype == "answer":
            lower_content = content_cleaned.lower()
            academic_keywords = ["essay", "project", "exam", "solution", "question", "assignment"]
            if any(word in lower_content for word in academic_keywords):
                send_buttons(phone)

        return "OK", 200

    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return "Error", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
