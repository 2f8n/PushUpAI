from flask import Flask, request
import requests
import os
import json
import google.generativeai as genai
from datetime import datetime

app = Flask(__name__)

# === ENV ===
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MEMORY_DIR = "memory"

# === Gemini Setup ===
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-pro")

# === Ensure memory dir exists ===
os.makedirs(MEMORY_DIR, exist_ok=True)

# === AI Prompt Boilerplate ===
def studymate_prompt(user_name, user_input):
    system_instruction = f"""
You are StudyMate AI, an empathetic and expert academic tutor helping students via WhatsApp.
Your tone is helpful, concise, and kind. Greet only once during onboarding.
Your task is to reply based on the student's input below.
If the input is just a greeting (like 'hi'), do not assume it's their name.
Avoid repeating greetings. If the user's name is not known, ask clearly:
\"Can I get your full name so I can personalize your experience?\"
Otherwise, be concise unless the message is study-related, then give a full educational response.
Always end with: \"Did that make sense to you? ✅ Yes or ❓ Not yet?\"
"""
    return f"""{system_instruction}

Student name: {user_name or '[Unknown]'}
Message: {user_input}
"""

# === User Memory ===
def get_user_memory(user_id):
    path = os.path.join(MEMORY_DIR, f"{user_id}.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_user_memory(user_id, memory):
    path = os.path.join(MEMORY_DIR, f"{user_id}.json")
    with open(path, "w") as f:
        json.dump(memory, f)

# === WhatsApp API ===
def send_whatsapp_message(phone_number, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=payload)
    print("WhatsApp API response:", response.status_code, response.text)

# === Webhook ===
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    if request.method == "POST":
        data = request.json
        try:
            entry = data["entry"][0]
            changes = entry["changes"][0]["value"]
            messages = changes.get("messages")
            if not messages:
                print("Webhook received non-message event.")
                return "OK", 200

            msg = messages[0]
            user_id = msg["from"]
            text = msg.get("text", {}).get("body", "").strip()
            if not text:
                return "OK", 200

            memory = get_user_memory(user_id)
            user_name = memory.get("name")

            if not user_name:
                if text.lower() in ["hi", "hello", "hey"]:
                    send_whatsapp_message(user_id, "👋 Hi there! What's your full name so I can personalize your StudyMate experience?")
                    return "OK", 200
                elif len(text.split()) >= 2:
                    memory["name"] = text.strip()
                    save_user_memory(user_id, memory)
                    send_whatsapp_message(user_id, f"Thanks, {text.strip().split()[0]}! You can now ask me anything to get started ✨")
                    return "OK", 200
                else:
                    send_whatsapp_message(user_id, "Please enter your full name (first and last) ✍️")
                    return "OK", 200

            # Send thinking...
            send_whatsapp_message(user_id, "🤖 Thinking...")

            prompt = studymate_prompt(user_name, text)
            response = model.generate_content(prompt)
            reply = response.text.strip()

            send_whatsapp_message(user_id, reply)
        except Exception as e:
            print("Error handling message:", e)
        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
