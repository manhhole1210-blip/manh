"""Customer screens replace prior private screens without affecting other users."""
import asyncio
import importlib.util
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
import test_bot_sqlite as baseline


@unittest.skipUnless(importlib.util.find_spec('discord') and importlib.util.find_spec('requests'), 'Install requirements.txt')
class ShopWindowTests(unittest.TestCase):
    setUp = baseline.BotSQLiteTests.setUp
    tearDown = baseline.BotSQLiteTests.tearDown

    def interaction(self, user_id=42, channel=100):
        interaction = baseline.BotSQLiteTests.interaction(self)
        interaction.user = SimpleNamespace(id=user_id)
        interaction.channel_id = channel
        interaction.delete_original_response = AsyncMock()
        return interaction

    def test_buy_then_history_deletes_only_previous_private_response(self):
        async def scenario():
            old, new = self.interaction(), self.interaction()
            await self.bot.open_buy_flow(old)
            view = self.bot.ShopPanelView('vi')
            button = next(b for b in view.children if b.custom_id == 'lemon:shop:orders')
            await button.callback(new)
            old.delete_original_response.assert_awaited_once()
            new.delete_original_response.assert_not_awaited()
            self.assertTrue(new.response.send_message.call_args.kwargs['ephemeral'])
        asyncio.run(scenario())

    def test_user_and_channel_isolation(self):
        async def scenario():
            first = self.interaction()
            other_user = self.interaction(99)
            other_channel = self.interaction(channel=101)
            for item in (first, other_user, other_channel):
                await self.bot.send_shop_response(item, 'private')
            for item in (first, other_user, other_channel):
                item.delete_original_response.assert_not_awaited()
            await self.bot.send_shop_response(self.interaction(), 'replacement')
            first.delete_original_response.assert_awaited_once()
            other_user.delete_original_response.assert_not_awaited()
            other_channel.delete_original_response.assert_not_awaited()
        asyncio.run(scenario())

    def test_failed_replacement_keeps_old_window(self):
        async def scenario():
            first, second = self.interaction(), self.interaction()
            await self.bot.send_shop_response(first, 'old')
            second.response.send_message.side_effect = OSError('network failure')
            with self.assertRaises(OSError):
                await self.bot.send_shop_response(second, 'new')
            first.delete_original_response.assert_not_awaited()
            self.assertIs(self.bot._SHOP_WINDOWS[self.bot.shop_window_key(first)][0], first)
        asyncio.run(scenario())

    def test_expired_old_window_does_not_break_new_window(self):
        async def scenario():
            first, second = self.interaction(), self.interaction()
            await self.bot.send_shop_response(first, 'old')
            first.delete_original_response.side_effect = OSError('expired token')
            await self.bot.send_shop_response(second, 'new')
            second.response.send_message.assert_awaited_once()
        asyncio.run(scenario())

    def test_concurrent_clicks_leave_last_response_and_remove_earlier_ones(self):
        async def scenario():
            items = [self.interaction() for _ in range(4)]
            await asyncio.gather(*(self.bot.send_shop_response(item, 'screen') for item in items))
            for item in items[:-1]:
                item.delete_original_response.assert_awaited_once()
            items[-1].delete_original_response.assert_not_awaited()
        asyncio.run(scenario())

    def test_modal_open_dismisses_prior_private_screen(self):
        async def scenario():
            first, new = self.interaction(), self.interaction()
            new.user = self.user
            new.response.send_modal = AsyncMock()
            await self.bot.send_shop_response(first, 'old')
            view = self.bot.ShopPanelView('vi')
            button = next(b for b in view.children if b.custom_id == 'lemon:shop:topup')
            await button.callback(new)
            new.response.send_modal.assert_awaited_once()
            first.delete_original_response.assert_awaited_once()
        asyncio.run(scenario())

    def test_followup_screen_replaces_previous_response(self):
        async def scenario():
            old, new = self.interaction(), self.interaction()
            await self.bot.send_shop_response(old, 'old')
            await self.bot.send_shop_response(new, 'payment', followup=True)
            new.followup.send.assert_awaited_once()
            old.delete_original_response.assert_awaited_once()
        asyncio.run(scenario())
