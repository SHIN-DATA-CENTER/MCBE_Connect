"""Local BDS admission API and a guild-scoped, read-only-role Discord bot."""
import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import signal
from pathlib import Path

import discord
from discord import app_commands
from aiohttp import web

from store import Store, LinkError

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('mochi-auth')


class Unavailable(RuntimeError):
    pass


class AuthService:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.policy = hashlib.sha256(json.dumps([config['guild_id'], sorted(config['subscriber_role_ids'])]).encode()).hexdigest()
        store.configure_policy(self.policy)
        self.client = None

    @property
    def configured(self):
        return bool(self.config.get('bot_token') and self.config['guild_id'] and self.config['subscriber_role_ids'] and self.config['channel_id'])

    async def member_allowed(self, discord_id):
        if not self.configured or not self.client or not self.client.is_ready():
            raise Unavailable('Discord connection unavailable')
        guild = self.client.get_guild(int(self.config['guild_id']))
        if not guild:
            raise Unavailable('Configured guild unavailable')
        try:
            member = await asyncio.wait_for(guild.fetch_member(int(discord_id)), 2)
        except discord.NotFound:
            return False
        except (discord.HTTPException, asyncio.TimeoutError, OSError) as exc:
            raise Unavailable('Discord member lookup failed') from exc
        return bool({str(r.id) for r in member.roles} & set(self.config['subscriber_role_ids']))

    async def refresh(self, discord_id):
        before = self.store.user(discord_id)
        try:
            allowed = await self.member_allowed(discord_id)
        except Unavailable:
            return False
        after = self.store.user(discord_id)
        # A gateway revocation/unlink arriving during the REST request wins.
        if before and after and (before['pid'], before['revision']) == (after['pid'], after['revision']):
            self.store.decide(discord_id, allowed)
        return True

    async def authorize(self, pid, name, want_code=False):
        row = self.store.player(pid, name)
        online = False
        if row['discord_id'] and (row['checked_at'] is None or self.store.clock() - row['checked_at'] >= 300):
            online = await self.refresh(row['discord_id'])
            row = self.store.player(pid)
        elif row['discord_id']:
            online = bool(self.client and self.client.is_ready())
        result = {'pid': pid, 'decision': row['decision'], 'revision': row['revision'],
                  'policy': self.policy, 'offline': not online}
        if not row['discord_id']:
            result['decision'] = 'unlinked'
            if want_code:
                try:
                    result['code'] = self.store.challenge(pid)
                except LinkError as exc:
                    result['message'] = str(exc)
                else:
                    result['message'] = f"Discord連携が必要です。\nDiscordの指定チャンネルで /mc link {result['code']}\nコード有効期限: 10分"
            else:
                result['decision'] = 'denied'
        elif row['decision'] == 'denied':
            result['message'] = 'サブスクロールが確認できないため参加できません。'
        elif row['decision'] == 'unknown':
            result['message'] = '参加資格を確認できません。Discordの復旧後に再接続してください。'
        return result

    def app(self):
        @web.middleware
        async def security(request, handler):
            if request.path != '/health':
                supplied = request.headers.get('Authorization', '')
                if not secrets.compare_digest(supplied, 'Bearer ' + self.config['api_secret']):
                    raise web.HTTPUnauthorized()
            try:
                return await handler(request)
            except (ValueError, KeyError, TypeError):
                raise web.HTTPBadRequest(text='Invalid request')

        app = web.Application(middlewares=[security], client_max_size=32768)

        async def health(request):
            return web.json_response({'ok': True, 'discord_configured': self.configured,
                                      'discord_ready': bool(self.client and self.client.is_ready())})

        def identity(value):
            pid, name = value['pid'], value.get('name', '')
            if not isinstance(pid, str) or not 1 <= len(pid) <= 128 or not isinstance(name, str) or len(name) > 128:
                raise ValueError('Invalid identity')
            if any(ord(c) < 32 for c in pid + name):
                raise ValueError('Invalid identity')
            return pid, name

        async def authorize(request):
            value = await request.json()
            pid, name = identity(value)
            return web.json_response(await self.authorize(pid, name, True))

        async def online(request):
            value = await request.json()
            players = value['players']
            if not isinstance(players, list) or len(players) > 100:
                raise ValueError('Too many players')
            # A single deadline also bounds the batch if Discord is slow.
            results = await asyncio.gather(*(self.authorize(*identity(p)) for p in players))
            return web.json_response({'players': results, 'policy': self.policy})

        app.router.add_get('/health', health)
        app.router.add_post('/v1/authorize', authorize)
        app.router.add_post('/v1/online', online)
        return app


class Bot(discord.Client):
    def __init__(self, service):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents, max_messages=None, enable_debug_events=True)
        self.service = service
        self.tree = app_commands.CommandTree(self)
        self.group = app_commands.Group(name='mc', description='Minecraftアカウント連携')
        self.group.add_command(app_commands.Command(name='link', description='Minecraftに表示されたコードで連携する', callback=self.link))
        self.group.add_command(app_commands.Command(name='status', description='自分の連携と参加資格を確認する', callback=self.status))
        self.group.add_command(app_commands.Command(name='unlink', description='自分のMinecraft連携を解除する', callback=self.unlink))
        self.tree.add_command(self.group, guild=discord.Object(id=int(service.config['guild_id'])))

    async def setup_hook(self):
        await self.tree.sync(guild=discord.Object(id=int(self.service.config['guild_id'])))

    async def guard(self, interaction):
        config = self.service.config
        if str(interaction.guild_id) != config['guild_id'] or str(interaction.channel_id) != config['channel_id']:
            await interaction.response.send_message('このコマンドは指定されたチャンネルで利用してください。', ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True)
        return True

    async def link(self, interaction: discord.Interaction, code: str):
        if not await self.guard(interaction):
            return
        try:
            allowed = await self.service.member_allowed(interaction.user.id)
            row = self.service.store.claim(interaction.user.id, code, allowed)
        except LinkError as exc:
            message = str(exc)
        except Unavailable:
            message = '現在Discordの参加資格を確認できません。後ほど再試行してください。'
        else:
            message = f"{row['name']} と連携しました。Minecraftへ再接続してください。"
            LOG.info('account linked')
        await interaction.followup.send(message, ephemeral=True)

    async def status(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        row = self.service.store.user(interaction.user.id)
        if row:
            refreshed = await self.service.refresh(interaction.user.id)
            row = self.service.store.user(interaction.user.id)
            if row:
                label = {'allowed': '参加可能', 'denied': '参加不可', 'unknown': '未確認'}[row['decision']]
                message = f"連携先: {row['name']} / {label}" + ('' if refreshed else '（最後に確認した資格）')
            else:
                message = '連携は解除されています。'
        else:
            message = '未連携です。Minecraftに接続してコードを取得してください。'
        await interaction.followup.send(message, ephemeral=True)

    async def unlink(self, interaction: discord.Interaction):
        if not await self.guard(interaction):
            return
        done = self.service.store.unlink(interaction.user.id)
        LOG.info('account unlinked' if done else 'unlink requested without account')
        await interaction.followup.send('連携を解除しました。接続中の場合は退出します。' if done else '連携されていません。', ephemeral=True)

    async def on_ready(self):
        LOG.info('Discord connected; synchronizing linked accounts')
        await self.reconcile()

    async def reconcile(self):
        for row in self.service.store.linked():
            await self.service.refresh(row['discord_id'])

    async def on_member_update(self, before, after):
        if str(after.guild.id) != self.service.config['guild_id']:
            return
        allowed = bool({str(r.id) for r in after.roles} & set(self.service.config['subscriber_role_ids']))
        self.service.store.decide(after.id, allowed)
        LOG.info('Discord role update applied')

    async def on_socket_response(self, payload):
        # discord.py discards member_update for members absent from its cache.
        # Handle the authenticated gateway event too, without logging payloads.
        if payload.get('t') != 'GUILD_MEMBER_UPDATE':
            return
        data = payload.get('d') or {}
        if str(data.get('guild_id')) == self.service.config['guild_id'] and 'roles' in data and data.get('user', {}).get('id'):
            allowed = bool(set(str(r) for r in data['roles']) & set(self.service.config['subscriber_role_ids']))
            self.service.store.decide(data['user']['id'], allowed)

    async def on_raw_member_remove(self, event):
        if str(event.guild_id) == self.service.config['guild_id']:
            self.service.store.decide(event.user.id, False)
            LOG.info('Discord membership removal applied')

    async def on_guild_role_delete(self, role):
        if str(role.guild.id) == self.service.config['guild_id'] and str(role.id) in self.service.config['subscriber_role_ids']:
            await self.reconcile()


async def main():
    config = json.loads((ROOT / 'config.json').read_text())
    for key in ('guild_id', 'channel_id'):
        config[key] = str(config[key])
    config['subscriber_role_ids'] = [str(x) for x in config['subscriber_role_ids']]
    if len(config['api_secret']) < 32 or config['api_secret'].startswith('GENERATED_'):
        raise RuntimeError('API secret must contain at least 32 characters')
    port = config.get('api_port', 18080)
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError('api_port must be an integer from 1 to 65535')
    store = Store(ROOT / 'accounts.sqlite3')
    service = AuthService(config, store)
    runner = web.AppRunner(service.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', port).start()
    LOG.info('Local API ready on 127.0.0.1:%s; Discord configured=%s', port, service.configured)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def run_discord():
        while not stop.is_set():
            bot = Bot(service)
            service.client = bot
            try:
                await bot.start(config['bot_token'])
            except (discord.DiscordException, OSError) as exc:
                LOG.error('Discord connection failed (%s); cached decisions retained', type(exc).__name__)
            finally:
                await bot.close()
            try:
                await asyncio.wait_for(stop.wait(), 60)
            except asyncio.TimeoutError:
                pass

    async def reconcile():
        while not stop.is_set():
            if service.client and service.client.is_ready():
                await service.client.reconcile()
            try:
                await asyncio.wait_for(stop.wait(), 300)
            except asyncio.TimeoutError:
                pass

    tasks = [asyncio.create_task(reconcile())]
    if service.configured:
        tasks.append(asyncio.create_task(run_discord()))
    await stop.wait()
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await runner.cleanup()
    store.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
