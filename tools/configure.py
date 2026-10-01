"""Configure an explicitly selected, stopped Linux BDS installation."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import struct

from world_nbt import load, dump

ROOT = Path(__file__).resolve().parents[1]
HEADER_UUID = '941cdab3-8bc0-4de5-914f-a39b92a67ea6'
MODULE_UUID = '9fe36aba-3f5d-4cd0-b2fd-c281a47a5ad1'
SUPPORTED_BDS = '1.26.52.3'


def write_json(path, data, secret=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    # Create private credential files with 0600 from their first write.
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    tmp.chmod(0o600 if secret else 0o644)
    tmp.replace(path)


def init_config(path):
    if path.exists():
        raise ValueError('Config already exists; refusing to overwrite credentials')
    config = json.loads((ROOT / 'discord-auth/config.example.json').read_text())
    config['api_secret'] = secrets.token_urlsafe(40)
    write_json(path, config, secret=True)


def read_config(path):
    config = json.loads(path.read_text())
    for key in ('guild_id', 'channel_id'):
        value = str(config[key])
        if not value.isascii() or not value.isdecimal():
            raise ValueError(key + ' must be a numeric Discord ID')
        config[key] = value
    roles = config['subscriber_role_ids']
    if not isinstance(roles, list) or not roles:
        raise ValueError('subscriber_role_ids must be a nonempty list')
    config['subscriber_role_ids'] = [str(role) for role in roles]
    if any(not role.isascii() or not role.isdecimal() for role in config['subscriber_role_ids']):
        raise ValueError('subscriber_role_ids must contain numeric Discord IDs')
    key = config['api_secret']
    if not isinstance(key, str) or len(key) < 32 or key.startswith('GENERATED_') or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('Generate a new api_secret with the init command')
    if not config.get('bot_token'):
        raise ValueError('bot_token is required')
    port = config.get('api_port', 18080)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('api_port must be an integer from 1 to 65535')
    config['api_port'] = port
    return config


def world_path(server):
    props = dict(line.split('=', 1) for line in (server / 'server.properties').read_text().splitlines()
                 if '=' in line and not line.lstrip().startswith('#'))
    if props.get('online-mode', 'true').strip() != 'true':
        raise ValueError('online-mode=true is required for authenticated player identities')
    worlds = (server / 'worlds').resolve()
    world = (worlds / props.get('level-name', 'Bedrock level').strip()).resolve()
    if world == worlds or not world.is_relative_to(worlds):
        raise ValueError('level-name must resolve inside server/worlds')
    if not (world / 'level.dat').is_file():
        raise ValueError('Start BDS once to create the world, then stop it before configuring')
    return world


def require_stopped(server):
    proc = Path('/proc')
    if not proc.is_dir():
        raise ValueError('This installation helper currently supports Linux only')
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            exe = (entry / 'exe').resolve(strict=True)
            cwd = (entry / 'cwd').resolve(strict=True)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if exe.name == 'bedrock_server' and (exe.parent == server or cwd == server):
            raise ValueError('The selected BDS is running; stop it before configuring')


def configure(server, config_path, mode, bds_version):
    server = server.resolve(strict=True)
    if not (server / 'bedrock_server').is_file():
        raise ValueError('--server-dir must contain the official bedrock_server executable')
    if bds_version != SUPPORTED_BDS:
        raise ValueError('This release is validated only with BDS ' + SUPPORTED_BDS)
    if (server / '.version').exists() and (server / '.version').read_text().strip() != bds_version:
        raise ValueError('The selected server version differs from --bds-version')
    require_stopped(server)
    config = read_config(config_path)
    world = world_path(server)
    raw = (world / 'level.dat').read_bytes()
    root = load(raw)
    version = struct.unpack_from('<I', raw)[0]
    if dump(root, version) != raw or root[0] != 10:
        raise ValueError('Unsupported level.dat format; nothing has been modified')
    experiments = root[2].get('experiments', (10, {}))
    if experiments[0] != 10:
        raise ValueError('Unsupported experiments tag')
    packs_path = world / 'world_behavior_packs.json'
    packs = json.loads(packs_path.read_text()) if packs_path.exists() else []
    if not isinstance(packs, list) or any(not isinstance(item, dict) for item in packs):
        raise ValueError('Invalid world_behavior_packs.json')
    properties = server / 'server.properties'
    lines = properties.read_text().splitlines()
    active = mode == 'enable' or any(line.strip() == 'allow-player-joining=false' for line in lines)
    if mode == 'refresh' and not active:
        raise ValueError('Use enable for the first installation')
    policy = hashlib.sha256(json.dumps([config['guild_id'], sorted(config['subscriber_role_ids'])]).encode()).hexdigest()
    endpoint = 'http://127.0.0.1:' + str(config['api_port'])
    pack = server / 'behavior_packs/mcbe-connect'
    module = server / 'config' / MODULE_UUID
    changed = [properties, world / 'level.dat', packs_path, pack, module]
    for path in changed:
        if path.is_symlink() or not path.resolve().is_relative_to(server):
            raise ValueError('Installation target must stay inside the selected server: ' + str(path))
    backup = server / '.discord-auth-backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    if not backup.resolve().is_relative_to(server):
        raise ValueError('Backup target must stay inside the selected server')
    backup.mkdir(parents=True, mode=0o700)
    for path in changed:
        if path.exists():
            dst = backup / path.relative_to(server)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                shutil.copytree(path, dst)
            else:
                shutil.copy2(path, dst)
    try:
        shutil.copytree(ROOT / 'addon', pack, dirs_exist_ok=True)
        # Do not set force_tls/force_https, including false: BDS treats its presence as TLS-only.
        write_json(module / 'permissions.json', {
            'allowed_modules': ['@minecraft/server', '@minecraft/server-admin', '@minecraft/server-net'],
            'module_permissions': {'@minecraft/server-net': {
                'allowed_uris': [endpoint + '/v1/authorize', endpoint + '/v1/online'],
                'max_body_bytes': 32768, 'max_concurrent_requests': 12}}
        })
        write_json(module / 'variables.json', {'api_url': endpoint, 'policy_hash': policy})
        write_json(module / 'secrets.json', {'auth_header': 'Bearer ' + config['api_secret']}, secret=True)
        packs = [item for item in packs if item.get('pack_id') != HEADER_UUID]
        packs.append({'pack_id': HEADER_UUID, 'version': [1, 0, 0]})
        write_json(packs_path, packs)
        if mode == 'enable':
            flags = experiments[1]
            for name in ('gametest', 'experiments_ever_used', 'saved_with_toggled_experiments'):
                flags[name] = (1, 1)
            root[2]['experiments'] = (10, flags)
            (world / 'level.dat').write_bytes(dump(root, version))
            controlled = ('allow-list', 'allow-player-joining', 'content-log-console-output-enabled')
            lines = [line for line in lines if line.split('=', 1)[0].strip() not in controlled]
            properties.write_text('\n'.join(lines + ['allow-list=false', 'allow-player-joining=false',
                                                    'content-log-console-output-enabled=true']) + '\n')
    except Exception:
        # Restore each pre-existing target; remove only newly created addon/config targets.
        for path in changed:
            saved = backup / path.relative_to(server)
            if saved.is_dir():
                if path.exists():
                    shutil.rmtree(path)
                shutil.copytree(saved, path)
            elif saved.exists():
                shutil.copy2(saved, path)
            elif path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        raise
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['init', 'enable', 'refresh'])
    parser.add_argument('--config', type=Path, default=ROOT / 'discord-auth/config.json')
    parser.add_argument('--server-dir', type=Path)
    parser.add_argument('--bds-version', choices=[SUPPORTED_BDS])
    parser.add_argument('--stopped', action='store_true', help='Confirm that you stopped the selected BDS')
    args = parser.parse_args()
    try:
        if args.mode == 'init':
            init_config(args.config)
            print('Private config created. Fill in the Discord credentials and IDs before enabling.')
        else:
            if args.server_dir is None or not args.stopped or args.bds_version is None:
                parser.error('enable/refresh require --server-dir, --bds-version and --stopped')
            backup = configure(args.server_dir, args.config, args.mode, args.bds_version)
            print('Authorization installed. Configuration backup: ' + str(backup))
    except (ValueError, OSError, json.JSONDecodeError, struct.error) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
