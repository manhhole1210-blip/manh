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


if __name__ == '__main__':
    unittest.main()
