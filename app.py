from flask import Flask, request
import os
import json
import requests
import google.generativeai as genai

app = Flask(__name__)

# Load env vars
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-latest")

# --- User Memory Utilities ---

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
    with open(memory_path(phone), "w") as f:
        json.dump(data, f)

# --- AI Reply with Tutor Prompt ---

def get_gemini_reply(user_input, name="Student"):
    prompt = f"""
You are StudyMate AI — a friendly academic tutor on WhatsApp.

🎓 Your job is to:
- Explain clearly
- Break down difficult ideas into steps
- Stay positive and supportive
- End every response with a check: "Did that make sense? ✅ Yes / ❓ Not yet?"

👤 Student Name: {name}
📩 Question: "{user_input}"

Your response format should be:
- Clear step-by-step explanation
- Example if helpful
- End with: “Did that make sense? ✅ Yes / ❓ Not yet?”
"""
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I had trouble responding. Try again soon!"

# --- WhatsApp Message Sender ---

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

# --- Webhook Endpoint ---

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if (request.args.get("hub.mode") == "subscribe" and
            request.args.get("hub.verify_token") == VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    if request.method == "POST":
        data = request.json
        try:
            msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
            phone_number = msg["from"]
            user_text = msg["text"]["body"].strip()

            # Load or create user memory
            user_memory = load_user_memory(phone_number)

            # Handle name onboarding
            if "name" not in user_memory:
                send_whatsapp_message(phone_number, "Hey! What's your full name?")
                user_memory["expecting_name"] = True
                save_user_memory(phone_number, user_memory)
                return "OK", 200

            if user_memory.get("expecting_name"):
                full_name = user_text
                user_memory["name"] = full_name
                user_memory["expecting_name"] = False
                save_user_memory(phone_number, user_memory)
                send_whatsapp_message(phone_number, f"Nice to meet you, {full_name}! 👋 What would you like help with today?")
                return "OK", 200

            # Normal AI reply
            reply = get_gemini_reply(user_text, name=user_memory.get("name", "Student"))
            send_whatsapp_message(phone_number, reply)

        except Exception as e:
            print("Error handling message:", e)

        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
