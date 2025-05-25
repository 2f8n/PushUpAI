from flask import Flask, request
import os
import json
import re
import requests
import google.generativeai as genai

app = Flask(__name__)

# ─── Environment ────────────────────────────────────────────────────────────────
VERIFY_TOKEN   = os.environ.get("VERIFY_TOKEN",   "pushupai_verify_token")
ACCESS_TOKEN   = os.environ.get("ACCESS_TOKEN",   "")
PHONE_NUMBER_ID= os.environ.get("PHONE_NUMBER_ID","")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ─── Gemini Init ───────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-1.5-pro-002"
model = genai.GenerativeModel(MODEL_NAME)

# ─── Memory Helpers ────────────────────────────────────────────────────────────
MEMORY_DIR = "memory"
def memory_path(phone: str) -> str:
    return os.path.join(MEMORY_DIR, f"{phone}.json")

def load_memory(phone: str) -> dict:
    try:
        with open(memory_path(phone), "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_memory(phone: str, data: dict):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(memory_path(phone), "w") as f:
        json.dump(data, f)
    print(f"[✔] Memory saved: {memory_path(phone)}")

# ─── WhatsApp Senders ──────────────────────────────────────────────────────────
def send_whatsapp_message(phone: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    resp = requests.post(url, headers=headers, json=payload)
    print("WhatsApp API response:", resp.status_code, resp.text)

def send_interactive_buttons(phone: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense to you?"},
            "action": {
                "buttons": [
                    {"type":"reply", "reply":{"id":"understood","title":"Understood"}},
                    {"type":"reply", "reply":{"id":"explain_more","title":"Explain more"}}
                ]
            }
        }
    }
    resp = requests.post(url, headers=headers, json=payload)
    print("Interactive buttons response:", resp.status_code, resp.text)

# ─── Text Cleaners ─────────────────────────────────────────────────────────────
def strip_greeting(text: str) -> str:
    # remove common greetings at start
    return re.sub(r'^(hi|hello|hey)[^\n]*\n?', '', text, flags=re.IGNORECASE).strip()

# ─── Gemini Reply ──────────────────────────────────────────────────────────────
def get_gemini_reply(prompt: str) -> str:
    try:
        resp = model.generate_content(prompt)
        return strip_greeting(resp.text.strip())
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I had trouble responding. Try again soon!"

# ─── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if (request.args.get("hub.mode") == "subscribe" and
            request.args.get("hub.verify_token") == VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            print("Webhook non-message event")
            return "OK", 200

        msg = entry["messages"][0]
        phone = msg["from"]
        memory = load_memory(phone)

        # ─── Onboarding: ask for full name if missing ──────────────────────
        if "name" not in memory:
            # if user is replying with a name (two words)
            text = msg.get("text", {}).get("body", "").strip()
            if len(text.split()) >= 2:
                memory["name"] = text
                save_memory(phone, memory)
                send_whatsapp_message(
                    phone,
                    f"Nice to meet you, {text}! 🎓 What would you like to study today?"
                )
            else:
                send_whatsapp_message(
                    phone,
                    "Hey! Could you share your full name (first and last) so I know what to save in my contacts?"
                )
            return "OK", 200

        # ─── Button Replies: Understood / Explain more ──────────────────────
        if msg.get("type") == "button":
            payload = msg["button"]["payload"]
            if payload == "understood":
                send_whatsapp_message(
                    phone,
                    "Great! 🎉 What's next on your study list?"
                )
            elif payload == "explain_more":
                last_prompt = memory.get("last_prompt")
                if last_prompt:
                    detail = get_gemini_reply(last_prompt + "\n\nPlease explain in more detail.")
                    send_whatsapp_message(phone, detail)
                    send_interactive_buttons(phone)
                else:
                    send_whatsapp_message(phone, "Sorry, I don't have anything to expand on yet.")
            return "OK", 200

        # ─── Normal Study Query ────────────────────────────────────────────
        user_text = msg.get("text", {}).get("body", "").strip()
        # build prompt with context
        name = memory["name"]
        prompt = (
            f"You are StudyMate AI, founded by ByteWave Media, "
            f"helping {name}. Question:\n\n{user_text}\n\n"
            "Give a clear, step-by-step explanation. "
            "Use an encouraging, conversational tone."
        )
        memory["last_prompt"] = prompt
        save_memory(phone, memory)

        send_whatsapp_message(phone, "🤖 Thinking...")
        answer = get_gemini_reply(prompt)
        send_whatsapp_message(phone, answer)
        send_interactive_buttons(phone)

    except Exception as e:
        print("Error handling webhook:", e)

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
