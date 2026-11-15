import asyncio
import json
import logging
import time
from urllib.parse import urlencode

import discord
from discord import app_commands

from .engine import Engine
from .store import Store, avatar_id
from .vrchat import MonitoringError, VRChat


class OwnerTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        config = self.client.config
        if interaction.guild_id != config.guild_id or interaction.user.id != config.owner_id:
            await interaction.response.send_message("Only the configured bot owner can use these commands in the configured server.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error):
        # Avoid dumping interaction contents or arbitrary response bodies into logs.
        logging.error("Discord command failed (%s)", type(error).__name__)
        message = "Command failed. Check /monitor health and the Railway configuration."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


class AvatarBot(discord.Client):
    def __init__(self, config):
        # Guild metadata is needed to validate alert-channel permissions. This is
        # a standard intent, not a privileged members/message-content intent.
        super().__init__(intents=discord.Intents(guilds=True), allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = OwnerTree(self)
        self.tasks = []
        self.delivery_error = ""
        self.store = None
        self.api = None

    async def setup_hook(self):
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.config.data_dir / "monitor.sqlite3")
        self.api = VRChat(self.config.user_agent, self.config.data_dir / "session.json")
        await self.api.start()
        self.engine = Engine(self.config, self.store, self.api)
        self.register_commands()
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.tasks.extend([asyncio.create_task(self.worker()), asyncio.create_task(self.deliver())])

    async def worker(self):
        await self.wait_until_ready()
        await self.engine.run()

    def register_commands(self):
        monitor = app_commands.Group(name="monitor", description="Configure your avatar monitor")
        avatars = app_commands.Group(name="avatar", description="Manage watched avatars")

        @monitor.command(name="setup", description="Choose the alert channel and exactly one person or role to ping")
        @app_commands.describe(person="One person to mention", role="Or one mentionable role", private_alerts="Alert when public avatars become private")
        async def setup(interaction: discord.Interaction, channel: discord.TextChannel,
                        person: discord.Member = None, role: discord.Role = None,
                        private_alerts: bool = True):
            if (person is None) == (role is None):
                await interaction.response.send_message("Choose exactly one person OR one role.", ephemeral=True)
                return
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                await interaction.response.send_message("Give the bot View Channel, Send Messages and Embed Links in that channel.", ephemeral=True)
                return
            if role and (role.is_default() or (not role.mentionable and not perms.mention_everyone)):
                await interaction.response.send_message("Choose a role other than @everyone and make it mentionable so the bot can ping it.", ephemeral=True)
                return
            self.store.set("alerts", dict(channel_id=channel.id, target_type="user" if person else "role",
                                          target_id=(person or role).id, private_alerts=private_alerts))
            await interaction.response.send_message(f"Alert destination saved. Private-status alerts: {'on' if private_alerts else 'off'}. Use /monitor login next.", ephemeral=True)

        @monitor.command(name="private-alerts", description="Enable or disable public-to-private notifications")
        async def private_alerts(interaction: discord.Interaction, enabled: bool):
            settings = self.store.get("alerts")
            if not settings:
                await interaction.response.send_message("Run /monitor setup first.", ephemeral=True)
                return
            settings["private_alerts"] = enabled
            self.store.set("alerts", settings)
            await interaction.response.send_message(f"Private-status alerts {'enabled' if enabled else 'disabled'}.", ephemeral=True)

        @monitor.command(name="login", description="Get a private VRChat device-approval link (owner only)")
        async def login(interaction: discord.Interaction):
            if self.engine.login_running:
                await interaction.response.send_message("A login is already pending. Approve it or wait for it to expire.", ephemeral=True)
                return
            self.engine.login_running = True
            await interaction.response.defer(ephemeral=True, thinking=True)
            task = asyncio.create_task(self.login_flow(interaction))
            self.tasks.append(task)
            task.add_done_callback(lambda completed: self.tasks.remove(completed) if completed in self.tasks else None)

        @monitor.command(name="health", description="See login, polling and delivery health")
        async def health(interaction: discord.Interaction):
            rows = self.store.rows()
            settings = self.store.get("alerts", {})
            message = (
                f"VRChat session: {'connected' if self.api.authenticated else 'not authenticated'}\n"
                f"Owner account: {self.store.get('vrchat_owner', 'not yet bound')}\n"
                f"Watched avatars: {len(rows)}\n"
                f"Target check interval: {self.config.interval}s (backoff may extend this)\n"
                f"Private alerts: {'on' if settings.get('private_alerts', True) else 'off'}\n"
                f"Alert destination: {'configured' if settings else 'run /monitor setup'}\n"
                f"Last successful avatar check (UTC): {self.engine.last_success or 'none this run'}\n"
                f"Last discovery (UTC): {self.store.get('last_discovery', 'none')}\n"
                f"Monitor: {self.engine.health}\n"
                f"Delivery: {self.delivery_error or 'no recorded error'}\n"
                f"Pending alerts: {len(self.store.pending())} (display capped at 20)")
            await interaction.response.send_message(message[:1900], ephemeral=True)

        @avatars.command(name="watch", description="Watch one of your avatars using its URL or ID")
        async def watch(interaction: discord.Interaction, url_or_id: str):
            try:
                aid = avatar_id(url_or_id)
            except ValueError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
            self.store.set("ignored", [value for value in self.store.get("ignored", []) if value != aid])
            self.store.watch(aid)
            await interaction.response.send_message(f"Watching `{aid}`. The first successful check establishes its baseline; later changes trigger alerts.", ephemeral=True)

        @avatars.command(name="unwatch", description="Stop watching an avatar and exclude it from discovery")
        async def unwatch(interaction: discord.Interaction, url_or_id: str):
            try:
                aid = avatar_id(url_or_id)
            except ValueError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
            self.store.unwatch(aid)
            await interaction.response.send_message(f"Stopped watching `{aid}`. Use /avatar watch to add it again.", ephemeral=True)

        @avatars.command(name="list", description="List observations; these are last-known states, not live guarantees")
        async def list_avatars(interaction: discord.Interaction):
            rows = self.store.rows()
            if not rows:
                await interaction.response.send_message("No avatars yet. Log in for automatic discovery, or use /avatar watch.", ephemeral=True)
                return
            # Split messages to stay within Discord's size limits.
            await interaction.response.defer(ephemeral=True)
            lines = []
            for row in rows:
                label = discord.utils.escape_markdown(row["name"] or row["id"])[:90]
                lines.append(f"**{label}** — {row['state']}\n`{row['id']}`\nLast observation (UTC): {row['checked_at'] or 'pending'}\n{row['detail'][:160]}\n")
            chunk = ""
            for line in lines[:100]:
                if len(chunk) + len(line) > 1800:
                    await interaction.followup.send(chunk, ephemeral=True)
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                await interaction.followup.send(chunk, ephemeral=True)
            if len(lines) > 100:
                await interaction.followup.send("Showing the first 100 avatars.", ephemeral=True)

        self.tree.add_command(monitor)
        self.tree.add_command(avatars)

    async def login_flow(self, interaction):
        try:
            device = await self.api.create_device()
            code = str(device["userCode"])
            link = device["verificationUrl"] + "?" + urlencode({"code": code, "source": "app"})
            lifetime = min(900, max(1, int(device["expiresIn"])))
            interval = min(60, max(5, float(device["interval"])))
            await interaction.edit_original_response(content=(
                f"[Approve this bot on VRChat's website]({link})\n"
                f"Code: `{discord.utils.escape_markdown(code)}`\n"
                f"Approve only your own account. This expires in about {lifetime // 60} minutes. "
                "Anyone who approves logs this server into their account; keep this message private. "
                "Monitoring is paused until approval finishes."))
            expires = time.monotonic() + lifetime
            while time.monotonic() < expires:
                await asyncio.sleep(interval)
                if await self.api.check_device(device["deviceCode"]):
                    uid = await self.engine.bind_session()
                    self.engine.attempted_restore = True
                    await interaction.edit_original_response(content=f"Connected to your VRChat account `{uid}`. Automatic avatar discovery will start shortly.")
                    return
            await interaction.edit_original_response(content="VRChat approval expired. Run /monitor login to try again.")
        except MonitoringError as error:
            self.engine.report_error(error)
            await interaction.edit_original_response(content=f"Login did not complete: {error} Run /monitor login when ready to retry.")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.engine.report_error(MonitoringError("Login flow failed; run /monitor login again."))
            try:
                await interaction.edit_original_response(content="Login could not finish. Run /monitor login again; check /monitor health.")
            except discord.HTTPException:
                pass
        finally:
            self.engine.login_running = False

    async def deliver(self):
        await self.wait_until_ready()
        while True:
            try:
                settings = self.store.get("alerts")
                if settings:
                    for event in self.store.pending():
                        channel_id = settings["channel_id"]
                        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.config.guild_id:
                            raise ValueError("Alert channel does not belong to the configured server.")
                        payload = json.loads(event["payload"])
                        title, color = {
                            "unavailable": ("Avatar unavailable — possible takedown", discord.Color.red()),
                            "private": ("Avatar became private", discord.Color.orange()),
                            "recovered": ("Avatar accessible again", discord.Color.green()),
                            "health": ("Avatar monitoring delayed / needs attention", discord.Color.yellow()),
                        }[event["kind"]]
                        embed = discord.Embed(title=title, color=color)
                        if event["kind"] == "health":
                            embed.description = payload["detail"][:1000]
                        else:
                            embed.description = discord.utils.escape_markdown(payload["name"])[:250]
                            embed.url = "https://vrchat.com/home/avatar/" + event["avatar_id"]
                            embed.add_field(name="Avatar ID", value=event["avatar_id"], inline=False)
                            embed.add_field(name="Observed change", value=f"{payload['previous']} → {payload['current']}", inline=False)
                            embed.add_field(name="Details", value=payload["detail"][:800] or "Availability changed.", inline=False)
                            embed.add_field(name="Last known accessible (UTC)", value=payload["last_ok"] or "Unknown", inline=False)
                        embed.add_field(name="Observed at (UTC)", value=payload["observed_at"], inline=False)
                        embed.set_footer(text=f"Event {event['id']} • Observation time is not the actual takedown time")
                        target = discord.Object(id=settings["target_id"])
                        is_user = settings["target_type"] == "user"
                        ping = event["kind"] != "recovered"
                        mention = (f"<@{target.id}>" if is_user else f"<@&{target.id}>") if ping else None
                        allowed = discord.AllowedMentions(everyone=False, replied_user=False,
                            users=[target] if ping and is_user else False,
                            roles=[target] if ping and not is_user else False)
                        await channel.send(content=mention, embed=embed, allowed_mentions=allowed)
                        self.store.delivered(event["id"])
                        self.delivery_error = ""
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.delivery_error = f"Alert delivery failed ({type(error).__name__}); retrying in 30 seconds."
                logging.warning(self.delivery_error)
                await asyncio.sleep(30)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.api:
            await self.api.close()
        if self.store:
            self.store.close()
            self.store = None
        await super().close()
