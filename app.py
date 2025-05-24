from flask import Flask, request
import requests
import os
import json
import google.generativeai as genai

app = Flask(__name__)

# 🔐 Environment variables
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 🧠 Gemini configuration
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-pro")

# 📁 Memory handling
def memory_path(phone):
    return f"memory/{phone}.json"

def load_user_memory(phone):
    try:
        with open(memory_path(phone), "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_user_memory(phone, data):
    os.makedirs("memory", exist_ok=True)
    path = memory_path(phone)
    with open(path, "w") as f:
        json.dump(data, f)
    print(f"[✔] Memory saved for {phone} → {path}")

# 📩 WhatsApp reply function
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

# 🤖 Gemini response generation
def get_gemini_reply(prompt):
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I had trouble responding. Try again soon!"

# 📬 Webhook logic
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
            message_entry = data["entry"][0]["changes"][0]["value"]
            if "messages" not in message_entry:
                print("Webhook received non-message event.")
                return "OK", 200

            msg = message_entry["messages"][0]
            phone = msg["from"]
            user_text = msg["text"]["body"].strip()

            memory = load_user_memory(phone)

            # Ask for name if not stored
            if "name" not in memory:
                memory["name"] = user_text
                save_user_memory(phone, memory)
                send_whatsapp_message(phone, f"Nice to meet you, {user_text}! 🎓 How can I help you study today?")
                return "OK", 200

            # Use stored name and respond via Gemini
            user_name = memory["name"]
            prompt = f"You're StudyMate AI helping {user_name}. Question: {user_text}"
            reply = get_gemini_reply(prompt)
            send_whatsapp_message(phone, reply)

        except Exception as e:
            print("Error handling message:", e)
        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
