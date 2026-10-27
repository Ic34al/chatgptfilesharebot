# Fast Telegram File Share Bot

A lightweight 24/7 Telegram file-sharing bot. It stores Telegram `file_id` values and metadata in MongoDB instead of downloading files to the server.

## Environment variables

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `MONGODB_URI`
- `DATABASE_NAME` (optional)
- `OWNER_ID` (optional)

## Local run

```bash
pip install -r requirements.txt
python main.py
```

## Heroku

This project uses a worker dyno:

```text
worker: python main.py
```

Add the environment variables in Heroku Config Vars, then scale the worker to 1.

Never commit Telegram or MongoDB credentials to GitHub.
