Install: pip install curl_cffi requests
Create a Telegram bot and copy the token.
Set the variables in PowerShell:

TELEGRAM_TOKEN="your_token"
TELEGRAM_CHAT_ID="your_chat_id"

Run:

python main.py          # runs continuously, checks every 5-10 min(to stop press ctrl+c)
python main.py --once   # single check, then exit
