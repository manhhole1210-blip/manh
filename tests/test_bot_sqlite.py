"""Exercise real bot callbacks using local Discord/payment doubles; no network."""
import asyncio
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


@unittest.skipUnless(importlib.util.find_spec('discord') and importlib.util.find_spec('requests'), 'Install requirements.txt for bot integration tests')
class BotSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copy2(Path(__file__).resolve().parents[1] / 'main.py', self.root / 'main.py')
        spec = importlib.util.spec_from_file_location('test_bot_instance', self.root / 'main.py')
        self.bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bot)
        self.user = SimpleNamespace(id=42, name='buyer', display_name='Buyer', mention='<@42>', send=AsyncMock())
        self.bot.ensure_user(self.user)
        self.bot.users['42']['balance'] = 10000
        self.bot.save_json(self.bot.USERS_FILE, self.bot.users)
        self.bot.save_stock(['account1:pass1', 'account2:pass2'])
        self.bot.update_stock_message = AsyncMock()
        self.bot.build_purchase_success_image = lambda *a, **kw: None
        self.bot.bot.fetch_user = AsyncMock(return_value=self.user)

    def tearDown(self):
        self.bot.STORE.close()
        self.bot._PROCESS_LOCK.close()
        self.temp.cleanup()

    def interaction(self):
        return SimpleNamespace(user=self.user,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

    def test_purchase_callback_debits_once_and_saves_delivery(self):
        async def scenario():
            view = self.bot.ConfirmBuyView(42, 1, 2500)
            interaction = self.interaction()
            await view.children[0].callback(interaction)
            await view.children[0].callback(interaction)
            self.assertEqual(self.bot.get_user_balance(42), 7500)
            self.assertEqual(self.bot.load_stock(), ['account2:pass2'])
            orders = self.bot.load_orders()
            self.assertEqual(len(orders), 1)
            self.assertEqual(next(iter(orders.values()))['accounts'], ['account1:pass1'])
            self.assertEqual(self.user.send.await_count, 1)
            self.assertEqual(self.bot.STORE.db.execute('SELECT count(*) FROM orders').fetchone()[0], 1)
        asyncio.run(scenario())

    def test_purchase_storage_failure_rolls_back_memory_and_database(self):
        def fail(*args, **kwargs):
            raise OSError('injected order write failure')
        self.bot.save_order = fail
        async def scenario():
            view = self.bot.ConfirmBuyView(42, 1, 2500)
            with self.assertRaises(OSError):
                await view.children[0].callback(self.interaction())
        asyncio.run(scenario())
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertEqual(self.bot.load_json(self.bot.USERS_FILE, {})['42']['balance'], 10000)
        self.assertEqual(len(self.bot.load_stock()), 2)
        self.assertEqual(self.bot.load_orders(), {})
        self.user.send.assert_not_awaited()

    def test_bank_payment_is_not_credited_twice(self):
        self.bot.fetch_vcb_history = lambda: {'transactions': [
            {'transactionId': 'bank-fixture-1', 'creditAmount': 5000, 'description': 'LC42'}]}
        async def scenario():
            await self.bot.process_bank_transactions()
            await self.bot.process_bank_transactions()
        asyncio.run(scenario())
        self.assertEqual(self.bot.get_user_balance(42), 15000)
        self.assertTrue(self.bot.is_transaction_processed('bank-fixture-1'))
        self.assertEqual(len(self.bot.load_topup_history()), 1)
        # Reload the state from SQLite, as a new process would do.
        self.bot.users = self.bot.load_json(self.bot.USERS_FILE, {})
        self.bot.transactions = self.bot.load_json(self.bot.TRANSACTIONS_FILE, [])
        asyncio.run(self.bot.process_bank_transactions())
        self.assertEqual(self.bot.get_user_balance(42), 15000)

    def test_crypto_payment_is_not_credited_twice(self):
        self.bot.crypto_orders['crypto-fixture'] = {
            'user_id': 42, 'username': 'buyer', 'trans_id': 'crypto-tx-1', 'credited': False}
        self.bot.get_payfjord_status = lambda trans_id: ({'status': 'completed', 'received': '2'}, None)
        async def scenario():
            await self.bot.process_crypto_orders()
            await self.bot.process_crypto_orders()
        asyncio.run(scenario())
        self.assertEqual(self.bot.get_user_balance(42), 60000)
        self.assertTrue(self.bot.load_json(self.bot.CRYPTO_ORDERS_FILE, {})['crypto-fixture']['credited'])
        self.assertEqual(len(self.bot.load_topup_history()), 1)

    def test_product_removal_and_ticket_persistence(self):
        status, product, _ = self.bot.add_or_update_product('Test product', 2000, 3)
        self.assertEqual(status, 'added')
        self.bot.save_product_stock(product, ['extra:password'])
        status, _ = self.bot.remove_product(product['id'])
        self.assertEqual(status, 'stock_confirmation_required')
        _, _, digest = self.bot.product_stock_snapshot(product)
        status, _ = self.bot.remove_product(product['id'], digest)
        self.assertEqual(status, 'removed')
        self.assertEqual(self.bot.load_product_stock(product), [])
        self.bot.ticket_save_json(self.bot.TICKETS_FILE, {'guild': {'ticket': {'user_id': 42}}})
        self.assertEqual(self.bot.ticket_load_json(self.bot.TICKETS_FILE, {})['guild']['ticket']['user_id'], 42)
        self.assertFalse(Path(self.bot.TICKETS_FILE).exists())

    def upload_interaction(self):
        interaction = self.interaction()
        interaction.response.is_done = lambda: interaction.response.defer.await_count > 0
        interaction.guild = SimpleNamespace(id=123)
        interaction.permissions = SimpleNamespace(administrator=True)
        return interaction

    def attachment(self, content, filename='accounts.txt'):
        return SimpleNamespace(filename=filename, size=len(content), read=AsyncMock(return_value=content))

    def test_addstock_default_bom_blank_lines_and_duplicates(self):
        async def scenario():
            interaction = self.upload_interaction()
            file = self.attachment(b'\xef\xbb\xbfaccount3:pass3\r\n\r\n account1:pass1 \naccount3:pass3\naccount4:pass4\n')
            await self.bot.addstock.callback(interaction, 'default', file)
            self.assertEqual(self.bot.load_stock(), ['account1:pass1', 'account2:pass2', 'account3:pass3', 'account4:pass4'])
            embed = interaction.followup.send.call_args.kwargs['embed']
            self.assertEqual([field.value for field in embed.fields], ['2', '2', '4'])
            self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
            self.bot.update_stock_message.assert_awaited_once()
            logs = str(self.bot.load_activity_logs())
            self.assertIn('admin_add_stock', logs)
            self.assertNotIn('pass3', logs)
        asyncio.run(scenario())

    def test_addstock_extra_product_and_autocomplete(self):
        _, product, _ = self.bot.add_or_update_product('Extra product', 2000, 3)
        async def scenario():
            interaction = self.upload_interaction()
            choices = await self.bot.addstock_product_autocomplete(interaction, 'extra')
            self.assertEqual([choice.value for choice in choices], [product['id']])
            await self.bot.addstock.callback(interaction, product['id'], self.attachment(b'extra:password\n'))
            self.assertEqual(self.bot.load_product_stock(product), ['extra:password'])
            self.assertEqual(len(self.bot.load_stock()), 2)
            interaction.permissions.administrator = False
            self.assertEqual(await self.bot.addstock_product_autocomplete(interaction, ''), [])
            self.assertTrue(self.bot.addstock.guild_only)
            with self.assertRaises(self.bot.app_commands.MissingPermissions):
                for check in self.bot.addstock.checks:
                    await check(interaction)
        asyncio.run(scenario())

    def test_addstock_rejects_bad_files_without_changing_stock(self):
        async def scenario():
            cases = [
                self.attachment(b'abc', 'accounts.csv'),
                self.attachment(b'\xff\xfe'),
                self.attachment(b'a\0b'),
                self.attachment(b' \n\t'),
                self.attachment(b'a\n' * (self.bot.MAX_STOCK_UPLOAD_LINES + 1)),
            ]
            for file in cases:
                with self.subTest(filename=file.filename, size=file.size):
                    interaction = self.upload_interaction()
                    await self.bot.addstock.callback(interaction, 'default', file)
                    self.assertEqual(len(self.bot.load_stock()), 2)
                    messages = interaction.response.send_message.call_args_list + interaction.followup.send.call_args_list
                    self.assertTrue(messages)
                    self.assertTrue(all(message.kwargs['ephemeral'] for message in messages))
            unknown = self.attachment(b'a')
            await self.bot.addstock.callback(self.upload_interaction(), 'missing', unknown)
            unknown.read.assert_not_awaited()
        asyncio.run(scenario())

    def test_addstock_accepts_file_larger_than_former_5mb_limit(self):
        async def scenario():
            content = b'a' * (5 * 1024 * 1024 + 1)
            file = self.attachment(content)
            interaction = self.upload_interaction()
            await self.bot.addstock.callback(interaction, 'default', file)
            stock = self.bot.load_stock()
            self.assertEqual(len(stock), 3)
            self.assertEqual(stock[-1].encode('utf-8'), content)
            self.assertEqual(interaction.followup.send.call_args.kwargs['embed'].fields[0].value, '1')
        asyncio.run(scenario())

    def test_addstock_concurrent_uploads_do_not_duplicate_accounts(self):
        async def scenario():
            async def download():
                await asyncio.sleep(0)
                return b'new:account\n'
            files = [self.attachment(b'new:account\n') for _ in range(2)]
            for file in files:
                file.read.side_effect = download
            interactions = [self.upload_interaction(), self.upload_interaction()]
            await asyncio.gather(*(self.bot.addstock.callback(interaction, 'default', file)
                                   for interaction, file in zip(interactions, files)))
            self.assertEqual(self.bot.load_stock(), ['account1:pass1', 'account2:pass2', 'new:account'])
            counts = [interaction.followup.send.call_args.kwargs['embed'].fields[0].value for interaction in interactions]
            self.assertEqual(sorted(counts), ['0', '1'])
        asyncio.run(scenario())

    def test_addstock_detects_product_removed_during_download(self):
        _, product, _ = self.bot.add_or_update_product('Removed product', 2000, 3)
        async def scenario():
            async def download():
                self.bot.remove_product(product['id'])
                return b'new:account\n'
            file = self.attachment(b'new:account\n')
            file.read.side_effect = download
            interaction = self.upload_interaction()
            await self.bot.addstock.callback(interaction, product['id'], file)
            self.assertIsNone(self.bot.get_product_by_id(product['id']))
            self.assertEqual(self.bot.load_product_stock(product), [])
            self.assertIn('❌', interaction.followup.send.call_args.args[0])
        asyncio.run(scenario())

    def test_addstock_storage_failure_rolls_back(self):
        original = self.bot.save_product_stock
        def fail(product, stock):
            original(product, stock)
            raise OSError('injected stock write failure')
        self.bot.save_product_stock = fail
        async def scenario():
            with self.assertRaises(OSError):
                await self.bot.addstock.callback(self.upload_interaction(), 'default', self.attachment(b'new:account\n'))
            self.assertEqual(self.bot.load_stock(), ['account1:pass1', 'account2:pass2'])
            self.bot.update_stock_message.assert_not_awaited()
        asyncio.run(scenario())

    def test_removestock_removes_tail_and_preserves_financial_state(self):
        async def scenario():
            interaction = self.upload_interaction()
            await self.bot.removestock.callback(interaction, 'default', 1)
            self.assertEqual(self.bot.load_stock(), ['account1:pass1'])
            self.assertEqual(self.bot.get_user_balance(42), 10000)
            self.assertEqual(self.bot.load_orders(), {})
            fields = interaction.followup.send.call_args.kwargs['embed'].fields
            self.assertEqual([field.value for field in fields], ['1', '1'])
            self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
            self.bot.update_stock_message.assert_awaited_once()
            logs = str(self.bot.load_activity_logs())
            self.assertIn('admin_remove_stock', logs)
            self.assertNotIn('pass2', logs)
        asyncio.run(scenario())

    def test_removestock_extra_product_clear_and_permissions(self):
        _, product, _ = self.bot.add_or_update_product('Extra stock', 2000, 3)
        self.bot.save_product_stock(product, ['x:pass', 'y:pass'])
        async def scenario():
            interaction = self.upload_interaction()
            choices = await self.bot.removestock_product_autocomplete(interaction, 'extra')
            self.assertEqual([choice.value for choice in choices], [product['id']])
            await self.bot.removestock.callback(interaction, product['id'], 2)
            self.assertEqual(self.bot.load_product_stock(product), [])
            self.assertEqual(len(self.bot.load_stock()), 2)
            self.assertIsNotNone(self.bot.get_product_by_id(product['id']))
            self.assertTrue(self.bot.removestock.guild_only)
            interaction.permissions.administrator = False
            self.assertEqual(await self.bot.removestock_product_autocomplete(interaction, ''), [])
            with self.assertRaises(self.bot.app_commands.MissingPermissions):
                for check in self.bot.removestock.checks:
                    await check(interaction)
        asyncio.run(scenario())

    def test_removestock_rejects_invalid_quantities_and_product(self):
        async def scenario():
            for product, quantity in [('default', 0), ('default', -1), ('default', 3), ('missing', 1)]:
                interaction = self.upload_interaction()
                await self.bot.removestock.callback(interaction, product, quantity)
                self.assertEqual(self.bot.load_stock(), ['account1:pass1', 'account2:pass2'])
            self.bot.save_stock([])
            await self.bot.removestock.callback(self.upload_interaction(), 'default', 1)
            self.assertEqual(self.bot.load_stock(), [])
            self.bot.update_stock_message.assert_not_awaited()
        asyncio.run(scenario())

    def test_removestock_concurrent_requests_cannot_overdraw_stock(self):
        async def scenario():
            interactions = [self.upload_interaction(), self.upload_interaction()]
            await asyncio.gather(*(self.bot.removestock.callback(interaction, 'default', 2) for interaction in interactions))
            self.assertEqual(self.bot.load_stock(), [])
            successes = sum('embed' in interaction.followup.send.call_args.kwargs for interaction in interactions)
            self.assertEqual(successes, 1)
        asyncio.run(scenario())

    def test_removestock_storage_failure_rolls_back(self):
        original = self.bot.save_product_stock
        def fail(product, stock):
            original(product, stock)
            raise OSError('injected deletion write failure')
        self.bot.save_product_stock = fail
        async def scenario():
            interaction = self.upload_interaction()
            with self.assertRaises(OSError):
                await self.bot.removestock.callback(interaction, 'default', 1)
            self.assertEqual(self.bot.load_stock(), ['account1:pass1', 'account2:pass2'])
            interaction.followup.send.assert_not_awaited()
            self.bot.update_stock_message.assert_not_awaited()
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
