"""Paid preorder invariants with real SQLite and mocked Discord."""
import asyncio
import importlib.util
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import test_bot_sqlite as baseline
from sqlite_store import SQLiteStore


@unittest.skipUnless(importlib.util.find_spec('discord') and importlib.util.find_spec('requests'), 'Install requirements.txt')
class PreorderTests(unittest.TestCase):
    setUp = baseline.BotSQLiteTests.setUp
    tearDown = baseline.BotSQLiteTests.tearDown
    interaction = baseline.BotSQLiteTests.interaction

    def book(self, name='PRE-TEST', quantity=2, user=None):
        user = user or self.user
        product = self.bot.get_product_by_id('default')
        return self.bot.create_paid_preorder(name, user, 123, 'default', quantity,
                                             self.bot.product_price(product, quantity))

    def test_payment_is_once_and_survives_restart(self):
        row, created = self.book()
        balance = self.bot.get_user_balance(42)
        self.assertEqual(balance, 10000 - row['total'])
        self.assertTrue(created)
        self.assertFalse(self.book()[1])
        self.assertEqual(self.bot.get_user_balance(42), balance)
        self.bot.STORE.close()
        self.bot.STORE = SQLiteStore(self.root)
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'waiting')
        self.assertEqual(self.bot.load_json(self.bot.USERS_FILE, {})['42']['balance'], balance)

    def test_cancel_refunds_once_and_rejects_another_owner(self):
        row, _ = self.book()
        with self.assertRaises(ValueError):
            self.bot.refund_preorder(row['id'], 99)
        self.assertTrue(self.bot.refund_preorder(row['id'], 42))
        self.assertFalse(self.bot.refund_preorder(row['id'], 42))
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertEqual(len(self.bot.load_stock()), 2)
        self.assertEqual(self.bot.load_orders(), {})

    def test_allocation_uses_locked_price_and_does_not_debit_twice(self):
        row, _ = self.book()
        balance = self.bot.get_user_balance(42)
        self.assertEqual(self.bot.allocate_preorders(), [row['id']])
        self.assertEqual(self.bot.allocate_preorders(), [])
        order = self.bot.load_orders()[row['id']]
        self.assertEqual(order['accounts'], ['account1:pass1', 'account2:pass2'])
        self.assertEqual(order['total'], row['total'])
        self.assertEqual(order['warranty_days'], row['warranty_days'])
        self.assertGreaterEqual(order['created_at'], row['created_at'])
        self.assertEqual(self.bot.get_user_balance(42), balance)
        self.assertEqual(self.bot.STORE.delivery(row['id'])['status'], 'pending')
        self.assertFalse(self.bot.refund_preorder(row['id'], 42))
        self.assertEqual(self.bot.load_stock(), [])

    def test_fifo_does_not_skip_large_older_order(self):
        first, _ = self.book(quantity=3)
        second, _ = self.book('PRE-SECOND', quantity=1)
        self.assertEqual(self.bot.allocate_preorders(), [])
        self.bot.save_stock(['a', 'b', 'c', 'd'])
        self.assertEqual(self.bot.allocate_preorders(), [first['id'], second['id']])
        self.assertEqual(self.bot.load_orders()[first['id']]['accounts'], ['a', 'b', 'c'])
        self.assertEqual(self.bot.load_orders()[second['id']]['accounts'], ['d'])

    def test_insufficient_wallet_and_changed_quote_never_charge(self):
        with self.assertRaises(ValueError):
            self.book(quantity=50000)
        with self.assertRaises(ValueError):
            self.bot.create_paid_preorder('PRE-BAD', self.user, 123, 'default', 1, 1)
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertEqual(self.bot.STORE.list_preorders(), [])

    def test_failed_payment_write_rolls_back_balance(self):
        with patch.object(self.bot, 'log_activity', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.book()
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertEqual(self.bot.STORE.list_preorders(), [])

    def test_failed_allocation_keeps_paid_order_waiting_and_inventory(self):
        row, _ = self.book()
        balance = self.bot.get_user_balance(42)
        with patch.object(self.bot, 'save_order', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.bot.allocate_preorders()
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'waiting')
        self.assertEqual(len(self.bot.load_stock()), 2)
        self.assertEqual(self.bot.get_user_balance(42), balance)
        self.assertEqual(self.bot.load_orders(), {})
        self.assertEqual(self.bot.STORE.db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 0)

    def test_product_removal_refunds_and_preserves_preorder_record(self):
        row, _ = self.book()
        _, _, digest = self.bot.product_stock_snapshot(self.bot.get_default_product())
        status, _ = self.bot.remove_product('default', digest)
        self.assertEqual(status, 'removed')
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'cancelled')

    def test_restore_pause_blocks_payment_refund_and_allocation(self):
        row, _ = self.book()
        self.bot.STORE.db.execute("INSERT OR REPLACE INTO metadata VALUES ('payments_paused_after_restore','1')")
        with self.assertRaises(ValueError):
            self.book('PRE-PAUSED')
        with self.assertRaises(ValueError):
            self.bot.refund_preorder(row['id'], 42)
        self.assertEqual(self.bot.allocate_preorders(), [])

    def test_confirm_twice_then_dm_failure_never_recharges(self):
        self.bot.notify_admins = AsyncMock()
        self.user.send.side_effect = OSError('DM blocked')
        async def scenario():
            product = self.bot.get_default_product()
            view = self.bot.ConfirmPreorderView(42, product, 2, self.bot.product_price(product, 2), 'vi')
            await view.children[0].callback(self.interaction())
            await view.children[0].callback(self.interaction())
            rows = self.bot.STORE.list_preorders(user_id=42)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(self.bot.get_user_balance(42), 10000-row['total'])
            self.assertEqual(self.bot.STORE.delivery(row['id'])['status'], 'failed')
            self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'fulfilled')
            self.assertEqual(self.bot.load_stock(), [])
        asyncio.run(scenario())

    def test_normal_purchase_cannot_jump_waiting_preorder(self):
        self.book(quantity=3)
        balance = self.bot.get_user_balance(42)
        async def scenario():
            view = self.bot.ConfirmBuyView(42, 1, 2500)
            await view.children[0].callback(self.interaction())
        asyncio.run(scenario())
        self.assertEqual(self.bot.get_user_balance(42), balance)
        self.assertEqual(len(self.bot.load_stock()), 2)
        self.assertEqual(self.bot.load_orders(), {})

    def test_help_fields_fit_discord_limits_and_customer_list_is_private(self):
        for language in ('vi', 'en'):
            for field in self.bot.build_help_embed(language).fields:
                self.assertLessEqual(len(field.value), 1024)
        self.book()
        other = self.interaction()
        other.user = SimpleNamespace(id=99)
        asyncio.run(self.bot.mypreorders.callback(other))
        message = other.response.send_message.call_args.kwargs
        self.assertTrue(message['ephemeral'])
        self.assertNotIn('PRE-TEST', message['embed'].description)

    def test_addstock_fulfills_preorder_and_sends_without_second_charge(self):
        self.bot.save_stock([])
        row, _ = self.book()
        balance = self.bot.get_user_balance(42)
        self.assertEqual(self.bot.allocate_preorders(), [])
        async def scenario():
            interaction = baseline.BotSQLiteTests.upload_interaction(self)
            attachment = baseline.BotSQLiteTests.attachment(self, b'new1:pw\nnew2:pw\nnew3:pw\n')
            await self.bot.addstock.callback(interaction, 'default', attachment)
        asyncio.run(scenario())
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'fulfilled')
        self.assertEqual(self.bot.STORE.delivery(row['id'])['status'], 'sent')
        self.assertEqual(self.bot.get_user_balance(42), balance)
        self.assertEqual(self.bot.load_stock(), ['new3:pw'])
        self.user.send.assert_awaited_once()

    def test_refund_failure_rolls_back_status_and_wallet(self):
        row, _ = self.book()
        balance = self.bot.get_user_balance(42)
        with patch.object(self.bot, 'log_activity', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.bot.refund_preorder(row['id'], 42)
        self.assertEqual(self.bot.get_user_balance(42), balance)
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'waiting')

    def test_cancel_racing_allocation_has_one_financial_outcome(self):
        row, _ = self.book()
        async def cancel():
            async with self.bot.SHOP_TRANSACTION_LOCK:
                return self.bot.refund_preorder(row['id'], 42)
        async def scenario():
            await asyncio.gather(cancel(), self.bot.process_preorders())
        asyncio.run(scenario())
        row = self.bot.STORE.preorder(row['id'])
        if row['status'] == 'cancelled':
            self.assertEqual(self.bot.get_user_balance(42), 10000)
            self.assertEqual(len(self.bot.load_stock()), 2)
            self.assertEqual(self.bot.load_orders(), {})
        else:
            self.assertEqual(row['status'], 'fulfilled')
            self.assertEqual(self.bot.get_user_balance(42), 10000-row['total'])
            self.assertEqual(self.bot.load_stock(), [])

    def test_preorder_panel_button_and_slash_command_show_confirmation(self):
        async def scenario():
            view = self.bot.ShopPanelView('vi')
            button = next(b for b in view.children if b.custom_id == 'lemon:shop:preorder')
            self.assertEqual(button.label, 'Đặt trước')
            await button.callback(self.interaction())
            interaction = self.interaction()
            await self.bot.preorder.callback(interaction, 'default', 2)
            response = interaction.response.send_message.call_args.kwargs
            self.assertIsInstance(response['view'], self.bot.ConfirmPreorderView)
            self.assertTrue(response['ephemeral'])
            self.assertEqual(self.bot.get_user_balance(42), 10000)
        asyncio.run(scenario())

    def test_customers_cannot_cancel_through_either_command(self):
        row, _ = self.book()
        balance = self.bot.get_user_balance(42)
        self.bot.POLICY.config['admin_user_ids'] = [99]
        async def scenario():
            for command in (self.bot.cancelpreorder, self.bot.cancelcustomerpreorder):
                interaction = self.interaction()
                with self.assertRaises(self.bot.app_commands.CheckFailure):
                    for check in command.checks:
                        await check(interaction)
                await command.callback(interaction, row['id'])
                self.assertIn('Only admins', interaction.response.send_message.call_args.args[0])
        asyncio.run(scenario())
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'waiting')
        self.assertEqual(self.bot.get_user_balance(42), balance)

    def test_admin_can_cancel_customer_order_and_refund_customer_once(self):
        row, _ = self.book()
        self.bot.POLICY.config['admin_user_ids'] = [99]
        async def scenario():
            for command in (self.bot.cancelpreorder, self.bot.cancelcustomerpreorder):
                interaction = self.interaction()
                interaction.user = SimpleNamespace(id=99, name='admin')
                for check in command.checks:
                    self.assertTrue(await check(interaction))
                await command.callback(interaction, row['id'])
        asyncio.run(scenario())
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'cancelled')

    def test_admin_cannot_cancel_another_servers_preorder(self):
        row, _ = self.book()
        self.bot.POLICY.config['allowed_guild_ids'].append(456)
        balance = self.bot.get_user_balance(42)
        interaction = self.interaction()
        interaction.guild.id = 456
        asyncio.run(self.bot.cancelpreorder.callback(interaction, row['id']))
        self.assertEqual(self.bot.STORE.preorder(row['id'])['status'], 'waiting')
        self.assertEqual(self.bot.get_user_balance(42), balance)
