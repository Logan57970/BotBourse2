@echo off
REM Lancement local sous Windows (test)
if not exist venv (
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)
REM Renseigne tes clés ici :
REM set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
REM set ANTHROPIC_API_KEY=sk-ant-...
python bot.py --once
pause
