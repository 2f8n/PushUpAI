import os
import json
import tempfile
import logging

import requests
import pdfplumber
from flask import Flask, request, abort

# Optional OCR:
try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

import google.generativeai as genai

# ── CONFIG ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN",    "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN",    "your_whatsapp_token")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "your_phone_number_id")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "your_gemini_key")

genai.configure(api_key=GEMINI_API_KEY)
MODEL_ID = "models/chat-bison-001"

MEMORY_DIR = "memory"
os.makedirs(MEMORY_DIR, exist_ok=True)


# ── MEMORY HELPERS ─────────────────────────────────────────────────────────────

def load_memory(user_id: str) -> dict:
    path = os.path.join(MEMORY_DIR, f"{user_id}.json")
    if os.path.exists(path):
        return json.load(open(path))
    return {}

def save_memory(user_id: str, mem: dict):
    with open(os.path.join(MEMORY_DIR, f"{user_id}.json"), "w") as f:
        json.dump(mem, f)


# ── GEMINI CALL ─────────────────────────────────────────────────────────────────

def call_gemini(prompt: str) -> str:
    resp = genai.chat.create(
        model=MODEL_ID,
        messages=[{"author": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


# ── WHATSAPP SENDER ────────────────────────────────────────────────────────────

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
        buttons.append({
            "type": "reply",
            "reply": {"id": opt.lower().replace(" ", "_"), "title": opt}
        })
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


# ── MEDIA PARSING ──────────────────────────────────────────────────────────────

def download_media(media_id: str) -> str:
    # 1) fetch URL + mime_type
    meta = requests.get(
        f"https://graph.facebook.com/v17.0/{media_id}",
        params={"fields": "url,mime_type", "access_token": ACCESS_TOKEN}
    ).json()
    url = meta.get("url")
    if not url:
        raise RuntimeError("Could not fetch media URL")
    # 2) download with token
    r = requests.get(url, params={"access_token": ACCESS_TOKEN})
    r.raise_for_status()
    ext = ".pdf" if meta.get("mime_type","").startswith("application") else ".jpg"
    path = os.path.join(tempfile.gettempdir(), f"{media_id}{ext}")
    with open(path, "wb") as f:
        f.write(r.content)
    return path

def parse_pdf(path: str) -> str:
    text_pages = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            t = p.extract_text()
            if t:
                text_pages.append(t)
    return "\n".join(text_pages)

def parse_image(path: str) -> str:
    if not OCR_AVAILABLE:
        return ""
    try:
        return pytesseract.image_to_string(Image.open(path))
    except Exception:
        return ""


# ── WEBHOOK ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # Verification handshake
        if request.args.get("hub.mode") == "subscribe" \
        and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args["hub.challenge"], 200
        return "Forbidden", 403

    data = request.get_json(force=True)
    entry = data.get("entry", [])
    if not entry:
        logging.info("No entry")
        return "OK", 200

    changes = entry[0].get("changes", [])
    if not changes:
        logging.info("No changes")
        return "OK", 200

    value = changes[0].get("value", {})
    if "contacts" not in value or "messages" not in value:
        logging.info("Non-message event")
        return "OK", 200

    contacts = value["contacts"]
    messages = value["messages"]
    to_user = contacts[0]["wa_id"]
    mem = load_memory(to_user)
    msg = messages[0]
    mtype = msg["type"]

    # ─── STEP 1: NAME COLLECTION ────────────────────────────────────────────────
    if "name" not in mem:
        if not mem.get("awaiting_name"):
            mem["awaiting_name"] = True
            save_memory(to_user, mem)
            send_text(
                to_user,
                "👋 Hey! What's your full name so I can save you nicely in my contacts?"
            )
            return "OK", 200

        # capture their name
        if mem.get("awaiting_name") and mtype == "text":
            full = msg["text"]["body"].strip()
            mem["name"] = full
            mem.pop("awaiting_name", None)
            save_memory(to_user, mem)
            send_text(to_user, f"Thanks, {full}! What topic shall we dive into first?")
            return "OK", 200

    # ─── BUTTON REPLIES ─────────────────────────────────────────────────────────
    if mtype == "button":
        payload = msg["button"]["payload"]
        if payload == "understood":
            send_text(to_user, "👍 Great! What next?")
            return "OK", 200
        if payload == "explain_more":
            prompt = mem.get("last_academic_prompt", "")
            reply = call_gemini(prompt) if prompt else "Sure—what exactly?"
            send_buttons(to_user, reply, ["Understood", "Explain More"])
            return "OK", 200

    # ─── MEDIA (PDF/IMAGE) ──────────────────────────────────────────────────────
    if mtype in ("document", "image"):
        mid = msg[mtype]["id"]
        try:
            path = download_media(mid)
        except Exception:
            send_text(to_user, "😕 Couldn’t download that file. Try again?")
            return "OK", 200

        text = parse_pdf(path) if mtype=="document" else parse_image(path)
        if not text.strip():
            send_text(to_user, "I got it but couldn’t read any text.")
            return "OK", 200

        mem["last_academic_prompt"] = text
        save_memory(to_user, mem)
        ans = call_gemini(text)
        send_buttons(to_user, ans, ["Understood", "Explain More"])
        return "OK", 200

    # ─── TEXT: BROAD TOPIC EXAMPLES ─────────────────────────────────────────────
    if mtype == "text":
        text_in = msg["text"]["body"].strip()
        lower = text_in.lower()

        samples = {
            "english":   ["grammar rules", "essay structure", "vocabulary"],
            "chemistry": ["periodic table", "stoichiometry", "bonding"],
            "math":      ["algebra", "calculus", "geometry"],
            "history":   ["WWII", "Renaissance", "Ancient Rome"],
        }
        if lower in samples:
            opts = samples[lower]
            name = mem.get("name", "Hey")
            send_text(
                to_user,
                f"{name}, {text_in.capitalize()} is huge—what specifically? "
                f"For example: {', '.join(opts)}."
            )
            return "OK", 200

        # long/“solve” questions → academic flow
        if len(text_in) > 30 or "solve" in lower:
            mem["last_academic_prompt"] = text_in
            save_memory(to_user, mem)
            ans = call_gemini(text_in)
            send_buttons(to_user, ans, ["Understood", "Explain More"])
            return "OK", 200

        # fallback small talk
        ans = call_gemini(text_in)
        send_text(to_user, ans)
        return "OK", 200

    # ─── CATCH-ALL ──────────────────────────────────────────────────────────────
    send_text(to_user, "Sorry—I didn’t catch that. Could you rephrase?")
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
