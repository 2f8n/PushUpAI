# File: app.py

from flask import Flask, request
import os
import json
import requests
import pdfplumber
from PIL import Image
import pytesseract
import google.generativeai as genai

app = Flask(__name__)

# ─── Configuration ────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME      = "gemini-1.5-pro-002"

# ─── Initialize Gemini ───────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)

# ─── In-Memory State (per-conversation) ──────────────────────────────────
user_states = {}  
# { phone: { "last_file_text": str, "last_file_type": "pdf"/"image",
#            "last_user_q": str } }

# ─── Persistent Memory (name, etc.) ──────────────────────────────────────
def memory_path(phone): return f"memory/{phone}.json"

def load_user_memory(phone):
    try:
        return json.load(open(memory_path(phone)))
    except FileNotFoundError:
        return {}

def save_user_memory(phone, data):
    os.makedirs("memory", exist_ok=True)
    with open(memory_path(phone), "w") as f:
        json.dump(data, f)

# ─── Helpers: download & parse ───────────────────────────────────────────
def download_media(url, dest):
    r = requests.get(url); r.raise_for_status()
    with open(dest, "wb") as f: f.write(r.content)
    return dest

def parse_pdf(path):
    text_pages = []
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            text_pages.append(pg.extract_text() or "")
    return "\n".join(text_pages)

def parse_image(path):
    img = Image.open(path)
    return pytesseract.image_to_string(img)

# ─── WhatsApp Sends ──────────────────────────────────────────────────────
def send_text(phone, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    hdr = {"Authorization":f"Bearer {ACCESS_TOKEN}","Content-Type":"application/json"}
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"text","text":{"body":text}
    }
    r = requests.post(url, headers=hdr, json=payload)
    print("→ text", r.status_code, r.text)

def send_buttons(phone, text):
    """Send a message with two quick-reply buttons."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    hdr = {"Authorization":f"Bearer {ACCESS_TOKEN}","Content-Type":"application/json"}
    buttons = [
        {"type":"reply","reply":{"id":"understood","title":"Understood"}},
        {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
    ]
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":text},
            "action":{"buttons":buttons}
        }
    }
    r = requests.post(url, headers=hdr, json=payload)
    print("→ buttons", r.status_code, r.text)

# ─── Gemini Reply ────────────────────────────────────────────────────────
def get_gemini_reply(prompt, name="Student"):
    system = f"""
You are StudyMate AI, a passionate tutor (founded by ByteWave Media; mention only if asked).
Use name: {name}.  
Answer step-by-step, skipping any “Hi” intros.  
After each multi-step explanation, I will ask “Did that make sense?”
"""
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        res = model.generate_content(system + "\n" + prompt)
        return res.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I’m having trouble right now."

# ─── Webhook Endpoint ────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method=="GET":
        if (request.args.get("hub.mode")=="subscribe" and
            request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Fail", 403

    data = request.json
    change = data["entry"][0]["changes"][0]["value"]
    if "messages" not in change:
        return "OK", 200

    msg   = change["messages"][0]
    phone = msg["from"]
    mtype = msg.get("type")
    text  = msg.get("text",{}).get("body","").strip()

    # Load memory
    memory = load_user_memory(phone)
    # Ensure ephemeral state
    if phone not in user_states:
        user_states[phone] = {}

    # 1) Onboarding: ask for full name
    if "name" not in memory:
        if len(text.split()) >= 2:
            memory["name"] = text
            save_user_memory(phone, memory)
            send_text(phone, f"Great, {memory['name']}! What do you want to study today?")
        else:
            send_text(phone,
                "Hey! Could you tell me your full name (first & last)?")
        return "OK", 200

    # 2) Button reply handling
    if mtype == "interactive":
        btn = msg["interactive"]["button_reply"]["id"]
        if btn == "understood":
            send_text(phone, "Awesome! What would you like to tackle next?")
        elif btn == "explain_more":
            last_q = user_states[phone].get("last_user_q","")
            if last_q:
                prompt = f"Please explain in more detail: {last_q}"
                more = get_gemini_reply(prompt, name=memory["name"])
                # store again to allow another round
                user_states[phone]["last_user_q"] = last_q
                send_buttons(phone, more)
            else:
                send_text(phone, "Can you remind me your question?")
        return "OK", 200

    # 3) Media (PDF/Image) upload
    if mtype in ("document","image"):
        media_id = msg[mtype]["id"]
        # fetch URL
        meta = requests.get(
            f"https://graph.facebook.com/v19.0/{media_id}",
            params={"fields":"url","access_token":ACCESS_TOKEN}
        ).json()
        url = meta.get("url","")
        os.makedirs("uploads", exist_ok=True)
        path = download_media(url, f"uploads/{media_id}")
        if mtype=="document":
            parsed = parse_pdf(path)
        else:
            parsed = parse_image(path)
        user_states[phone]["last_file_text"] = parsed
        # ask next
        send_text(phone,
            f"I’ve read your {mtype.upper()}. Reply *summarize* or *quiz* when ready.")
        return "OK", 200

    # 4) Summarize or Quiz commands
    cmd = text.lower()
    if cmd == "summarize":
        parsed = user_states[phone].get("last_file_text")
        if not parsed:
            send_text(phone, "No file found—please upload first.")
        else:
            summary = get_gemini_reply(f"Summarize:\n\n{parsed}", name=memory["name"])
            # Show summary then buttons for follow-up
            user_states[phone]["last_user_q"] = f"Summarize:\n\n{parsed}"
            send_buttons(phone, summary)
        return "OK", 200

    if cmd == "quiz":
        parsed = user_states[phone].get("last_file_text")
        if not parsed:
            send_text(phone, "No file found—please upload first.")
        else:
            quiz = get_gemini_reply(f"Make a 5-question quiz on:\n\n{parsed}", name=memory["name"])
            user_states[phone]["last_user_q"] = f"Quiz:\n\n{parsed}"
            send_buttons(phone, quiz)
        return "OK", 200

    # 5) Regular academic Q&A
    # Store last question
    user_states[phone]["last_user_q"] = text
    reply = get_gemini_reply(f"Question: {text}", name=memory["name"])
    # Send with buttons
    send_buttons(phone, reply)

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
