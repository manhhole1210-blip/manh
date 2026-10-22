"""Optional public stock announcements: no credentials, no duplicate posts."""
import asyncio
import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import test_bot_sqlite as baseline


@unittest.skipUnless(importlib.util.find_spec('discord') and importlib.util.find_spec('requests'), 'Install requirements.txt')
class StockNotifyTests(unittest.TestCase):
    setUp = baseline.BotSQLiteTests.setUp
    tearDown = baseline.BotSQLiteTests.tearDown
    interaction = baseline.BotSQLiteTests.interaction
    upload_interaction = baseline.BotSQLiteTests.upload_interaction
    attachment = baseline.BotSQLiteTests.attachment

    def channel(self, guild_id=123):
        return SimpleNamespace(id=456, guild=SimpleNamespace(id=guild_id), mention='<#456>', send=AsyncMock(),
            permissions_for=lambda member: SimpleNamespace(view_channel=True, send_messages=True, embed_links=True))

    def configure(self, channel):
        self.bot.save_json(self.bot.stock_notify_config_path(), {'123': channel.id})
        self.bot.bot.get_channel = lambda channel_id: channel

    def view(self):
        return self.bot.StockNotifyView(42, 123, 'default', 'Public Product', 3, 'vi')

    def test_upload_asks_yes_no_without_posting_automatically(self):
        async def scenario():
            channel = self.channel()
            self.configure(channel)
            interaction = self.upload_interaction()
            await self.bot.addstock.callback(interaction, 'default', self.attachment(b'private1:password\n'))
            response = interaction.followup.send.call_args.kwargs
            self.assertTrue(response['ephemeral'])
            self.assertIsInstance(response['view'], self.bot.StockNotifyView)
            channel.send.assert_not_awaited()
        asyncio.run(scenario())

    def test_yes_posts_once_and_never_exposes_stock(self):
        async def scenario():
            channel = self.channel()
            self.configure(channel)
            view = self.view()
            await asyncio.gather(view.children[0].callback(self.interaction()), view.children[0].callback(self.interaction()))
            channel.send.assert_awaited_once()
            kwargs = channel.send.call_args.kwargs
            content = str(kwargs['embed'].to_dict())
            self.assertIn('Public Product', content)
            self.assertIn('Stock Added', content)
            self.assertIn('Quantity added', content)
            self.assertIn('Use /stock', content)
            self.assertIn('Dùng /stock', content)
            self.assertNotIn('password', content)
            self.assertNotIn('account1', content)
            self.assertNotIn('file', kwargs)
            self.assertFalse(kwargs['allowed_mentions'].everyone)
        asyncio.run(scenario())

    def test_no_disables_posting(self):
        async def scenario():
            channel = self.channel()
            self.configure(channel)
            view = self.view()
            await view.children[1].callback(self.interaction())
            await view.children[0].callback(self.interaction())
            channel.send.assert_not_awaited()
        asyncio.run(scenario())

    def test_missing_channel_and_cross_guild_are_rejected(self):
        async def scenario():
            view = self.view()
            await view.children[0].callback(self.interaction())
            self.assertFalse(view.completed)
            channel = self.channel(999)
            self.configure(channel)
            await view.children[0].callback(self.interaction())
            channel.send.assert_not_awaited()
        asyncio.run(scenario())

    def test_other_user_and_revoked_admin_cannot_post(self):
        async def scenario():
            channel = self.channel()
            self.configure(channel)
            view = self.view()
            interaction = self.interaction()
            interaction.user = SimpleNamespace(id=99)
            await view.children[0].callback(interaction)
            self.bot.POLICY.config['admin_user_ids'] = []
            await view.children[0].callback(self.interaction())
            channel.send.assert_not_awaited()
        asyncio.run(scenario())

    def test_send_failure_does_not_reimport_and_can_retry(self):
        async def scenario():
            channel = self.channel()
            channel.send.side_effect = OSError('network failure')
            self.configure(channel)
            view = self.view()
            await view.children[0].callback(self.interaction())
            self.assertFalse(view.completed)
            self.assertEqual(len(self.bot.load_stock()), 2)
            channel.send.side_effect = None
            await view.children[0].callback(self.interaction())
            self.assertTrue(view.completed)
        asyncio.run(scenario())

    def test_set_channel_persists_per_guild(self):
        interaction = self.interaction()
        interaction.guild.me = SimpleNamespace(id=1)
        asyncio.run(self.bot.setstocknotify.callback(interaction, self.channel()))
        self.assertEqual(self.bot.load_json(self.bot.stock_notify_config_path(), {}), {'123': 456})

    def test_duplicate_only_upload_does_not_ask_to_announce(self):
        async def scenario():
            interaction = self.upload_interaction()
            await self.bot.addstock.callback(interaction, 'default', self.attachment(b'account1:pass1\n'))
            self.assertNotIn('view', interaction.followup.send.call_args.kwargs)
        asyncio.run(scenario())
