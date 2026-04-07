# DayZ Basic Discord Bot

A basic Discord bot for DayZ server communities. Shows live player counts, server status, restart countdowns, and population trends — via both slash commands and legacy prefix commands.

## Features

- Live player count in bot presence and optional voice channel name
- `/players` — list online players with session time
- `/server` — server info embed (map, players, ping, restart time)
- `/nextrestart` — next scheduled restart ETA
- `/population` — peak/average/low from recent snapshots
- `!uptime` — how long the DayZ process has been running (Linux only)
- Configurable restart warning messages (e.g. 60/30/15/10/5/3/1 min before restart)
- Population snapshots written to JSONL for trend data
- All config via environment variables — no secrets in code

## Requirements

- Python 3.11+ (`python3` on Linux/Mac, `py` on Windows)
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- DayZ server with Steam query port accessible
- Linux host for `!uptime` (uses `pgrep`/`ps`); all other features are cross-platform

## Discord bot setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**
2. **Bot** → **Add Bot** → copy your token
3. Still on the Bot page, scroll to **Privileged Gateway Intents** → enable **Message Content Intent**
4. **OAuth2 → URL Generator** → scopes: `bot`, `applications.commands` → permissions: `Send Messages`, `Embed Links`, `Manage Channels`, `Read Message History`
5. Open the generated URL and invite the bot to your server

## Setup

```bash
git clone https://github.com/rodebrekht/dayz-discord-bot.git
cd dayz-discord-bot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your values (see Configuration below), then:

```bash
python3 bot.py                  # Windows: py bot.py
```

## Configuration

All options are set in `.env`. See `.env.example` for the full reference with descriptions.

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Your bot token |
| `DISCORD_GUILD_ID` | — | Guild ID for instant slash command sync (omit for global sync, which can take up to an hour). Requires Developer Mode in Discord to copy. |
| `DAYZ_QUERY_HOST` | `127.0.0.1` | Steam query host |
| `DAYZ_QUERY_PORT` | `27016` | Steam query port |
| `STATUS_CHANNEL_ID` | — | Voice channel to rename with player count |
| `ALERTS_CHANNEL_ID` | — | Text channel for restart warnings |
| `STATUS_UPDATE_SECONDS` | `60` | Presence update interval |
| `SNAPSHOT_INTERVAL_SECONDS` | `300` | Population snapshot interval |
| `RESTART_EVERY_HOURS` | `4` | Restart schedule frequency |
| `RESTART_MINUTE` | `0` | Minute within the hour restarts occur |
| `RESTART_WARN_MINUTES` | `60,30,15,10,5,3,1` | Warning thresholds in minutes |
| `ENABLE_PREFIX_COMMANDS` | `true` | Enable `!command` style |
| `ENABLE_SLASH_COMMANDS` | `true` | Enable `/command` style |

## Running as a service (systemd)

```ini
[Unit]
Description=DayZ Discord Bot
After=network.target

[Service]
User=dayz
WorkingDirectory=/home/dayz/dayz-discord-bot
ExecStart=/home/dayz/dayz-discord-bot/venv/bin/python bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save to `/etc/systemd/system/dayz-bot.service`, then:

```bash
systemctl enable --now dayz-bot
```

## License

MIT — see [LICENSE](LICENSE).
