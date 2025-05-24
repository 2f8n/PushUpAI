from flask import Flask, request
import requests
import os
import json
import google.generativeai as genai

app = Flask(__name__)

# Environment variables
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "studymate_verify")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Ensure memory directory exists
if not os.path.exists("memory"):
    os.makedirs("memory")

def load_user_memory(phone):
    path = f"memory/{phone}.json"
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_user_memory(phone, data):
    path = f"memory/{phone}.json"
    with open(path, "w") as f:
        json.dump(data, f)

def is_name_question(text):
    text = text.lower()
    # Recognize various phrasings asking for user's name
    return any(q in text for q in [
        "what's my name",
        "whats my name",
        "what is my name",
        "who am i"
    ])

def get_gemini_reply(prompt, context=""):
    try:
        full_prompt = (context + "\n" if context else "") + prompt
        response = model.generate_content(full_prompt)
        return response.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I had trouble thinking that through. Try again soon!"

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
    response = requests.post(url, headers=headers, json=payload)
    print("WhatsApp API response:", response.status_code, response.text)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge")
        return "Verification failed", 403

    if request.method == "POST":
        data = request.json
        try:
            msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
            phone = msg["from"]
            text = msg.get("text", {}).get("body", "").strip()
            memory = load_user_memory(phone)
            name = memory.get("name")

            if not name:
                if len(text.split()) >= 2 and text.replace(" ", "").isalpha():
                    memory["name"] = text
                    save_user_memory(phone, memory)
                    send_whatsapp_message(phone, f"Thanks! I’ll remember your name as {text} ✅")
                else:
                    send_whatsapp_message(phone, "Hey 👋 What’s your full name so I can remember you?")
                return "OK", 200

            # Special: user asked who they are
            if is_name_question(text):
                send_whatsapp_message(phone, f"You are {name}! 🧠 I’ve got you saved.")
                return "OK", 200

            # Send placeholder thinking message
            thinking_msg = send_temp_thinking(phone)

            # Generate reply using Gemini
            context = f"User's name is {name}. Respond casually unless it's a study-related topic."
            reply = get_gemini_reply(text, context=context)
            
            # Delete thinking and send reply
            delete_message(thinking_msg)
            send_whatsapp_message(phone, reply)

        except Exception as e:
            print("Error handling message:", e)
        return "OK", 200

def send_temp_thinking(phone):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": "🤖 Thinking..."}
    }
    response = requests.post(url, headers=headers, json=payload).json()
    return response.get("messages", [{}])[0].get("id")

def delete_message(msg_id):
    if not msg_id:
        return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    requests.delete(f"{url}/{msg_id}", headers=headers)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
