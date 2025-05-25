import os
import json
import tempfile
import logging

import requests
import pdfplumber
from PIL import Image
import google.generativeai as genai

from flask import Flask, request

# ── CONFIG & INIT ────────────────────────────────────────────────────────────

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN",    "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN",    "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "")

# Gemini (Google LLM)
genai.configure(api_key=GEMINI_API_KEY)
MODEL_ID = "models/chat-bison-001"

# where we store per-user memory
MEMORY_DIR = "memory"
os.makedirs(MEMORY_DIR, exist_ok=True)


# ── MEMORY HELPERS ────────────────────────────────────────────────────────────

def load_memory(user_id: str) -> dict:
    path = os.path.join(MEMORY_DIR, f"{user_id}.json")
    if os.path.exists(path):
        return json.load(open(path))
    return {}

def save_memory(user_id: str, mem: dict):
    with open(os.path.join(MEMORY_DIR, f"{user_id}.json"), "w") as f:
        json.dump(mem, f)


# ── GEMINI CALL ───────────────────────────────────────────────────────────────

def call_gemini(prompt: str) -> str:
    resp = genai.chat.create(
        model=MODEL_ID,
        messages=[{"author": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


# ── WHATSAPP SENDER HELPERS ──────────────────────────────────────────────────

def send_whatsapp(payload: dict):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()

def send_text(to: str, body: str):
    send_whatsapp({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    })

def send_buttons(to: str, text: str, options: list[str]):
    buttons = []
    for opt in options:
        bid = opt.lower().replace(" ", "_")
        buttons.append({"type": "reply", "reply": {"id": bid, "title": opt}})
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


# ── MEDIA HANDLING ────────────────────────────────────────────────────────────

def download_media(media_id: str) -> str:
    meta = requests.get(
        f"https://graph.facebook.com/v17.0/{media_id}",
        params={"fields": "url,mime_type", "access_token": ACCESS_TOKEN}
    ).json()
    url = meta.get("url")
    if not url:
        raise RuntimeError("Could not fetch media URL")
    r = requests.get(url, params={"access_token": ACCESS_TOKEN})
    r.raise_for_status()
    ext = ".pdf" if meta.get("mime_type", "").startswith("application") else ".jpg"
    path = os.path.join(tempfile.gettempdir(), f"{media_id}{ext}")
    with open(path, "wb") as f:
        f.write(r.content)
    return path

def parse_pdf(path: str) -> str:
    text_pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_pages.append(t)
    return "\n".join(text_pages)

def parse_image(path: str) -> str:
    try:
        import pytesseract
        return pytesseract.image_to_string(Image.open(path))
    except Exception:
        return ""


# ── WEBHOOK ENDPOINT ─────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Verification handshake
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" \
        and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args["hub.challenge"], 200
        return "Forbidden", 403

    data = request.json or {}
    # guard non-message events
    if "contacts" not in data or "messages" not in data:
        logging.info("Webhook received non-message event.")
        return "OK", 200

    contact = data["contacts"][0]
    user_id = contact["wa_id"]
    mem = load_memory(user_id)

    msg = data["messages"][0]
    mtype = msg["type"]

    # ── STEP 1: ASK FOR FULL NAME ────────────────────────────────────────────────
    if "name" not in mem:
        if not mem.get("awaiting_name"):
            mem["awaiting_name"] = True
            save_memory(user_id, mem)
            send_text(user_id, "Hey! What's your **full name** so I can save you nicely in my contacts?")
            return "OK", 200

        if mem.get("awaiting_name") and mtype == "text":
            full = msg["text"]["body"].strip()
            mem["name"] = full
            mem.pop("awaiting_name")
            save_memory(user_id, mem)
            send_text(user_id, f"Great, {full}! You're all set. What topic shall we tackle first?")
            return "OK", 200

    # ── BUTTON RESPONSE HANDLING ─────────────────────────────────────────────────
    if mtype == "button":
        payload = msg["button"]["payload"]
        if payload == "understood":
            send_text(user_id, "Awesome! 👍 What would you like next?")
            return "OK", 200
        if payload == "explain_more":
            prompt = mem.get("last_academic_prompt", "")
            ans = call_gemini(prompt) if prompt else "Could you clarify what you'd like more on?"
            send_buttons(user_id, ans, ["Understood", "Explain More"])
            return "OK", 200

    # ── FILE OR IMAGE UPLOAD ─────────────────────────────────────────────────────
    if mtype in ("document", "image"):
        media = msg[mtype]
        mid = media["id"]
        try:
            loc = download_media(mid)
        except Exception:
            send_text(user_id, "Sorry, I couldn’t download that. Please try again.")
            return "OK", 200

        content = parse_pdf(loc) if mtype == "document" else parse_image(loc)
        if not content.strip():
            send_text(user_id, "I got the file but couldn’t extract text. Try a clearer PDF or image?")
            return "OK", 200

        mem["last_academic_prompt"] = content
        save_memory(user_id, mem)
        ans = call_gemini(content)
        send_buttons(user_id, ans, ["Understood", "Explain More"])
        return "OK", 200

    # ── BROAD TOPIC (ENGAGE NICELY) ──────────────────────────────────────────────
    if mtype == "text":
        text_in = msg["text"]["body"].strip()
        lower = text_in.lower()

        samples = {
            "english":    ["grammar rules", "essay structure", "vocabulary"],
            "chemistry":  ["periodic table", "stoichiometry", "chemical bonding"],
            "math":       ["algebra", "calculus", "geometry"],
            "history":    ["WWII", "Renaissance", "Ancient Empires"],
        }
        if lower in samples:
            opts = samples[lower]
            send_text(
                user_id,
                f"{mem.get('name','Hey')}—{text_in.capitalize()} is huge. What specifically?  For example: {', '.join(opts)}."
            )
            return "OK", 200

        # academic Q&A
        if len(text_in) > 30 or "solve" in lower:
            mem["last_academic_prompt"] = text_in
            save_memory(user_id, mem)
            ans = call_gemini(text_in)
            send_buttons(user_id, ans, ["Understood", "Explain More"])
            return "OK", 200

        # fallback small talk
        ans = call_gemini(text_in)
        send_text(user_id, ans)
        return "OK", 200

    # ── CATCH-ALL ────────────────────────────────────────────────────────────────
    send_text(user_id, "Hmm, I didn’t catch that—could you rephrase?")
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
