import asyncio
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from store import Store, LinkError
from service import AuthService, Bot, Unavailable
from aiohttp.test_utils import TestClient, TestServer

CONFIG = {'guild_id': '123', 'subscriber_role_ids': ['456'], 'channel_id': '789',
          'bot_token': 'placeholder-not-a-real-token', 'invite_url': 'https://example.invalid/',
          'api_secret': 'test-secret-' + 'x' * 40}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.time = 1000
        self.path = Path(self.temp.name) / 'auth.sqlite3'
        self.store = Store(self.path, lambda: self.time)
        self.store.configure_policy('one')
        self.store.player('p1', 'Player One')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.close())

    def test_link_requires_role_and_is_one_time(self):
        code = self.store.challenge('p1')
        with self.assertRaises(LinkError):
            self.store.claim('d1', code, False)
        row = self.store.claim('d1', code, True)
        self.assertEqual(row['decision'], 'allowed')
        with self.assertRaises(LinkError):
            self.store.claim('d2', code, True)

    def test_expired_and_replaced_codes_are_rejected(self):
        old = self.store.challenge('p1')
        fresh = self.store.challenge('p1')
        with self.assertRaises(LinkError):
            self.store.claim('d1', old, True)
        self.time += 600
        with self.assertRaises(LinkError):
            self.store.claim('d1', fresh, True)

    def test_four_digit_code_preserves_leading_zero(self):
        with patch('store.secrets.randbelow', return_value=7):
            code = self.store.challenge('p1')
        self.assertEqual(code, '0007')
        self.assertEqual(self.store.claim('d1', ' 0007 ', True)['pid'], 'p1')

    def test_colliding_codes_do_not_cross_link_players(self):
        self.store.player('p2', 'Player Two')
        with patch('store.secrets.randbelow', side_effect=[1234, 1234, 1235]):
            first = self.store.challenge('p1')
            second = self.store.challenge('p2')
        self.assertEqual(self.store.claim('d2', second, True)['pid'], 'p2')
        self.assertEqual(self.store.claim('d1', first, True)['pid'], 'p1')

    def test_replaced_and_consumed_codes_are_not_reassigned_during_lifetime(self):
        self.store.player('p2', 'Player Two')
        with patch('store.secrets.randbelow', side_effect=[1234, 1234, 1235, 1235, 1236]):
            old = self.store.challenge('p1')
            fresh = self.store.challenge('p1')
            self.store.claim('d1', fresh, True)
            other = self.store.challenge('p2')
        for code in (old, fresh):
            with self.assertRaises(LinkError):
                self.store.claim('d2', code, True)
        self.assertEqual(self.store.claim('d2', other, True)['pid'], 'p2')

    def test_only_four_ascii_digits_can_be_redeemed(self):
        with patch('store.secrets.randbelow', return_value=7):
            self.store.challenge('p1')
        for code in ('ABCD2345', '１２３４', '123', '12345'):
            with self.assertRaisesRegex(LinkError, '半角数字4桁'):
                self.store.claim('d1', code, True)
        self.assertEqual(self.store.claim('d1', '0007', True)['pid'], 'p1')

    def test_legacy_codes_are_invalidated_without_losing_existing_links(self):
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.store.player('p2', 'Player Two')
        with self.store.db:
            self.store.db.execute('DELETE FROM metadata WHERE key=?', ('code_format',))
            self.store.db.execute('INSERT INTO codes VALUES(?,?,?)', (hashlib.sha256(b'ABCD2345').hexdigest(), 'p2', self.time + 600))
        self.store.close()
        self.store = Store(self.path, lambda: self.time)
        self.assertEqual(self.store.user('d1')['decision'], 'allowed')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM codes').fetchone()[0], 0)

    def test_account_ownership_is_one_to_one(self):
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.store.player('p2', 'Player Two')
        with self.assertRaises(LinkError):
            self.store.claim('d1', self.store.challenge('p2'), True)
        with self.assertRaises(LinkError):
            self.store.claim('d2', self.store.challenge('p1'), True)

    def test_unlink_persists_tombstone_and_allows_relink(self):
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.assertTrue(self.store.unlink('d1'))
        self.assertEqual(self.store.player('p1')['decision'], 'denied')
        self.assertIsNone(self.store.user('d1'))
        self.store.close()
        self.store = Store(self.path, lambda: self.time)
        self.assertEqual(self.store.player('p1')['decision'], 'denied')
        self.store.claim('d2', self.store.challenge('p1'), True)

    def test_role_revocation_and_resubscription(self):
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.store.decide('d1', False)
        denied_revision = self.store.player('p1')['revision']
        self.store.decide('d1', True)
        self.assertEqual(self.store.player('p1')['decision'], 'allowed')
        self.assertGreater(self.store.player('p1')['revision'], denied_revision)

    def test_bruteforce_limit_survives_restart(self):
        for _ in range(5):
            with self.assertRaises(LinkError):
                self.store.claim('d1', 'INVALID', True)
        self.store.close()
        self.store = Store(self.path, lambda: self.time)
        with self.assertRaisesRegex(LinkError, '上限'):
            self.store.claim('d1', self.store.challenge('p1'), True)

    def test_policy_change_invalidates_saved_authorizations(self):
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.store.configure_policy('different-guild')
        self.assertEqual(self.store.player('p1')['decision'], 'unknown')


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.service = AuthService(dict(CONFIG), self.store)
        self.client = TestClient(TestServer(self.service.app()))
        await self.client.start_server()
        self.headers = {'Authorization': 'Bearer ' + CONFIG['api_secret']}

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()

    async def post(self, path, data, headers=None):
        return await self.client.post(path, json=data, headers=self.headers if headers is None else headers)

    async def test_api_is_authenticated_and_rejects_invalid_identity(self):
        response = await self.post('/v1/authorize', {'pid':'p1','name':'Player'}, {})
        self.assertEqual(response.status, 401)
        response = await self.post('/v1/authorize', {'pid':'','name':'Player'})
        self.assertEqual(response.status, 400)
        response = await self.post('/v1/online', {'players': [{}] * 101})
        self.assertEqual(response.status, 400)

    async def test_code_shown_when_bot_is_unavailable(self):
        response = await self.post('/v1/authorize', {'pid':'p1','name':'Player'})
        data = await response.json()
        self.assertEqual(data['decision'], 'unlinked')
        self.assertRegex(data['code'], r'^[0-9]{4}$')
        self.assertIn(data['code'], data['message'])
        self.assertNotIn(CONFIG['invite_url'], data['message'])
        self.assertNotIn('http', data['message'])

    async def test_indefinite_cache_and_explicit_denial(self):
        self.store.player('p1', 'Player')
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.service.member_allowed = AsyncMock(side_effect=Unavailable())
        self.store.db.execute('UPDATE accounts SET checked_at=0')
        data = await self.service.authorize('p1', 'Renamed')
        self.assertEqual(data['decision'], 'allowed')
        self.assertTrue(data['offline'])
        self.store.decide('d1', False)
        data = await self.service.authorize('p1', 'Renamed')
        self.assertEqual(data['decision'], 'denied')
        self.assertEqual(self.store.user('d1')['name'], 'Renamed')

    async def test_online_batch_revokes_unlinked_user(self):
        self.store.player('p1', 'Player')
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.store.unlink('d1')
        response = await self.post('/v1/online', {'players':[{'pid':'p1','name':'Player'}]})
        self.assertEqual((await response.json())['players'][0]['decision'], 'denied')

    async def test_recovery_refreshes_expired_positive_decision(self):
        self.store.player('p1', 'Player')
        self.store.claim('d1', self.store.challenge('p1'), True)
        self.store.db.execute('UPDATE accounts SET checked_at=0')
        self.service.member_allowed = AsyncMock(return_value=False)
        result = await self.service.authorize('p1', 'Player')
        self.assertEqual(result['decision'], 'denied')
        self.assertFalse(result['offline'])

    async def test_discord_commands_are_constructed_and_channel_restricted(self):
        bot = Bot(self.service)
        try:
            self.assertEqual({c.name for c in bot.group.commands}, {'link','status','unlink'})
            interaction = SimpleNamespace(guild_id=123, channel_id=999,
                                          response=SimpleNamespace(send_message=AsyncMock()))
            self.assertFalse(await bot.guard(interaction))
            interaction.response.send_message.assert_awaited_once()
        finally:
            await bot.close()

    async def test_member_event_revokes_permission_without_rest_delay(self):
        self.store.player('p1', 'Player')
        self.store.claim('42', self.store.challenge('p1'), True)
        bot = Bot(self.service)
        try:
            await bot.on_member_update(None, SimpleNamespace(id=42,guild=SimpleNamespace(id=123),roles=[]))
            self.assertEqual(self.store.player('p1')['decision'], 'denied')
            await bot.on_member_update(None, SimpleNamespace(id=42,guild=SimpleNamespace(id=123),roles=[SimpleNamespace(id=456)]))
            self.assertEqual(self.store.player('p1')['decision'], 'allowed')
            await bot.on_raw_member_remove(SimpleNamespace(guild_id=123,user=SimpleNamespace(id=42)))
            self.assertEqual(self.store.player('p1')['decision'], 'denied')
        finally:
            await bot.close()

    async def test_role_events_for_uncached_members_are_applied(self):
        self.store.player('p1', 'Player')
        self.store.claim('42', self.store.challenge('p1'), True)
        bot = Bot(self.service)
        try:
            await bot.on_socket_response({'t':'GUILD_MEMBER_UPDATE','d':{'guild_id':'123','user':{'id':'42'},'roles':[]}})
            self.assertEqual(self.store.player('p1')['decision'], 'denied')
        finally:
            await bot.close()

    async def test_revocation_during_rest_request_wins(self):
        self.store.player('p1', 'Player')
        self.store.claim('42', self.store.challenge('p1'), True)
        async def stale_positive(_):
            self.store.decide('42', False)
            return True
        self.service.member_allowed = stale_positive
        await self.service.refresh('42')
        self.assertEqual(self.store.player('p1')['decision'], 'denied')


if __name__ == '__main__':
    unittest.main()
