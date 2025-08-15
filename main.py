import csv
import os
import re
from flask import Flask, request, Response
from datetime import datetime
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import whisper
import requests
import subprocess
import threading
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
app = Flask(__name__)

# Twilio credentials
account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
twilio_number = os.getenv('TWILIO_PHONE_NUMBER')

client = Client(account_sid, auth_token)

# Load Whisper model
model = whisper.load_model("base")

# CSV
CSV_FILE = "borrowers_data.csv"
conversation_state = {}

# -------------------
# Helper functions
# -------------------
def normalize_response(text):
    """Clean transcription for easier matching."""
    text = text.lower().strip()
    text = re.sub(r'[^a-z\s]', '', text)  # remove punctuation/numbers
    return text

def is_yes(text):
    yes_words = {"yes", "yeah", "yep", "yup", "ya", "sure", "absolutely", "correct", "right", "affirmative"}
    return any(word in text.split() for word in yes_words)

def is_no(text):
    no_words = {"no", "nope", "nah", "not", "never", "negative"}
    return any(word in text.split() for word in no_words)

def load_borrower(phone_number):
    with open(CSV_FILE, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            if row['phone_number'] == phone_number:
                return row
    return None

def update_csv(phone_number, updates):
    rows = []
    with open(CSV_FILE, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        fieldnames = reader.fieldnames + list(updates.keys())
        for row in reader:
            if row['phone_number'] == phone_number:
                row.update(updates)
            rows.append(row)
    with open(CSV_FILE, mode='w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=list(set(fieldnames)))
        writer.writeheader()
        writer.writerows(rows)

# -------------------
# Voice Call Flow
# -------------------
@app.route("/voice", methods=['GET', 'POST'])
def voice():
    call_sid = request.values.get("CallSid", "")
    to_number = request.values.get("To", "")
    borrower = load_borrower(to_number)

    if not borrower:
        vr = VoiceResponse()
        vr.say("We couldn't find your details. Goodbye.", voice="Polly.Aditi")
        return Response(str(vr), mimetype="text/xml")

    conversation_state[call_sid] = {
        "step": 0,
        "answers": {},
        "borrower": borrower,
        "chat_history": []
    }

    intro = f"Namaste! This is a call from Bargaj Finance. I’m here to talk about your microfinance loan. Am I speaking to {borrower['name']}?"

    vr = VoiceResponse()
    vr.say(intro, voice="Polly.Aditi")
    vr.record(max_length=5, action="/process", recording_status_callback="/save", play_beep=True)
    return Response(str(vr), mimetype="text/xml")

@app.route("/process", methods=['POST'])
def process():
    call_sid = request.form.get('CallSid', '')
    borrower = conversation_state[call_sid]["borrower"]
    recording_url = request.form['RecordingUrl'] + '.mp3'

    # Download and convert audio
    r = requests.get(recording_url, auth=(account_sid, auth_token))
    with open("input.mp3", "wb") as f:
        f.write(r.content)
    subprocess.run(["ffmpeg", "-y", "-i", "input.mp3", "-ar", "16000", "-ac", "1", "input.wav"])

    # Transcribe
    result = model.transcribe("input.wav")
    user_response = normalize_response(result["text"])

    step = conversation_state[call_sid]["step"]
    state = conversation_state[call_sid]

    vr = VoiceResponse()

    # Step 0: Confirm identity
    if step == 0:
        if is_yes(user_response):
            vr.say(
                f"You have an active loan of ₹{borrower['loan_amount']} with a pending amount of ₹{borrower['pending_amount']}. "
                f"Last due date was {borrower['last_due_date']}. Did you take this loan?",
                voice="Polly.Aditi"
            )
            state["step"] += 1
        elif is_no(user_response):
            vr.say("Alright, we’ll reach out another time. Goodbye.", voice="Polly.Aditi")
            update_csv(borrower['phone_number'], {"responded": "No"})
            return Response(str(vr), mimetype="text/xml")
        else:
            vr.say("Sorry, I didn’t get that. Could you please say yes or no?", voice="Polly.Aditi")
        vr.record(max_length=6, action="/process", recording_status_callback="/save", play_beep=True)
        return Response(str(vr), mimetype="text/xml")

    # Step 1: Ask about taking loan
    elif step == 1:
        if is_yes(user_response):
            state["answers"]["took_loan"] = "Yes"
            gather = vr.gather(
                input="dtmf",
                num_digits=1,
                action="/handle_reason",
                timeout=5
            )
            gather.say(
                "Please select the reason why the last installment was not paid. "
                "Press 1 if you didn’t know the EMI was due. "
                "Press 2 if the collector didn’t come. "
                "Press 3 if you don’t have money right now. "
                "Press 4 if you forgot. "
                "Press 5 if you will pay soon.",
                voice="Polly.Aditi"
            )
            state["step"] = 2  # Waiting for DTMF
            return Response(str(vr), mimetype="text/xml")
        elif is_no(user_response):
            state["answers"]["took_loan"] = "No"
            state["answers"]["reason"] = "Did not take loan"
            vr.say(
                "Did someone else use your documents, or could this be a mistake? "
                "Please say yes if you think it’s a mistake, or no if not.",
                voice="Polly.Aditi"
            )
            state["step"] = "confirm_mistake"
            vr.record(max_length=6, action="/process", recording_status_callback="/save", play_beep=True)
            return Response(str(vr), mimetype="text/xml")
        else:
            vr.say("Sorry, I didn’t get that. Could you please say yes or no?", voice="Polly.Aditi")
            vr.record(max_length=6, action="/process", recording_status_callback="/save", play_beep=True)
            return Response(str(vr), mimetype="text/xml")

    # Confirm mistake step
    elif step == "confirm_mistake":
        if is_yes(user_response):
            vr.say("Our support team will investigate the issue. Goodbye.", voice="Polly.Aditi")
            update_csv(
                borrower['phone_number'],
                {
                    "took_loan": "No",
                    "reason": "Possible identity misuse",
                    "responded": "Yes"
                }
            )
        elif is_no(user_response):
            vr.say("Alright, we have recorded your response. Goodbye.", voice="Polly.Aditi")
            update_csv(
                borrower['phone_number'],
                {
                    "took_loan": "No",
                    "reason": "Did not take loan",
                    "responded": "Yes"
                }
            )
        else:
            vr.say("Sorry, I didn’t get that. Please say yes or no.", voice="Polly.Aditi")
            vr.record(max_length=6, action="/process", recording_status_callback="/save", play_beep=True)
            return Response(str(vr), mimetype="text/xml")
        return Response(str(vr), mimetype="text/xml")

    # Step 3: After reason given
    elif step == 3:
        if "remind" in user_response or is_yes(user_response):
            state["answers"]["wants_reminder"] = "Yes"
        elif "settlement" in user_response or "lower amount" in user_response:
            state["answers"]["settlement_requested"] = "Yes"
        else:
            state["answers"]["wants_reminder"] = "No"

        vr.say("Thank you for your time. Staying on track helps your credit score. Goodbye!", voice="Polly.Aditi")
        update_csv(borrower['phone_number'], {**state["answers"], "responded": "Yes"})
        return Response(str(vr), mimetype="text/xml")

    vr.record(max_length=6, action="/process", recording_status_callback="/save", play_beep=True)
    return Response(str(vr), mimetype="text/xml")

@app.route("/handle_reason", methods=['POST'])
def handle_reason():
    call_sid = request.form.get('CallSid', '')
    digits = request.form.get('Digits', '')
    state = conversation_state.get(call_sid)
    vr = VoiceResponse()

    reasons = {
        "1": "Didn’t know EMI was due",
        "2": "Collector didn’t come",
        "3": "No money",
        "4": "Forgot",
        "5": "Will pay soon"
    }

    selected_reason = reasons.get(digits, "Unknown")
    state["answers"]["reason"] = selected_reason
    state["step"] = 3

    if digits == "3":  # Offer settlement
        vr.say("We can help with a settlement option where you pay a lower amount. Would you like that?", voice="Polly.Aditi")
    else:
        vr.say("Thank you. Should I set a reminder for your next payment?", voice="Polly.Aditi")

    vr.record(max_length=6, action="/process", recording_status_callback="/save", play_beep=True)
    return Response(str(vr), mimetype="text/xml")

@app.route("/save", methods=["POST"])
def save_recording():
    return Response("Saved", status=200)

# Call initiation
def make_initial_call(phone_number):
    ngrok_url = os.getenv("NGROK_URL")
    call = client.calls.create(
        to=phone_number,
        from_=twilio_number,
        url=f"{ngrok_url}/voice"
    )
    print(f"📞 Call placed to {phone_number} — SID: {call.sid}")

if __name__ == "__main__":
    with open("borrowers_data.csv", newline='') as file:
        reader = csv.DictReader(file)
        for row in reader:
            threading.Thread(target=make_initial_call, args=(row["phone_number"],)).start()
    app.run(debug=True, port=5500)
