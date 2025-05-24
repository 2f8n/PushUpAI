from flask import Flask, request
import requests
import os
import google.generativeai as genai
from user_memory import get_user_profile, update_user_profile, add_message_to_history

app = Flask(__name__)

# Environment variables
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

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
            value = data["entry"][0]["changes"][0]["value"]
            if "messages" in value:
                msg = value["messages"][0]
                phone_number = msg["from"]
                user_text = msg["text"]["body"]
                add_message_to_history(phone_number, user_text)

                user_profile = get_user_profile(phone_number)
                user_name = user_profile.get("name")

                # Ask for name if not saved yet
                if not user_name:
                    if any(word in user_text.lower() for word in ["i am", "my name is", "this is", "call me"]):
                        name_guess = user_text.strip().split()[-1]
                        update_user_profile(phone_number, "name", name_guess)
                        reply = f"Thanks, {name_guess}! Now I can personalize your workouts and meals."
                    else:
                        reply = "Hey there! Before we get started, can I know your full name?"
                else:
                    reply = get_gemini_reply(user_text, user_name)

                send_whatsapp_message(phone_number, reply)
            else:
                print("Webhook received non-message event.")
        except Exception as e:
            print("Error handling message:", e)
        return "OK", 200

def get_gemini_reply(user_input, name="Student"):
    try:
        model = genai.GenerativeModel("gemini-1.5-pro-latest")

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
        response = model.generate_content(prompt)
        return response.text.strip()

    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I had trouble responding. Try again soon!"

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
