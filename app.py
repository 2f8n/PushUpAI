import os
import json
import random
import logging
from flask import Flask, request
import requests
import pdfplumber
from PIL import Image
from pytesseract import image_to_string, TesseractNotFoundError
import google.generativeai as genai

# ─── Configuration ─────────────────────────────────────────────────────────────

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Env vars you must set in Render or your hosting:
WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID   = os.getenv("PHONE_NUMBER_ID")
GENAI_API_KEY     = os.getenv("GENAI_API_KEY")

# Directory to store per‐user JSON memory
MEMORY_DIR = "memory"
os.makedirs(MEMORY_DIR, exist_ok=True)

# Tell the client library your API key
genai.configure(api_key=GENAI_API_KEY)

# Variants for asking a new user’s name
NAME_REQUESTS = [
    "Hey there! What name should I save you under in my contacts so I know who I'm talking to?",
    "Hi! Just so I can add you to my contacts, what's your name?",
    "Hello! Could you share your name so I can save it in my contacts?",
    "Nice to meet you! What should I call you in my contacts?",
    "Welcome! How should I save your name in my contacts?"
]

# Examples for broad‐topic follow-ups
SUBJECT_EXAMPLES = {
    "english": ["Parts of speech", "Tenses", "Subject-verb agreement", "Punctuation"],
    "grammar": ["Verb conjugations", "Sentence structure", "Active vs passive voice", "Common errors"],
    "chemistry": ["Periodic table", "Stoichiometry", "Chemical bonding", "Acid-base reactions"]
}

# ─── Helpers ───────────────────────────────────────────────────────────────────

def send_whatsapp(payload):
    """POST a message to the WhatsApp Cloud API."""
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    resp = requests.post(
        f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages",
        json=payload, headers=headers
    )
    resp.raise_for_status()

def send_text(to, text):
    send_whatsapp({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    })

def send_buttons(to, text, buttons):
    """buttons is a list of dicts like:
    {"type":"reply","reply":{"id":"UNDERSTOOD","title":"Understood"}}"""
    send_whatsapp({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": buttons}
        }
    })

def download_media(media_id, dest):
    """Fetch media URL via Graph API, then download to dest."""
    # Step 1: get the URL
    meta = requests.get(
        f"https://graph.facebook.com/v17.0/{media_id}",
        params={"fields": "url", "access_token": WHATSAPP_TOKEN}
    )
    meta.raise_for_status()
    url = meta.json()["url"]
    # Step 2: download with auth
    r = requests.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)
    return dest

def parse_pdf(path):
    txt = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            txt.append(page.extract_text() or "")
    return "\n".join(txt).strip()

def parse_image(path):
    try:
        return image_to_string(Image.open(path)).strip()
    except TesseractNotFoundError:
        return None

def call_gemini(user_msg):
    """Call Gemini via the Python library."""
    resp = genai.ChatCompletion.create(
        model="chat-bison-001",
        messages=[
            {"author": "system", "content": "You are StudyMate AI—friendly, concise, passionate, texting style."},
            {"author": "user",   "content": user_msg}
        ],
        temperature=0.7
    )
    # take the first candidate
    return resp.candidates[0].content

def is_broad_topic(text):
    words = text.strip().split()
    return len(words) <= 2 and "?" not in text

def handle_broad_topic(to, subject):
    ex = SUBJECT_EXAMPLES.get(subject.lower(),
                               ["Topic A", "Topic B", "Topic C", "Topic D"])
    prompt = (
        f"Great, {subject.capitalize()} is vast—what specifically are you interested in? "
        f"For example: {', '.join(ex[:4])}."
    )
    # only one button for broad topic
    btn = [{"type":"reply","reply":{"id":"EXPLAIN_MORE","title":"Explain More"}}]
    send_buttons(to, prompt, btn)

# ─── Webhook Endpoint ─────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    try:
        # drill into the change
        value = data["entry"][0]["changes"][0]["value"]
    except Exception:
        logging.info("Webhook received non-message event.")
        return "", 200

    # contact and message validation
    if not value.get("contacts") or not value.get("messages"):
        return "", 200

    contact = value["contacts"][0]
    phone   = contact["wa_id"]
    msg     = value["messages"][0]

    # load or init memory
    mem_file = os.path.join(MEMORY_DIR, f"{phone}.json")
    if not os.path.exists(mem_file):
        # new user: ask name
        with open(mem_file, "w") as f:
            json.dump({"awaiting_name": True}, f)
        send_text(phone, random.choice(NAME_REQUESTS))
        return "", 200

    memory = json.load(open(mem_file))

    # BUTTON REPLIES
    if msg["type"] == "interactive":
        ir = msg["interactive"]
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "UNDERSTOOD":
                send_text(phone, "Awesome, glad it helped! 😊")
                return "", 200
            if bid == "EXPLAIN_MORE":
                ans = call_gemini("Please explain more about that.")
                send_text(phone, ans)
                return "", 200

    # TEXT
    if msg["type"] == "text":
        text = msg["text"]["body"].strip()

        # if we were waiting for their name
        if memory.get("awaiting_name"):
            memory["name"] = text
            memory["awaiting_name"] = False
            with open(mem_file, "w") as f:
                json.dump(memory, f)
            send_text(phone, f"Sweet, I'll save you as *{text}*! What can I help you with today?")
            return "", 200

        # broad‐topic branch
        if is_broad_topic(text):
            handle_broad_topic(phone, text)
            return "", 200

        # regular question—send to Gemini
        ans = call_gemini(text)
        send_text(phone, ans)

        # if it looks like an academic answer (has any “?” from user)
        if "?" in text:
            btns = [
                {"type":"reply","reply":{"id":"UNDERSTOOD","title":"Understood"}},
                {"type":"reply","reply":{"id":"EXPLAIN_MORE","title":"Explain More"}}
            ]
            send_buttons(phone, "Did that make sense?", btns)

        return "", 200

    # IMAGE or DOCUMENT
    if msg["type"] in ("image", "document"):
        mtype = msg["type"]
        media_id = msg[mtype]["id"]
        os.makedirs("uploads", exist_ok=True)
        dst = f"uploads/{phone}_{media_id}"
        try:
            local = download_media(media_id, dst)
        except Exception as e:
            logging.exception("Media download failed")
            send_text(phone, "Sorry, I couldn't fetch that file.")
            return "", 200

        if mtype == "document":
            parsed = parse_pdf(local)
        else:
            parsed = parse_image(local)

        if not parsed:
            send_text(phone, "OCR isn’t available right now—please install Tesseract.")
            return "", 200

        send_text(phone, parsed)
        # follow up with academic buttons
        btns = [
            {"type":"reply","reply":{"id":"UNDERSTOOD","title":"Understood"}},
            {"type":"reply","reply":{"id":"EXPLAIN_MORE","title":"Explain More"}}
        ]
        send_buttons(phone, "Did that make sense?", btns)
        return "", 200

    return "", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
