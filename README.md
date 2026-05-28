# Telegram Anime Bot

Anime bot for Telegram with browser, watch history, AI chat.

## Features
- Anime browser: genres, seasons, episodes, movies
- Watch history (last 300 episodes)
- Search anime by name
- ChatGPT (gpt-4o-mini)
- Streaming links: AniLibria, AniDUB, Russian dub

## Deploy on Render
1. Create a new **Background Worker** on render.com
2. Connect this repository
3. Add environment variables:
   - `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather
   - `OPENAI_API_KEY` — your OpenAI key
4. Build command: `pip install -r requirements.txt`
5. Start command: `python bot/main.py`
