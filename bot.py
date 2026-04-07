import os
import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import a2s
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    token: str
    guild_id: Optional[int]
    server_host: str
    server_port: int
    status_channel_id: Optional[int]
    alerts_channel_id: Optional[int]
    status_update_seconds: int
    snapshot_interval_seconds: int
    command_prefix: str
    enable_prefix_commands: bool
    enable_slash_commands: bool
    restart_minute: int
    restart_every_hours: int
    restart_warn_minutes: list[int]
    data_dir: Path

    @staticmethod
    def _int_env(name: str, default: Optional[int] = None) -> Optional[int]:
        raw = os.getenv(name, "").strip()
        return int(raw) if raw else default

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN is required — set it in .env or environment")

        warn_raw = os.getenv("RESTART_WARN_MINUTES", "60,30,15,10,5,3,1")
        warn_minutes = sorted(
            {int(x.strip()) for x in warn_raw.split(",") if x.strip().isdigit()},
            reverse=True,
        )

        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            token=token,
            guild_id=cls._int_env("DISCORD_GUILD_ID"),
            server_host=os.getenv("DAYZ_QUERY_HOST", "127.0.0.1"),
            server_port=int(os.getenv("DAYZ_QUERY_PORT", "27016")),
            status_channel_id=cls._int_env("STATUS_CHANNEL_ID"),
            alerts_channel_id=cls._int_env("ALERTS_CHANNEL_ID"),
            status_update_seconds=int(os.getenv("STATUS_UPDATE_SECONDS", "60")),
            snapshot_interval_seconds=int(os.getenv("SNAPSHOT_INTERVAL_SECONDS", "300")),
            command_prefix=os.getenv("COMMAND_PREFIX", "!"),
            enable_prefix_commands=cls._bool_env("ENABLE_PREFIX_COMMANDS", True),
            enable_slash_commands=cls._bool_env("ENABLE_SLASH_COMMANDS", True),
            restart_minute=int(os.getenv("RESTART_MINUTE", "0")),
            restart_every_hours=int(os.getenv("RESTART_EVERY_HOURS", "4")),
            restart_warn_minutes=warn_minutes,
            data_dir=data_dir,
        )


cfg = Config.load()
SERVER_ADDRESS = (cfg.server_host, cfg.server_port)

intents = discord.Intents.default()
intents.message_content = cfg.enable_prefix_commands
bot = commands.Bot(command_prefix=cfg.command_prefix, intents=intents)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def next_restart_utc(now: Optional[datetime] = None) -> datetime:
    now = now or utc_now()
    block = (now.hour // cfg.restart_every_hours) * cfg.restart_every_hours
    next_hour = block + cfg.restart_every_hours
    base = now.replace(minute=cfg.restart_minute, second=0, microsecond=0)
    if next_hour >= 24:
        return (base + timedelta(days=1)).replace(hour=0)
    return base.replace(hour=next_hour)


def restart_countdown_text(now: Optional[datetime] = None) -> str:
    now = now or utc_now()
    delta = max(timedelta(0), next_restart_utc(now) - now)
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m"


def restart_label_text(now: Optional[datetime] = None) -> str:
    now = now or utc_now()
    nxt = next_restart_utc(now)
    return f"{nxt:%H:%M} UTC (in {restart_countdown_text(now)})"


def fmt_uptime(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


async def fetch_info():
    return await asyncio.to_thread(a2s.info, SERVER_ADDRESS)


async def fetch_players():
    return await asyncio.to_thread(a2s.players, SERVER_ADDRESS)


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

_warned_marks: set[int] = set()
_last_restart_key: Optional[str] = None


async def status_loop():
    """Update bot presence and optional voice channel name every interval."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            info = await fetch_info()
            countdown = restart_countdown_text()
            await bot.change_presence(
                activity=discord.Game(name=f"{info.player_count}/{info.max_players} online | Restart in {countdown}")
            )
            if cfg.status_channel_id:
                channel = bot.get_channel(cfg.status_channel_id)
                if channel:
                    new_name = f"🟢 {info.player_count}/{info.max_players} | {countdown}"
                    if getattr(channel, "name", None) != new_name:
                        await channel.edit(name=new_name)
        except Exception:
            await bot.change_presence(activity=discord.Game(name="Server Offline"))
            if cfg.status_channel_id:
                channel = bot.get_channel(cfg.status_channel_id)
                if channel and getattr(channel, "name", "") != "🔴 Server Offline":
                    await channel.edit(name="🔴 Server Offline")

        await asyncio.sleep(max(15, cfg.status_update_seconds))


async def snapshot_loop():
    """Append a population snapshot to data/population_snapshots.jsonl."""
    await bot.wait_until_ready()
    snapshot_path = cfg.data_dir / "population_snapshots.jsonl"
    while not bot.is_closed():
        row: dict = {"ts": utc_now().isoformat(), "online": None, "max": None, "ok": False}
        try:
            info = await fetch_info()
            row.update({"online": int(info.player_count), "max": int(info.max_players), "ok": True})
        except Exception:
            pass

        with snapshot_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

        await asyncio.sleep(max(60, cfg.snapshot_interval_seconds))


async def restart_warning_loop():
    """Post countdown warnings to ALERTS_CHANNEL_ID before each restart."""
    global _warned_marks, _last_restart_key
    await bot.wait_until_ready()

    while not bot.is_closed():
        channel = bot.get_channel(cfg.alerts_channel_id) if cfg.alerts_channel_id else None
        now = utc_now()
        nxt = next_restart_utc(now)
        key = nxt.isoformat()

        # Reset tracking when we roll over to the next restart window
        if _last_restart_key != key:
            _last_restart_key = key
            _warned_marks = set()

        remaining = int((nxt - now).total_seconds() // 60)

        for mark in cfg.restart_warn_minutes:
            if remaining <= mark and mark not in _warned_marks:
                _warned_marks.add(mark)
                if channel:
                    await channel.send(
                        f"⏳ Server restart in **{mark} minute(s)** (ETA {nxt:%H:%M} UTC)."
                    )
                break

        if remaining <= 0 and 0 not in _warned_marks:
            _warned_marks.add(0)
            if channel:
                await channel.send("🔄 Scheduled restart window reached. Server may be briefly unavailable.")

        await asyncio.sleep(20)


# ---------------------------------------------------------------------------
# Prefix commands
# ---------------------------------------------------------------------------

if cfg.enable_prefix_commands:

    @bot.command()
    async def players(ctx: commands.Context):
        try:
            player_list = await fetch_players()
            info = await fetch_info()

            if not player_list and info.player_count == 0:
                await ctx.send("No players currently online.")
                return

            lines, unnamed = [], 0
            for p in player_list:
                name = (p.name or "").strip()
                if not name:
                    unnamed += 1
                    name = f"Unnamed player #{unnamed}"
                lines.append(f"• {name} ({int(p.duration // 60)} mins)")

            if not lines and info.player_count > 0:
                lines.append(f"• {info.player_count} player(s) online (query returned no names)")

            embed = discord.Embed(title="Players Online", description="\n".join(lines), color=0x22C55E)
            embed.set_footer(text=f"Reported: {info.player_count}/{info.max_players}")
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"Could not retrieve player list: {e}")

    @bot.command()
    async def server(ctx: commands.Context):
        try:
            info = await fetch_info()
            embed = discord.Embed(title="Server Info", color=0x22C55E)
            embed.add_field(name="Server", value=info.server_name, inline=False)
            embed.add_field(name="Map", value=info.map_name, inline=True)
            embed.add_field(name="Players", value=f"{info.player_count}/{info.max_players}", inline=True)
            embed.add_field(name="Next Restart", value=restart_label_text(), inline=True)
            embed.add_field(name="Version", value=info.version, inline=True)
            embed.add_field(name="Ping", value=f"{round(info.ping * 1000)}ms", inline=True)
            embed.add_field(name="Password", value="Yes" if info.password_protected else "No", inline=True)
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"Could not retrieve server info: {e}")

    @bot.command()
    async def nextrestart(ctx: commands.Context):
        await ctx.send(f"Next restart: **{restart_label_text()}**")

    @bot.command()
    async def uptime(ctx: commands.Context):
        try:
            pids_raw = subprocess.check_output(["pgrep", "-f", r"(^|/)DayZServer(\s|$)"], text=True)
            pids = [p.strip() for p in pids_raw.splitlines() if p.strip().isdigit()]

            uptimes = []
            for pid in pids:
                etimes = subprocess.check_output(["ps", "-o", "etimes=", "-p", pid], text=True).strip()
                if etimes.isdigit():
                    uptimes.append(int(etimes))

            if not uptimes:
                await ctx.send("Server does not appear to be running right now.")
                return

            # Report the longest-running instance if multiple exist
            await ctx.send(f"Server has been online for **{fmt_uptime(max(uptimes))}**")
        except subprocess.CalledProcessError:
            await ctx.send("Server does not appear to be running right now.")
        except Exception as e:
            await ctx.send(f"Could not retrieve uptime: {e}")


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

if cfg.enable_slash_commands:

    @bot.tree.command(name="players", description="Show currently online players")
    async def slash_players(interaction: discord.Interaction):
        try:
            player_list = await fetch_players()
            info = await fetch_info()

            if not player_list and info.player_count == 0:
                await interaction.response.send_message("No players currently online.")
                return

            lines, unnamed = [], 0
            for p in player_list:
                name = (p.name or "").strip()
                if not name:
                    unnamed += 1
                    name = f"Unnamed player #{unnamed}"
                lines.append(f"• {name} ({int(p.duration // 60)} mins)")

            if not lines:
                lines.append(f"• {info.player_count} player(s) online (query returned no names)")

            embed = discord.Embed(title="Players Online", description="\n".join(lines), color=0x22C55E)
            embed.set_footer(text=f"Reported: {info.player_count}/{info.max_players}")
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"Could not retrieve player list: {e}", ephemeral=True)

    @bot.tree.command(name="server", description="Show DayZ server status")
    async def slash_server(interaction: discord.Interaction):
        try:
            info = await fetch_info()
            embed = discord.Embed(title="Server Info", color=0x22C55E)
            embed.add_field(name="Server", value=info.server_name, inline=False)
            embed.add_field(name="Map", value=info.map_name, inline=True)
            embed.add_field(name="Players", value=f"{info.player_count}/{info.max_players}", inline=True)
            embed.add_field(name="Next Restart", value=restart_label_text(), inline=True)
            embed.add_field(name="Version", value=info.version, inline=True)
            embed.add_field(name="Ping", value=f"{round(info.ping * 1000)}ms", inline=True)
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            await interaction.response.send_message(f"Could not retrieve server info: {e}", ephemeral=True)

    @bot.tree.command(name="nextrestart", description="Show next scheduled restart time")
    async def slash_nextrestart(interaction: discord.Interaction):
        await interaction.response.send_message(f"Next restart: **{restart_label_text()}**")

    @bot.tree.command(name="population", description="Population trend from recent snapshots")
    async def slash_population(interaction: discord.Interaction):
        path = cfg.data_dir / "population_snapshots.jsonl"
        if not path.exists():
            await interaction.response.send_message("No population data yet.", ephemeral=True)
            return

        rows = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue

        recent = rows[-72:]  # ~6 hours at 5-minute intervals
        vals = [r["online"] for r in recent if r.get("ok") and isinstance(r.get("online"), int)]
        if not vals:
            await interaction.response.send_message("No successful snapshots in the recent window.", ephemeral=True)
            return

        avg = sum(vals) / len(vals)
        await interaction.response.send_message(
            f"Population (last ~6h): avg **{avg:.1f}**, peak **{max(vals)}**, low **{min(vals)}**"
        )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    if cfg.enable_slash_commands:
        if cfg.guild_id:
            guild = discord.Object(id=cfg.guild_id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {cfg.guild_id}")
        else:
            synced = await bot.tree.sync()
            print(f"Globally synced {len(synced)} slash command(s)")

    asyncio.create_task(status_loop())
    asyncio.create_task(snapshot_loop())
    asyncio.create_task(restart_warning_loop())


bot.run(cfg.token)
