# NFC — Telegram Group Management Bot

Serious, configurable group management bot. No games, economy, reaction commands, or verification gate.

## Core
- Moderation: warn, mute/unmute, ban/unban, kick, delete, purge, pin/unpin
- Admin management: promote/demote (subject to Telegram permissions)
- AutoMod: anti-spam, anti-flood, anti-link, anti-forward, anti-bot, anti-mention, repeat guard
- Security controls: anti-channel, anti-service
- Content locks: links, photos, videos, GIFs, stickers, documents, audio, voice, polls, forwards, commands
- Filters: custom trigger/action/response rules
- Approved members: per-group approval/whitelist that bypasses content protection
- Member events: welcome/goodbye (OFF by default)
- Group rules, admin list, statistics, notes, reports, AFK
- Admin audit logs and analytics are optional and OFF by default
- Intro GIF via `INTRO_GIF`
- `/help` in groups opens a private help menu; contact-admin button is configurable

## Environment
`BOT_TOKEN` is required. `ADMIN_ID`, `ADMIN_USERNAME`, and `INTRO_GIF` are optional.

## Render
Build: `pip install -r requirements.txt`
Start: `python NFC_RENDER.py`

The bot uses SQLite for local persistence. On hosting where the filesystem is ephemeral, use persistent storage/database if long-term data retention is required.
