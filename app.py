from flask import Flask, request
import os
import json
import requests
import pdfplumber
from PIL import Image
import pytesseract
import google.generativeai as genai

app = Flask(__name__)

# ─── Environment Variables ───────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")

# ─── Initialize Gemini ───────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-1.5-pro-002"

# ─── In-Memory Conversation State ────────────────────────────────────────
# Holds last parsed file text per user for this session only
user_states = {}  # e.g. { phone_number: { "last_file_text": "...", "last_file_type": "pdf" } }

# ─── Persistent User Memory ──────────────────────────────────────────────
def memory_path(phone): 
    return f"memory/{phone}.json"

def load_user_memory(phone):
    try:
        with open(memory_path(phone)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_user_memory(phone, data):
    os.makedirs("memory", exist_ok=True)
    with open(memory_path(phone), "w") as f:
        json.dump(data, f)

# ─── File Parsing Helpers ────────────────────────────────────────────────
def download_media(url, dest_path):
    resp = requests.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path

def parse_pdf(path):
    text_pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text_pages.append(page.extract_text() or "")
    return "\n".join(text_pages)

def parse_image(path):
    img = Image.open(path)
    return pytesseract.image_to_string(img)

# ─── WhatsApp Message Sender ─────────────────────────────────────────────
def send_whatsapp_message(phone, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    resp = requests.post(url, headers=headers, json=payload)
    print("Sent to WhatsApp:", resp.status_code, resp.text)

# ─── Generate AI Reply ────────────────────────────────────────────────────
def get_gemini_reply(prompt, name="Student"):
    system_prompt = f"""
You are StudyMate AI, a passionate, mentor-style tutor (founded by ByteWave Media, mention only if asked).
Use name: {name}.  
Answer clearly, step by step, skipping any greeting intros.  
Only after multi-step explanations should you send a follow-up check.  
"""
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(system_prompt + "\n" + prompt)
        return response.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, something went wrong. Try again soon!"

# ─── Webhook Endpoint ────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode, token, challenge = (request.args.get("hub.mode"),
                                   request.args.get("hub.verify_token"),
                                   request.args.get("hub.challenge"))
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    # POST: handle incoming messages
    incoming = request.json
    change = incoming["entry"][0]["changes"][0]["value"]
    if "messages" not in change:
        return "OK", 200

    msg   = change["messages"][0]
    phone = msg["from"]
    text  = msg.get("text", {}).get("body", "").strip().lower()

    # Load persistent memory (name, etc.)
    memory = load_user_memory(phone)

    # Initialize ephemeral state if needed
    if phone not in user_states:
        user_states[phone] = {}

    # 1) Onboarding: collect full name
    if "name" not in memory:
        # Only accept multi-word as “full name”
        if len(text.split()) >= 2:
            memory["name"] = msg["text"]["body"].strip()
            save_user_memory(phone, memory)
            send_whatsapp_message(phone,
                f"Nice to meet you, {memory['name']}! What would you like to study today?"
            )
        else:
            send_whatsapp_message(phone,
                "Hey! Could you share your full first and last name so I know what to call you?"
            )
        return "OK", 200

    # 2) Media upload handling (PDF or image)
    mtype = msg.get("type")
    if mtype in ("document", "image"):
        media_id = msg[mtype]["id"]
        # 2a) fetch URL
        meta = requests.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            params={"fields": "url", "access_token": ACCESS_TOKEN}
        ).json()
        url = meta.get("url")
        # 2b) download to temp
        os.makedirs("uploads", exist_ok=True)
        path = download_media(url, f"uploads/{media_id}")
        # 2c) parse
        if mtype == "document":
            parsed = parse_pdf(path); ftype = "PDF"
        else:
            parsed = parse_image(path); ftype = "image"
        # 2d) store parsed text in ephemeral state
        user_states[phone]["last_file_text"] = parsed
        user_states[phone]["last_file_type"] = ftype.lower()
        # 2e) ask next action
        send_whatsapp_message(phone,
            f"Got your {ftype}! What would you like me to do with it? "
            f"Reply **summarize** or **quiz**."
        )
        return "OK", 200

    # 3) Commands on parsed text
    if text == "summarize":
        parsed = user_states[phone].get("last_file_text")
        if not parsed:
            send_whatsapp_message(phone, "I don't see any recent file. Please upload first.")
        else:
            prompt = f"Please summarize the following content:\n\n{parsed}"
            summary = get_gemini_reply(prompt, name=memory["name"])
            send_whatsapp_message(phone, summary)
        return "OK", 200

    if text == "quiz":
        parsed = user_states[phone].get("last_file_text")
        if not parsed:
            send_whatsapp_message(phone, "I don't see any recent file. Please upload first.")
        else:
            prompt = f"Create a 5-question quiz (with answers) based on this text:\n\n{parsed}"
            quiz = get_gemini_reply(prompt, name=memory["name"])
            send_whatsapp_message(phone, quiz)
        return "OK", 200

    # 4) Regular academic Q&A
    # Build a user-specific prompt
    user_question = msg.get("text", {}).get("body", "").strip()
    ai_reply = get_gemini_reply(f"Question: {user_question}", name=memory["name"])
    send_whatsapp_message(phone, ai_reply)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
