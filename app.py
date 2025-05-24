from flask import Flask, request
import os
import json
import requests
import google.generativeai as genai

app = Flask(__name__)

# Environment variables
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Configure Gemini API
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# Memory handling
def memory_path(phone): return f"memory/{phone}.json"

def load_user_memory(phone):
    try:
        with open(memory_path(phone), "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_user_memory(phone, data):
    os.makedirs("memory", exist_ok=True)
    with open(memory_path(phone), "w") as f:
        json.dump(data, f)

# Gemini prompt logic
def get_gemini_reply(user_input, name="Student"):
    prompt = f"""
You are StudyMate AI — a smart, friendly academic tutor on WhatsApp.

👤 Student Name: {name}
❓ Question: "{user_input}"

Always reply with:
1. A clear explanation
2. Encouraging tone
3. Step-by-step if complex
4. End with: “Did that make sense? ✅ Yes / ❓ Not yet?”
"""
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I had trouble responding. Try again soon!"

# Send WhatsApp message
def send_whatsapp_message(phone, text):
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
    res = requests.post(url, headers=headers, json=payload)
    print("WhatsApp API response:", res.status_code, res.text)

# Webhook handler
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
            changes = data["entry"][0]["changes"][0]["value"]

            # Skip if not a message event
            if "messages" not in changes:
                print("Webhook received non-message event.")
                return "OK", 200

            msg = changes["messages"][0]
            phone_number = msg["from"]
            user_text = msg["text"]["body"].strip()

            memory = load_user_memory(phone_number)

            # Ask for name if missing
            if "name" not in memory:
                send_whatsapp_message(phone_number, "Hey! What's your full name?")
                memory["expecting_name"] = True
                save_user_memory(phone_number, memory)
                return "OK", 200

            if memory.get("expecting_name"):
                memory["name"] = user_text
                memory["expecting_name"] = False
                save_user_memory(phone_number, memory)
                send_whatsapp_message(phone_number, f"Nice to meet you, {user_text}! 👋 What would you like help with today?")
                return "OK", 200

            # Normal query
            reply = get_gemini_reply(user_text, name=memory["name"])
            send_whatsapp_message(phone_number, reply)

        except Exception as e:
            print("Error handling message:", e)

        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
