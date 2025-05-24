from flask import Flask, request
import requests
import os
import json
import google.generativeai as genai
from pathlib import Path

# Configure Flask app
app = Flask(__name__)

# Load environment variables
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "studymate_verify")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Ensure memory directory exists
Path("memory").mkdir(parents=True, exist_ok=True)

# Helper: Load user memory
def load_memory(user_id):
    memory_file = Path(f"memory/{user_id}.json")
    if memory_file.exists():
        with open(memory_file) as f:
            return json.load(f)
    else:
        return {}

# Helper: Save user memory
def save_memory(user_id, memory):
    with open(f"memory/{user_id}.json", "w") as f:
        json.dump(memory, f)

# Gemini reply handler
def get_gemini_reply(prompt):
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I had trouble responding. Try again soon."

# WhatsApp reply sender
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

# Webhook endpoint
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
            msg = data["entry"][0]["changes"][0]["value"].get("messages")
            if not msg:
                print("Webhook received non-message event.")
                return "OK", 200

            msg = msg[0]
            user_id = msg["from"]
            user_text = msg.get("text", {}).get("body", "").strip()

            memory = load_memory(user_id)

            # Ask for name if not set
            if "full_name" not in memory:
                if "waiting_for_name" in memory:
                    name = user_text
                    if len(name.split()) >= 2:
                        memory["full_name"] = name
                        del memory["waiting_for_name"]
                        save_memory(user_id, memory)
                        send_whatsapp_message(user_id, f"Nice to meet you, {name.split()[0]}! I'm StudyMate AI. How can I help with your studying today?")
                        return "OK", 200
                    else:
                        send_whatsapp_message(user_id, "Please enter your *full name* (first and last).")
                        return "OK", 200
                else:
                    memory["waiting_for_name"] = True
                    save_memory(user_id, memory)
                    send_whatsapp_message(user_id, "Hi! Before we begin, what's your *full name*?")
                    return "OK", 200

            prompt = f"You are StudyMate AI, a friendly WhatsApp tutor. {user_text}"
            reply = get_gemini_reply(prompt)
            send_whatsapp_message(user_id, reply)

        except Exception as e:
            print("Error handling message:", e)

        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
