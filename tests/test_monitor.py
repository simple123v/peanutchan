import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from monitor.config import Config
from monitor.engine import Engine
from monitor.store import Store, avatar_id
from monitor.vrchat import VRChat, AuthenticationRequired, MonitoringError, Throttled
from monitor.bot import AvatarBot


AID = "avtr_31d1567d-5b56-4859-9f3a-673baef1660a"
OWNER = "usr_11111111-1111-1111-1111-111111111111"


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "monitor.sqlite3"
        self.store = Store(self.path)
        self.store.watch(AID)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_private_and_initial_missing_baselines_do_not_alert(self):
        self.store.observe(AID, "unavailable")
        self.store.observe(AID, "private", "My avatar")
        self.assertEqual(self.store.pending(), [])

    def test_private_transition_defaults_on(self):
        self.store.observe(AID, "public", "Name")
        self.store.observe(AID, "private", "Name")
        self.assertEqual([e["kind"] for e in self.store.pending()], ["private"])

    def test_private_option_does_not_disable_takedown_detection(self):
        self.store.set("alerts", {"private_alerts": False})
        self.store.observe(AID, "public", "Name")
        self.store.observe(AID, "private", "Name")
        self.store.observe(AID, "unavailable", detail="404")
        self.assertEqual([e["kind"] for e in self.store.pending()], ["unavailable"])

    def test_one_alert_per_incident_and_recovery(self):
        self.store.observe(AID, "public", "Name")
        self.store.observe(AID, "unavailable")
        self.store.observe(AID, "unavailable")
        self.store.observe(AID, "public", "Renamed")
        self.store.observe(AID, "public", "Renamed again")
        self.assertEqual([e["kind"] for e in self.store.pending()], ["unavailable", "recovered"])

    def test_monitor_errors_preserve_state_and_do_not_emit_takedowns(self):
        self.store.observe(AID, "public", "Name")
        last_ok = self.store.rows()[0]["last_ok"]
        self.store.error(AID, "HTTP 401")
        row = self.store.rows()[0]
        self.assertEqual((row["state"], row["last_ok"]), ("public", last_ok))
        self.assertEqual(self.store.pending(), [])

    def test_pending_delivery_survives_restart_and_does_not_repeat_after_ack(self):
        self.store.observe(AID, "public", "Name")
        self.store.observe(AID, "unavailable")
        self.store.close()
        self.store = Store(self.path)
        event = self.store.pending()[0]
        self.store.delivered(event["id"])
        self.store.observe(AID, "unavailable")
        self.assertEqual(self.store.pending(), [])

    def test_removed_watch_cancels_undelivered_avatar_alert(self):
        self.store.observe(AID, "public")
        self.store.observe(AID, "unavailable")
        self.store.unwatch(AID)
        self.store.observe(AID, "public")  # Simulate an in-flight lookup finishing.
        self.assertEqual(self.store.rows(), [])
        self.assertEqual(self.store.pending(), [])
        self.assertIn(AID, self.store.get("ignored"))

    def test_url_validation_rejects_wrong_hosts_and_embedded_ids(self):
        self.assertEqual(avatar_id(f"https://vrchat.com/home/avatar/{AID}?x=1"), AID)
        for value in [f"https://evil.test/home/avatar/{AID}", f"garbage{AID}", f"http://vrchat.com/home/avatar/{AID}"]:
            with self.assertRaises(ValueError):
                avatar_id(value)


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status, self.data, self.headers = status, data, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class APITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = VRChat("Test/1 owner@example.com", Path("unused-session.json"))

    async def test_private_status_is_read_from_data(self):
        self.api.http = Transport([Response(data={"id": AID, "authorId": OWNER, "name": "Name", "releaseStatus": "private"})])
        state, _, _ = await self.api.avatar(AID, OWNER)
        self.assertEqual(state, "private")

    async def test_401_is_not_a_takedown(self):
        self.api.authenticated = True
        self.api.http = Transport([Response(401)])
        with self.assertRaises(AuthenticationRequired):
            await self.api.avatar(AID, OWNER)
        self.assertFalse(self.api.authenticated)

    async def test_429_stops_all_followup_requests(self):
        self.api.http = Transport([Response(429, headers={"Retry-After": "120"})])
        with self.assertRaises(Throttled):
            await self.api.avatar(AID, OWNER)
        self.assertGreater(self.api.blocked_until - time.monotonic(), 119)
        with self.assertRaises(Throttled):
            await self.api.request("GET", "/auth/user")
        self.assertEqual(len(self.api.http.calls), 1)

    async def test_5xx_is_not_a_takedown(self):
        self.api.http = Transport([Response(503)])
        with self.assertRaises(Throttled):
            await self.api.avatar(AID, OWNER)

    async def test_404_requires_healthy_matching_owner_session(self):
        self.api.http = Transport([Response(404)])
        self.api.current_user = AsyncMock(return_value=OWNER)
        self.assertEqual((await self.api.avatar(AID, OWNER))[0], "unavailable")
        self.api.current_user.assert_awaited_once()

    async def test_404_with_expired_session_is_monitoring_error(self):
        self.api.http = Transport([Response(404)])
        self.api.current_user = AsyncMock(side_effect=AuthenticationRequired("expired"))
        with self.assertRaises(AuthenticationRequired):
            await self.api.avatar(AID, OWNER)

    async def test_wrong_author_is_not_observed(self):
        self.api.http = Transport([Response(data={"id": AID, "authorId": "someone_else", "name": "Name", "releaseStatus": "public"})])
        with self.assertRaises(MonitoringError):
            await self.api.avatar(AID, OWNER)

    async def test_unrecognized_status_and_malformed_response_are_errors(self):
        for payload in [{"id": AID, "authorId": OWNER, "name": "Name", "releaseStatus": "new_status"}, ["bad"]]:
            self.api.next_request = 0
            self.api.http = Transport([Response(data=payload)])
            with self.assertRaises(MonitoringError):
                await self.api.avatar(AID, OWNER)

    async def test_non_json_is_not_takedown(self):
        self.api.http = Transport([Response(data=ValueError("not json"))])
        with self.assertRaises(MonitoringError):
            await self.api.avatar(AID, OWNER)

    async def test_discovery_requests_all_statuses_and_rejects_wrong_author(self):
        self.api.http = Transport([Response(data=[{"id": AID, "authorId": "different"}])])
        with self.assertRaises(MonitoringError):
            await self.api.own_avatars(OWNER)
        params = self.api.http.calls[0][2]["params"]
        self.assertEqual(params["releaseStatus"], "all")
        self.assertEqual(params["user"], "me")

    async def test_session_cookie_roundtrip_uses_json(self):
        from yarl import URL
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            api = VRChat("Test/1 owner@example.com", path)
            await api.start()
            try:
                api.http.cookie_jar.update_cookies({"auth": "fake-session-test-value"}, response_url=URL(api.base))
                api.save_session()
            finally:
                await api.close()
            restored = VRChat("Test/1 owner@example.com", path)
            await restored.start()
            try:
                self.assertTrue(restored.has_session())
                restored.clear_session()
                self.assertFalse(path.exists())
            finally:
                await restored.close()

    async def test_wrong_account_approval_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            try:
                store.set("vrchat_owner", OWNER)
                api = AsyncMock()
                api.current_user.return_value = "usr_wrong_account"
                # clear_session and save_session are synchronous.
                from unittest.mock import Mock
                api.clear_session = Mock()
                api.save_session = Mock()
                engine = Engine(Config("x", 1, 2, "Test/1 c@example.com", Path(directory)), store, api)
                with self.assertRaises(AuthenticationRequired):
                    await engine.bind_session()
                api.clear_session.assert_called_once()
                api.save_session.assert_not_called()
            finally:
                store.close()


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_reject_everyone_except_owner_in_configured_server(self):
        from unittest.mock import Mock
        config = Config("x", 123, 456, "Test/1 c@example.com", Path("unused"))
        bot = AvatarBot(config)
        try:
            for guild_id, user_id, permitted in [(123, 456, True), (123, 999, False), (999, 456, False), (None, 456, False)]:
                interaction = Mock()
                interaction.guild_id = guild_id
                interaction.user.id = user_id
                interaction.response.send_message = AsyncMock()
                self.assertEqual(await bot.tree.interaction_check(interaction), permitted)
                if not permitted:
                    self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        finally:
            await bot.close()

    async def test_alert_mentions_only_configured_target_and_recovery_never_pings(self):
        import discord
        from unittest.mock import Mock, patch
        for target_type, event_kind in [("user", "private"), ("role", "unavailable"), ("role", "recovered")]:
            with tempfile.TemporaryDirectory() as directory:
                bot = AvatarBot(Config("x", 123, 456, "Test/1 c@example.com", Path(directory)))
                bot.store = Store(Path(directory) / "test.sqlite3")
                bot.store.set("alerts", dict(channel_id=789, target_type=target_type, target_id=999))
                bot.store.watch(AID)
                bot.store.observe(AID, "public", "@everyone Name")
                if event_kind == "private":
                    bot.store.observe(AID, "private")
                else:
                    bot.store.observe(AID, "unavailable")
                    if event_kind == "recovered":
                        bot.store.delivered(bot.store.pending()[0]["id"])
                        bot.store.observe(AID, "public")
                channel = Mock(spec=discord.TextChannel)
                channel.guild.id = 123
                channel.send = AsyncMock()
                bot.get_channel = Mock(return_value=channel)
                bot.wait_until_ready = AsyncMock()
                try:
                    with patch("monitor.bot.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
                        with self.assertRaises(asyncio.CancelledError):
                            await bot.deliver()
                    args = channel.send.call_args.kwargs
                    allowed = args["allowed_mentions"].to_dict()
                    self.assertNotIn("everyone", allowed.get("parse", []))
                    if event_kind == "recovered":
                        self.assertIsNone(args["content"])
                        self.assertNotIn("users", allowed)
                        self.assertNotIn("roles", allowed)
                    else:
                        key = "users" if target_type == "user" else "roles"
                        self.assertEqual(allowed[key], [999])
                    self.assertEqual(bot.store.pending(), [])
                finally:
                    await bot.close()


if __name__ == "__main__":
    unittest.main()
