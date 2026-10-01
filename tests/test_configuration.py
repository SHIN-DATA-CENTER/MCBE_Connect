import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import configure
from world_nbt import dump, load


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = Path(self.temp.name) / 'bds'
        self.world = self.server / 'worlds/Existing World'
        self.world.mkdir(parents=True)
        (self.server / 'bedrock_server').write_text('not executed by these tests')
        self.props = self.server / 'server.properties'
        self.props.write_text('online-mode=true\nlevel-name=Existing World\nserver-port=19100\n'
                              'allow-list=true\nallow-player-joining=true\n'
                              'content-log-console-output-enabled=false\n')
        self.raw = dump((10, '', {'RandomSeed': (4, 123456), 'LevelName': (8, 'Existing World'),
                                 'experiments': (10, {'other_experiment': (1, 1)})}))
        (self.world / 'level.dat').write_bytes(self.raw)
        (self.world / 'world_behavior_packs.json').write_text(json.dumps([{'pack_id': 'other-pack', 'version': [1, 0, 0]}]))
        self.config = Path(self.temp.name) / 'auth-config.json'
        self.settings = {'bot_token': 'test-not-a-token', 'guild_id': '123', 'channel_id': '789',
                         'subscriber_role_ids': ['456'], 'api_port': 28080,
                         'api_secret': 'not-a-live-secret-' + 'x' * 40}
        self.config.write_text(json.dumps(self.settings))

    def enable(self, mode='enable'):
        return configure.configure(self.server, self.config, mode, '1.26.52.3')

    def test_enable_preserves_world_other_packs_and_network(self):
        backup = self.enable()
        props = self.props.read_text()
        self.assertIn('server-port=19100\n', props)
        self.assertIn('allow-list=false\n', props)
        self.assertIn('allow-player-joining=false\n', props)
        self.assertIn('content-log-console-output-enabled=true\n', props)
        level = load((self.world / 'level.dat').read_bytes())[2]
        self.assertEqual(level['RandomSeed'], (4, 123456))
        self.assertEqual(level['experiments'][1]['other_experiment'], (1, 1))
        self.assertEqual(level['experiments'][1]['gametest'], (1, 1))
        packs = json.loads((self.world / 'world_behavior_packs.json').read_text())
        self.assertEqual(packs[0]['pack_id'], 'other-pack')
        self.assertEqual(packs[1]['pack_id'], configure.HEADER_UUID)
        self.assertEqual((backup / 'worlds/Existing World/level.dat').read_bytes(), self.raw)
        module = self.server / 'config' / configure.MODULE_UUID
        variables = json.loads((module / 'variables.json').read_text())
        self.assertEqual(variables['api_url'], 'http://127.0.0.1:28080')
        permissions = json.loads((module / 'permissions.json').read_text())
        net = permissions['module_permissions']['@minecraft/server-net']
        self.assertEqual(net['allowed_uris'], ['http://127.0.0.1:28080/v1/authorize', 'http://127.0.0.1:28080/v1/online'])
        self.assertNotIn('force_tls', net)
        self.assertEqual((module / 'secrets.json').stat().st_mode & 0o777, 0o600)

    def test_refresh_keeps_admission_flags_and_does_not_duplicate_packs(self):
        self.enable()
        level = (self.world / 'level.dat').read_bytes()
        self.enable('refresh')
        self.assertEqual(level, (self.world / 'level.dat').read_bytes())
        packs = json.loads((self.world / 'world_behavior_packs.json').read_text())
        self.assertEqual(sum(item['pack_id'] == configure.HEADER_UUID for item in packs), 1)

    def test_init_generates_private_secret_and_does_not_overwrite(self):
        path = Path(self.temp.name) / 'new/config.json'
        configure.init_config(path)
        self.assertGreaterEqual(len(json.loads(path.read_text())['api_secret']), 32)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            configure.init_config(path)

    def test_offline_identity_and_world_escape_rejected_before_write(self):
        for props in ['online-mode=false\nlevel-name=Existing World\n',
                      'online-mode=true\nlevel-name=../../elsewhere\n']:
            self.props.write_text(props)
            with self.assertRaises(ValueError):
                self.enable()
            self.assertEqual((self.world / 'level.dat').read_bytes(), self.raw)
            self.assertFalse((self.server / 'behavior_packs').exists())

    def test_unknown_or_mismatched_bds_version_rejected(self):
        with self.assertRaises(ValueError):
            configure.configure(self.server, self.config, 'enable', '0.0.0.0')
        (self.server / '.version').write_text('0.0.0.0')
        with self.assertRaises(ValueError):
            self.enable()

    def test_symlinked_module_destination_rejected(self):
        external = Path(self.temp.name) / 'external-config'
        external.mkdir()
        (self.server / 'config').symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.enable()
        self.assertEqual(list(external.iterdir()), [])

    def test_invalid_ports_and_empty_roles_rejected(self):
        for value in (0, 65536, '28080', True):
            self.settings['api_port'] = value
            self.config.write_text(json.dumps(self.settings))
            with self.assertRaises(ValueError):
                self.enable()
        self.settings['api_port'] = 28080
        self.settings['subscriber_role_ids'] = []
        self.config.write_text(json.dumps(self.settings))
        with self.assertRaises(ValueError):
            self.enable()

    def test_partial_install_failure_restores_previous_files(self):
        original = self.props.read_bytes()
        with patch.object(configure, 'write_json', side_effect=OSError('simulated write failure')):
            with self.assertRaises(OSError):
                self.enable()
        self.assertEqual(self.props.read_bytes(), original)
        self.assertEqual((self.world / 'level.dat').read_bytes(), self.raw)
        self.assertFalse((self.server / 'behavior_packs/mochi_auth').exists())
        self.assertFalse((self.server / 'config' / configure.MODULE_UUID).exists())


if __name__ == '__main__':
    unittest.main()
