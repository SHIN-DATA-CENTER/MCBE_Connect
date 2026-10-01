# Bedrock Discord Role Gate

公式のMinecraft統合版Bedrock Dedicated Server（BDS）へ、Discordの指定ロールによる参加制御を追加するアドオンとBotです。Javaサーバーや中継サーバーを追加せず、BDSの参加前イベントで判定します。

**アドオン単体では動きません。** 同じホストで付属のPython Bot/APIを起動し、BDSのBeta APIsとサーバー専用スクリプト権限を設定してください。

## 機能

- 未連携プレイヤーを入室前に拒否し、切断画面へ数字4桁の連携コードを表示。Discordサーバーの招待リンクは表示しません。
- `/mc link コード` で、BDSが認証した `persistentId` とDiscordアカウントを1対1で連携。
- 指定ロールを持つ連携済みプレイヤーだけが参加可能。OP・運営にも例外はありません。
- ロール解除、Discord脱退、`/mc unlink` を検知すると、接続中のプレイヤーも次の資格確認で退出。
- `/mc status` で連携先と資格を確認。Discordの応答は本人だけに表示します。
- SQLiteとワールドの動的プロパティへ資格を保存。DiscordやBotの障害時は最後の判定を期限なしで利用し、未確認者・失効済みの人を拒否します。

コードは半角数字4桁、10分有効、使い切りです。`0123` の先頭の0も入力してください。再接続すると前のコードは無効になります。使用済み・差し替え済みコードは元の有効期限まで再割り当てしません。Discordアカウントごとの連携試行は10分で5回までです。

## 対応環境

| 項目 | この配布版の確認環境 |
| --- | --- |
| サーバー | 公式Linux BDS **1.26.52.3** |
| Python | **3.12**、依存バージョンは `discord-auth/requirements.lock` |
| Node.js | JavaScriptテスト用。実行サーバーには不要 |
| 認証 | `online-mode=true` 必須 |
| ワールド | Beta APIs（`gametest`）を有効化 |
| スクリプト | server `2.11.0-beta`、server-admin / server-net `1.0.0-beta` |

PocketMine/Nukkit/Java用プラグインではありません。サーバー専用APIを使うため、通常のクライアントワールドへのインポートだけでは利用できません。Beta API依存があるので、この配布版の設定ツールは確認済みBDSバージョンに限定しています。

## ファイル構成

```text
addon/                    BDS用Behavior Pack（manifestとJavaScript）
discord-auth/             Discord Bot、localhost API、SQLite処理、設定サンプル
tools/configure.py        停止したBDSへの導入・設定更新
tools/world_nbt.py        level.datのBeta APIsフラグ編集
examples/systemd/         Bot用ユーザーサービスのテンプレート
tests/                    アドオン・導入ツールのテスト
.github/workflows/        GitHub Actionsによるテスト
```

BDS本体、ワールド、Microsoft/Xbox識別鍵、実際のトークン、DiscordサーバーID、連携DBは含みません。独自のサーバー更新機構、NetBird設定、MCXboxBroadcast本体も含みません。

## Discordの準備

1. Discord Developer PortalでBotを作り、**Server Members Intent**を有効にします。Message Content Intentは不要です。
2. `bot` と `applications.commands` のスコープで対象のDiscordサーバーへ招待します。
3. コマンド用チャンネルを閲覧できるよう、Botへ「チャンネルを見る」の権限を付けます。ロール管理権限・Administratorは不要です。
4. Botトークン、DiscordサーバーID、チャンネルID、対象ロールIDを控えます。

このBotは既存のロールを読み取ります。Twitchのサブスク状況とDiscordロールの同期はDiscord側で設定してください。指定ロールが複数ある場合はいずれか1つがあれば参加可能です。

## 導入

リポジトリを任意の場所へ展開し、リポジトリのルートから以下を実行します。BotとBDSは同じホストで動かしてください。

```bash
python3 -m venv discord-auth/venv
discord-auth/venv/bin/python -m pip install -r discord-auth/requirements.lock
python3 tools/configure.py init
```

`discord-auth/config.json` が権限600で作成され、内部APIの秘密値が自動生成されます。次の項目を編集してください。

| 設定 | 内容 |
| --- | --- |
| `bot_token` | このサーバー用Botトークン |
| `guild_id` | 対象DiscordサーバーID。文字列 |
| `channel_id` | `/mc` を使うチャンネルID。文字列 |
| `subscriber_role_ids` | 参加を許可するロールIDの配列。文字列 |
| `api_port` | 同一ホスト内APIのTCPポート。初期値18080 |
| `api_secret` | `init` が自動生成した秘密値。そのまま利用 |

`/mc` コマンドは指定したギルドへ登録され、指定したチャンネルだけで使えます。テストと本番を同時に動かす場合は別々のBotトークンを使用してください。

BDSを一度通常起動してワールドを作成してから、**対象BDSと自動更新処理を停止**してください。ワールド全体と設定のバックアップを取ったうえで、対象のBDSディレクトリを明示して導入します。

```bash
python3 tools/configure.py enable \
  --server-dir /srv/minecraft/bedrock \
  --bds-version 1.26.52.3 \
  --stopped
```

`/srv/minecraft/bedrock` は `bedrock_server` と `server.properties` がある実際のディレクトリに置き換えてください。ツールはsystemdサービスを停止・再起動しません。指定BDSの起動プロセスが見つかった場合は編集を拒否します。

ツールはワールド名・ネットワーク設定・他のBehavior Packを保持し、以下を設定します。

```properties
online-mode=true
allow-list=false
allow-player-joining=false
content-log-console-output-enabled=true
```

`online-mode` がfalseの場合は変更前に拒否します。ワールドの `experiments.gametest` を有効にし、Behavior Packを登録します。モジュールの通信先は `127.0.0.1` の認証APIの2つのパスだけに制限し、秘密値はBDSの `secrets.json` へ権限600で保存します。導入前の編集対象ファイルはBDS内の `.discord-auth-backups/` へ保存します。これはワールド全体のバックアップの代わりにはなりません。

Bot/APIを起動します。

```bash
discord-auth/venv/bin/python discord-auth/service.py
```

別の端末で次を確認します。ポートを変更した場合は読み替えてください。

```bash
curl -fsS http://127.0.0.1:18080/health
```

`discord_configured` と `discord_ready` がtrueになれば、既存の方法でBDSを起動します。起動ログに `[mcbe-connect:ready]` と `[mcbe-connect:api] reachable` が出ることを確認してください。BDSの起動管理は既存の仕組みを利用します。

Botを常駐させる場合は `examples/systemd/discord-auth.service.example` 内の絶対パスを変更し、ユーザーサービスとして登録できます。

```bash
mkdir -p ~/.config/systemd/user
cp examples/systemd/discord-auth.service.example ~/.config/systemd/user/discord-auth.service
# コピー先の /opt/bedrock-discord-auth を実際の展開先へ変更する
systemctl --user daemon-reload
systemctl --user enable --now discord-auth.service
journalctl --user -u discord-auth.service -f
```

## プレイヤーの操作

1. Minecraftへ接続して切断画面の4桁コードを控える。
2. 指定したDiscordチャンネルで `/mc link` の `code` 欄へコードを文字列として入力。
3. Minecraftへ再接続する。

非サブスクのアカウントでは連携を許可しません。連携を解除するには `/mc unlink`、状況確認には `/mc status` を使います。ゲーマータグが変わってもBDSが認証する同じ `persistentId` の連携を利用します。

## 資格更新・障害時の動作

接続中の確認は100tick（通常5秒）、Discordとの再確認は5分ごとです。Discordのロールイベント・脱退イベントでもキャッシュを更新します。退出までの時間にはTwitchからDiscordへのロール同期の遅延と、サーバーのtick遅延が加わります。

API・Discord障害時は保存済みの資格を期限なしで利用します。障害中に新しく失効した資格はDiscordへ復旧するまで検知できません。失効を確認済みの資格は維持し、未確認者には参加を許可しません。Bot停止中は新しいコードを発行できません。連携DBやワールドの資格キャッシュを消すと障害時の判定を維持できないため、セットでバックアップしてください。

ギルドIDまたは許可ロールの集合を変更すると、資格ポリシーが変わり、古い資格キャッシュを参加許可には使いません。設定変更後はBDSを停止して `configure.py refresh` を `enable` と同じ引数で実行し、Bot/APIとBDSを再起動してください。

BDS更新で配置先が変わる場合も、新しい配置先へ `refresh` してください。このZIPには特定ホストの自動更新・自動ロールバックサービスは含めていません。別のBDSバージョンではBeta APIの互換性を検証してからmanifestと設定ツールを更新してください。

## テスト

```bash
discord-auth/venv/bin/python -m unittest discover -s discord-auth/tests -v
discord-auth/venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
npm test
```

Discord/BDSへの実接続を使わず、期限切れ・再利用・重複連携・先頭の0・招待リンク非表示・資格解除・脱退・オフラインキャッシュ・再起動・応答順序をテストします。導入ツールは一時ワールドを使い、設定の保持、別パスへの書き込み拒否、失敗時の設定復元を確認します。実際のMinecraftでの切断画面・参加・退出は、導入先で確認してください。

## GitHubへの配置

ZIP内の `bedrock-discord-auth` フォルダの中身をリポジトリのルートへ配置します。`.gitignore` とGitHub Actionsも含まれています。実運用後の `config.json`、`accounts.sqlite3`、BDSのワールド・識別鍵・モジュール設定を公開物へ追加しないでください。

公開ライセンスは未指定です。`package.json` の `UNLICENSED` はその状態を示しています。再利用を許可する形で公開する場合は、公開者がライセンスを選び、`LICENSE` と `package.json` を設定してください。
