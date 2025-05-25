import os
import json
import tempfile
import requests
import pdfplumber
from PIL import Image
import google.generativeai as genai
from flask import Flask, request

app = Flask(__name__)

# ── CONFIG ─────────────────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN",    "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN",    "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY",  "")

genai.configure(api_key=GEMINI_API_KEY)
MODEL_ID = "models/chat-bison-001"    # or your preferred Gemini model

MEMORY_DIR = "memory"
os.makedirs(MEMORY_DIR, exist_ok=True)


# ── HELPERS ────────────────────────────────────────────────────────────────────

def load_memory(user_id):
    path = os.path.join(MEMORY_DIR, f"{user_id}.json")
    if os.path.isfile(path):
        return json.load(open(path))
    return {}

def save_memory(user_id, mem):
    with open(os.path.join(MEMORY_DIR, f"{user_id}.json"), "w") as f:
        json.dump(mem, f)

def call_gemini(prompt: str) -> str:
    resp = genai.chat.create(
        model=MODEL_ID,
        messages=[{"author": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()

def send_whatsapp(payload: dict):
    url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()

def send_text(to: str, body: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    send_whatsapp(payload)

def send_buttons(to: str, text: str, options: list[str]):
    buttons = [{
        "type": "reply",
        "reply": {"id": opt.lower(), "title": opt}
    } for opt in options]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": buttons}
        }
    }
    send_whatsapp(payload)

def download_media(media_id: str) -> str:
    # 1) fetch media URL
    meta = requests.get(
        f"https://graph.facebook.com/v17.0/{media_id}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
    ).json()
    url = meta.get("url")
    if not url:
        raise RuntimeError("Could not fetch media URL")
    # 2) download content
    r = requests.get(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
    r.raise_for_status()
    ext = ".pdf" if meta.get("mime_type","").startswith("application") else ".jpg"
    path = os.path.join(tempfile.gettempdir(), f"{media_id}{ext}")
    with open(path, "wb") as f:
        f.write(r.content)
    return path

def parse_pdf(path: str) -> str:
    text = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            t = p.extract_text()
            if t:
                text.append(t)
    return "\n".join(text)

def parse_image(path: str) -> str:
    try:
        return pytesseract.image_to_string(Image.open(path))
    except Exception:
        return ""


# ── WEBHOOK ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.mode") == "subscribe" \
        and request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args["hub.challenge"], 200
        return "Verification failed", 403

    data = request.json
    contact = data["contacts"][0]
    user_id = contact["wa_id"]
    mem = load_memory(user_id)

    msg = data["messages"][0]
    mtype = msg["type"]

    # ── STEP 1: NAME MEMORY ─────────────────────────────────────────────────────

    if "name" not in mem:
        # ask once
        if not mem.get("awaiting_name"):
            mem["awaiting_name"] = True
            save_memory(user_id, mem)
            send_buttons(
                user_id,
                "Hey! Before we start, could I have your full name so I can save you in my contacts?",
                ["Sure!"]
            )
            return "OK", 200

        # save name on next text
        if mem.get("awaiting_name") and mtype == "text":
            full_name = msg["text"]["body"].strip()
            mem["name"] = full_name
            mem.pop("awaiting_name", None)
            save_memory(user_id, mem)
            send_text(user_id, f"Awesome, {full_name}! You're all set in my contacts. What shall we dive into today?")
            return "OK", 200

    # ── STEP 2: BUTTON HANDLES ──────────────────────────────────────────────────

    if mtype == "button":
        payload = msg["button"]["payload"]
        if payload == "understood":
            send_text(user_id, "Great! 👍 What's next?")
            return "OK", 200
        if payload == "explain more":
            prompt = mem.get("last_academic_prompt", "Can you clarify?")
            ans = call_gemini(prompt)
            send_buttons(user_id, ans, ["Understood", "Explain More"])
            return "OK", 200

    # ── STEP 2: FILE / IMAGE HANDLING ──────────────────────────────────────────

    if mtype in ("document", "image"):
        media = msg[mtype]
        media_id = media["id"]
        try:
            path = download_media(media_id)
        except Exception:
            send_text(user_id, "Oops, I couldn’t grab that file—please try again.")
            return "OK", 200

        content = parse_pdf(path) if mtype == "document" else parse_image(path)
        if not content.strip():
            send_text(user_id, "I read it, but couldn’t extract any text. Try a clearer PDF or image?")
            return "OK", 200

        mem["last_academic_prompt"] = content
        save_memory(user_id, mem)
        ans = call_gemini(content)
        send_buttons(user_id, ans, ["Understood", "Explain More"])
        return "OK", 200

    # ── BROAD-TOPIC PROMPTS ─────────────────────────────────────────────────────

    if mtype == "text":
        text_in = msg["text"]["body"].strip()
        lower = text_in.lower()

        # narrow down broad subjects
        samples = {
            "english":    ["grammar rules", "essay structure", "vocabulary", "comprehension"],
            "chemistry":  ["periodic table", "stoichiometry", "bonding", "thermodynamics"],
            "math":       ["algebra", "calculus", "geometry", "statistics"],
            "history":    ["WWII", "renaissance", "cold war", "ancient empires"],
        }
        if lower in samples:
            opts = samples[lower][:3]
            send_text(
                user_id,
                f"Great, {text_in.capitalize()} is vast—what specifically?  
For example: {', '.join(opts)}."
            )
            return "OK", 200

        # academic Q&A triggers
        if len(text_in.split()) > 3 or "solve" in lower:
            mem["last_academic_prompt"] = text_in
            save_memory(user_id, mem)
            ans = call_gemini(text_in)
            send_buttons(user_id, ans, ["Understood", "Explain More"])
            return "OK", 200

        # casual fallback
        ans = call_gemini(text_in)
        send_text(user_id, ans)
        return "OK", 200

    # unknown type
    send_text(user_id, "Sorry, I didn't get that. Could you rephrase?")
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
