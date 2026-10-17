import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_bot_sqlite as baseline


@unittest.skipUnless(importlib.util.find_spec('discord') and importlib.util.find_spec('requests'), 'Install requirements.txt')
class RuntimeBotTests(unittest.TestCase):
    setUp = baseline.BotSQLiteTests.setUp
    tearDown = baseline.BotSQLiteTests.tearDown
    interaction = baseline.BotSQLiteTests.interaction
    upload_interaction = baseline.BotSQLiteTests.upload_interaction
    attachment = baseline.BotSQLiteTests.attachment

    def test_admin_and_customer_callbacks_reject_other_guild(self):
        async def scenario():
            interaction = self.upload_interaction()
            interaction.guild.id = 999
            with self.assertRaises(self.bot.app_commands.CheckFailure):
                for check in self.bot.addstock.checks:
                    await check(interaction)
            self.assertFalse(await self.bot._tree_interaction_check(interaction))
            view = self.bot.ConfirmBuyView(42, 1, 2500)
            await view.children[0].callback(interaction)
            self.assertEqual(self.bot.get_user_balance(42), 10000)
            self.assertEqual(len(self.bot.load_stock()), 2)
            interaction.guild.id = 123
            interaction.user = SimpleNamespace(id=999, name='untrusted')
            with self.assertRaises(self.bot.app_commands.CheckFailure):
                for check in self.bot.addbalance.checks:
                    await check(interaction)
        asyncio.run(scenario())

    def test_failed_dm_is_durable_and_manual_delivery_does_not_charge_twice(self):
        self.bot.notify_admins = AsyncMock()
        async def scenario():
            self.user.send.side_effect = OSError('blocked DM')
            view = self.bot.ConfirmBuyView(42, 1, 2500)
            await view.children[0].callback(self.interaction())
            order_id = next(iter(self.bot.load_orders()))
            self.assertEqual(self.bot.STORE.delivery(order_id)['status'], 'failed')
            self.assertEqual(self.bot.get_user_balance(42), 7500)
            self.assertEqual(len(self.bot.load_stock()), 1)
            # Persisted retry state survives reading on a new SQLite connection.
            from sqlite_store import SQLiteStore
            self.bot.STORE.close()
            self.bot.STORE = SQLiteStore(self.root)
            self.assertEqual(self.bot.STORE.delivery(order_id)['status'], 'failed')
            self.bot.STORE.db.execute('UPDATE deliveries SET next_attempt_at=0 WHERE order_id=?', (order_id,))
            self.user.send.side_effect = None
            await self.bot.redeliver.callback(self.upload_interaction(), order_id)
            self.assertEqual(self.bot.STORE.delivery(order_id)['status'], 'sent')
            self.assertEqual(self.bot.get_user_balance(42), 7500)
            self.assertEqual(len(self.bot.load_stock()), 1)
            self.assertEqual(self.bot.load_orders()[order_id]['delivery_status'], 'sent')
        asyncio.run(scenario())

    def test_download_only_owner_and_retention_keeps_failed_delivery(self):
        self.bot.save_order('LC-OLD', {'user_id': 42, 'accounts': ['test:password'], 'created_at': (datetime.now()-timedelta(days=60)).isoformat(), 'warranty_days': 0, 'delivery_status': 'pending'})
        self.bot.cleanup_expired_orders()
        self.assertIn('LC-OLD', self.bot.load_orders())
        async def scenario():
            unauthorized = self.upload_interaction()
            unauthorized.user = SimpleNamespace(id=99, name='another')
            await self.bot.downloadorder.callback(unauthorized, 'LC-OLD')
            unauthorized.followup.send.assert_not_awaited()
            authorized = self.upload_interaction()
            await self.bot.downloadorder.callback(authorized, 'LC-OLD')
            self.assertTrue(authorized.followup.send.call_args.kwargs['ephemeral'])
            self.assertEqual(self.bot.STORE.delivery('LC-OLD')['status'], 'sent')
        asyncio.run(scenario())

    def test_short_code_credits_after_username_change_exactly_once(self):
        code = self.bot.STORE.get_topup_code(42)
        self.bot.users['42']['username'] = 'renamed-customer'
        self.bot.fetch_vcb_history = lambda: {'transactions': [{
            'transactionId': 'short-code-payment', 'creditAmount': 5000,
            'description': 'NAP ' + code.lower()}]}
        async def scenario():
            await self.bot.process_bank_transactions()
            await self.bot.process_bank_transactions()
        asyncio.run(scenario())
        self.assertEqual(self.bot.get_user_balance(42), 15000)
        self.assertEqual(self.bot.STORE.bank_payment('short-code-payment')['status'], 'credited')
        self.assertEqual(len(self.bot.load_topup_history()), 1)

    def test_pending_bank_transfer_manual_reconciliation_is_idempotent(self):
        self.bot.notify_admins = AsyncMock()
        self.bot.fetch_vcb_history = lambda: {'transactions': [{'transactionId': 'pending-1', 'creditAmount': 5000, 'description': 'LCunknown'}]}
        async def scenario():
            await self.bot.process_bank_transactions()
            await self.bot.process_bank_transactions()
            self.assertEqual(self.bot.get_user_balance(42), 10000)
            self.assertEqual(len(self.bot.STORE.pending_bank_payments()), 1)
            interaction = self.upload_interaction()
            await self.bot.reconciletopup.callback(interaction, 'pending-1', self.user, 'Verified bank receipt')
            await self.bot.reconciletopup.callback(self.upload_interaction(), 'pending-1', self.user, 'Duplicate request')
            self.assertEqual(self.bot.get_user_balance(42), 15000)
            self.assertEqual(self.bot.STORE.bank_payment('pending-1')['status'], 'credited')
            self.assertEqual(len(self.bot.load_topup_history()), 1)
        asyncio.run(scenario())

    def test_failed_reconciliation_rolls_back_claim_and_balance(self):
        self.bot.STORE.observe_bank_payment('rollback-bank', 5000, 'fixture', 'user_not_found')
        original = self.bot.add_topup_history
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('storage failure')
        self.bot.add_topup_history = fail
        with self.assertRaises(OSError):
            self.bot.credit_bank_payment('rollback-bank', 42, 42, 'test')
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertFalse(self.bot.is_transaction_processed('rollback-bank'))
        self.assertEqual(self.bot.STORE.bank_payment('rollback-bank')['status'], 'pending')
        self.assertEqual(self.bot.load_topup_history(), [])

    def test_ignore_and_unstable_bank_id_never_auto_credit(self):
        self.bot.notify_admins = AsyncMock()
        self.bot.fetch_vcb_history = lambda: {'transactions': [{'creditAmount': 5000, 'description': 'LC42'}]}
        async def scenario():
            await self.bot.process_bank_transactions()
            row = self.bot.STORE.pending_bank_payments()[0]
            self.assertEqual(row['reason'], 'no_stable_id')
            await self.bot.ignoretopup.callback(self.upload_interaction(), row['transaction_id'], 'Not a customer payment')
            await self.bot.process_bank_transactions()
            self.assertEqual(self.bot.get_user_balance(42), 10000)
            self.assertEqual(self.bot.STORE.pending_bank_payments(), [])
        asyncio.run(scenario())

    def test_health_alerts_are_throttled_and_recovery_is_reported(self):
        self.bot.notify_admins = AsyncMock()
        async def scenario():
            for _ in range(5):
                await self.bot.service_result('bank', False, 'unavailable')
            self.assertEqual(self.bot.notify_admins.await_count, 1)
            await self.bot.service_result('bank', True)
            self.assertEqual(self.bot.notify_admins.await_count, 2)
            await self.bot.service_result('bank', True)
            self.assertEqual(self.bot.notify_admins.await_count, 2)
        asyncio.run(scenario())

    def test_restore_pause_prevents_bank_and_crypto_auto_credit(self):
        self.bot.notify_admins = AsyncMock()
        self.bot.STORE.db.execute("INSERT INTO metadata VALUES ('payments_paused_after_restore','1')")
        self.bot.fetch_vcb_history = lambda: {'transactions': [{'transactionId':'after-restore', 'creditAmount':5000, 'description':'LC42'}]}
        self.bot.crypto_orders['fixture'] = {'user_id':42, 'trans_id':'restored-crypto'}
        self.bot.get_payfjord_status = lambda _: self.fail('Paused crypto must not poll or credit')
        async def scenario():
            await self.bot.process_bank_transactions()
            await self.bot.process_crypto_orders()
            self.assertEqual(self.bot.get_user_balance(42), 10000)
            self.assertEqual(self.bot.STORE.pending_bank_payments()[0]['reason'], 'restore_paused')
        asyncio.run(scenario())

    def test_native_crypto_claim_blocks_duplicate_after_memory_reload(self):
        self.bot.crypto_orders['first'] = {'user_id':42, 'trans_id':'same-crypto'}
        self.bot.get_payfjord_status = lambda _: ({'status':'completed', 'received':'1'}, None)
        async def scenario():
            await self.bot.process_crypto_orders()
            self.assertEqual(self.bot.get_user_balance(42), 35000)
            self.bot.crypto_orders.clear()
            self.bot.crypto_orders['second'] = {'user_id':42, 'trans_id':'same-crypto'}
            await self.bot.process_crypto_orders()
            self.assertEqual(self.bot.get_user_balance(42), 35000)
            self.assertIs(type(self.bot.users['42']['balance']), int)
        asyncio.run(scenario())

    def test_bootstrap_uses_configured_stock_channel_only(self):
        self.bot.POLICY.config.update(allowed_guild_ids=[], bootstrap_from_stock_channel=True)
        self.bot.bot.get_channel = lambda channel_id: SimpleNamespace(guild=SimpleNamespace(id=123)) if channel_id == self.bot.STOCK_CHANNEL_ID else None
        asyncio.run(self.bot.bootstrap_policy())
        self.assertEqual(self.bot.POLICY.config['allowed_guild_ids'], [123])
        self.assertFalse(self.bot.POLICY.config['bootstrap_from_stock_channel'])

    def test_admin_backup_command_and_help_fit_discord_limits(self):
        async def scenario():
            interaction = self.upload_interaction()
            await self.bot.backup_command.callback(interaction)
            self.assertIn('✅', interaction.followup.send.call_args.args[0])
            self.assertTrue(list((self.root / 'backups/sqlite').glob('bot-*.db')))
            for language in ('vi', 'en'):
                embed = self.bot.build_help_embed(language)
                self.assertLessEqual(len(embed), 6000)
                for field in embed.fields:
                    self.assertLessEqual(len(field.value), 1024)
            definitions = [command.to_dict(self.bot.tree) for command in self.bot.tree.get_commands()]
            self.assertIn('redeliver', [definition['name'] for definition in definitions])
        asyncio.run(scenario())

    def test_legacy_username_needs_review_even_if_name_matches(self):
        self.bot.notify_admins = AsyncMock()
        self.bot.fetch_vcb_history = lambda: {'transactions': [{'transactionId': 'legacy-username', 'creditAmount': 5000, 'description': 'LCbuyer'}]}
        asyncio.run(self.bot.process_bank_transactions())
        self.assertEqual(self.bot.get_user_balance(42), 10000)
        self.assertEqual(self.bot.STORE.bank_payment('legacy-username')['reason'], 'legacy_username')

    def test_crypto_recovers_current_order_reference_after_previous_rollback(self):
        self.bot.crypto_orders.update({
            'a': {'user_id': 42, 'trans_id': 'crypto-a'},
            'b': {'user_id': 42, 'trans_id': 'crypto-b'},
        })
        self.bot.get_payfjord_status = lambda _: ({'status': 'completed', 'received': '1'}, None)
        original = self.bot.add_topup_history
        attempts = 0
        def flaky(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError('injected first-payment storage failure')
            return original(*args, **kwargs)
        self.bot.add_topup_history = flaky
        asyncio.run(self.bot.process_crypto_orders())
        self.assertEqual(self.bot.get_user_balance(42), 35000)
        saved = self.bot.load_json(self.bot.CRYPTO_ORDERS_FILE, {})
        self.assertFalse(saved['a'].get('credited', False))
        self.assertTrue(saved['b']['credited'])

    def test_restore_maintenance_also_blocks_old_purchase_confirmations(self):
        async def scenario():
            view = self.bot.ConfirmBuyView(42, 1, 2500)
            self.bot.STORE.db.execute("INSERT INTO metadata VALUES ('payments_paused_after_restore','1')")
            await view.children[0].callback(self.upload_interaction())
            self.assertEqual(self.bot.get_user_balance(42), 10000)
            self.assertEqual(len(self.bot.load_stock()), 2)
            self.assertEqual(self.bot.load_orders(), {})
        asyncio.run(scenario())

    def test_guild_sync_registers_pendingtopups_without_waiting_for_globals(self):
        async def scenario():
            async def fake_sync(*, guild=None):
                return self.bot.tree.get_commands(guild=guild)
            self.bot.tree.sync = AsyncMock(side_effect=fake_sync)
            await self.bot.sync_application_commands()
            guild = self.bot.discord.Object(id=123)
            names = {command.name for command in self.bot.tree.get_commands(guild=guild)}
            self.assertIn('pendingtopups', names)
            self.assertIn('reconciletopup', names)
            self.assertEqual(self.bot.bot._synced_command_scopes, {123, 'global'})
            self.assertEqual(self.bot.tree.sync.await_count, 2)
            await self.bot.sync_application_commands()
            self.assertEqual(self.bot.tree.sync.await_count, 2)
            self.assertEqual(self.bot.tree.get_commands(guild=self.bot.discord.Object(id=999)), [])
        asyncio.run(scenario())

    def test_failed_guild_sync_is_retried_without_resyncing_successful_global_scope(self):
        async def scenario():
            async def failing(*, guild=None):
                if guild is not None:
                    raise OSError('test failure')
                return self.bot.tree.get_commands()
            self.bot.tree.sync = AsyncMock(side_effect=failing)
            await self.bot.sync_application_commands()
            self.assertEqual(self.bot.bot._synced_command_scopes, {'global'})
            async def recovered(*, guild=None):
                return self.bot.tree.get_commands(guild=guild)
            self.bot.tree.sync.side_effect = recovered
            await self.bot.sync_application_commands()
            self.assertEqual(self.bot.bot._synced_command_scopes, {123, 'global'})
            self.assertEqual(self.bot.tree.sync.await_count, 3)
        asyncio.run(scenario())

    def test_panel_does_not_edit_unchanged_stock_and_coalesces_fast_changes(self):
        from unittest.mock import Mock
        async def scenario():
            clock = [100.0]
            self.bot.build_panel_banner = lambda *args: None
            message = SimpleNamespace(id=555, edit=AsyncMock())
            channel = SimpleNamespace(guild=SimpleNamespace(id=123), fetch_message=AsyncMock(return_value=message), send=AsyncMock())
            self.bot.bot.get_channel = lambda _: channel
            panels = {'1234': {'message_id':555, 'banner':False}}
            with patch.object(self.bot.time, 'monotonic', side_effect=lambda: clock[0]):
                self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'ok')
                self.assertEqual(message.edit.await_count, 1)
                for _ in range(10):
                    clock[0] += 5
                    self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'ok')
                self.assertEqual(message.edit.await_count, 1)
                self.assertEqual(channel.fetch_message.await_count, 1)
                self.bot.save_stock(['changed:account'])
                self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'ok')
                self.assertEqual(message.edit.await_count, 2)
                clock[0] += 1
                self.bot.save_stock(['latest:account', 'another:account', 'third:account'])
                self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'deferred')
                self.assertEqual(message.edit.await_count, 2)
                clock[0] += self.bot.PANEL_MIN_EDIT_SECONDS
                self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'ok')
                self.assertEqual(message.edit.await_count, 3)
                clock[0] += self.bot.PANEL_VERIFY_SECONDS + 1
                self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'ok')
                self.assertEqual(message.edit.await_count, 3)
        asyncio.run(scenario())

    def test_panel_error_backs_off_instead_of_sending_every_poll(self):
        async def scenario():
            self.bot.build_panel_banner = lambda *args: None
            message = SimpleNamespace(id=555, edit=AsyncMock(side_effect=OSError('fixture failure')))
            channel = SimpleNamespace(guild=SimpleNamespace(id=123), fetch_message=AsyncMock(return_value=message))
            self.bot.bot.get_channel = lambda _: channel
            panels = {'1234': {'message_id':555, 'banner':False}}
            self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'error')
            self.assertEqual(await self.bot.update_stock_panel(1234, panels), 'deferred')
            self.assertEqual(message.edit.await_count, 1)
            self.assertEqual(channel.fetch_message.await_count, 1)
        asyncio.run(scenario())

    def test_global_commands_are_dm_only_and_local_dispatch_is_preserved(self):
        async def scenario():
            published = {}
            async def fake_sync(*, guild=None):
                commands = self.bot.tree.get_commands(guild=guild)
                published[guild.id if guild else 'global'] = [command.to_dict(self.bot.tree) for command in commands]
                return commands
            self.bot.tree.sync = AsyncMock(side_effect=fake_sync)
            await self.bot.sync_application_commands()
            guild_names = {command['name'] for command in published[123]}
            self.assertIn('pendingtopups', guild_names)
            global_names = {command['name'] for command in published['global']}
            self.assertNotIn('pendingtopups', global_names)
            self.assertIn('redeliver', global_names)
            for command in published['global']:
                self.assertEqual(command['contexts'], [1])
            self.assertIsNotNone(self.bot.tree.get_command('pendingtopups'))
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
