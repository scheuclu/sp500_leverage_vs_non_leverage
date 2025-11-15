import requests

BOT_TOKEN = "8575714249:AAHlqYJiPqSIoODqCmku5f-PkRNg8hD70TY"
CHAT_ID = "5126573205"


def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    r = requests.post(url, json=payload)
    print(r.json())
