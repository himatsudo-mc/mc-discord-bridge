# mc-discord-bridge

Minecraft (Java Edition, Paper/Purpur 26.3) と Discord 間で、チャットの双方向同期
とサーバーの起動・停止通知を行う連携システムです。

- `minecraft-plugin/` — Paper/Purpur 用プラグイン（Java）
- `discord-bot/` — discord.py 製 Discord Bot（Python 3.10+）
- `PROTOCOL.md` — 両者を繋ぐ WebSocket 通信プロトコル仕様書

両者は WebSocket で接続され、共通の秘密トークンで認証します。詳細は
[PROTOCOL.md](PROTOCOL.md) を参照してください。

## 機能

- **マイクラ → Discord**: プレイヤーの発言を指定チャンネルへ転送（メンション・
  Markdown はエスケープ済み、`@everyone` 等への誤爆なし）
- **Discord → マイクラ**: 指定チャンネルの投稿をサーバー全体チャットへ転送
  （Bot自身の発言・コマンドプレフィックス付きメッセージは無視し、ループを防止）
- **起動/停止通知**: サーバー起動完了時・シャットダウン時に Discord へ Embed 通知
- **入退室通知**: プレイヤーのログイン/ログアウトを指定チャンネルへ通知
- **参加者一覧コマンド**: Discord のチャットチャンネルでスラッシュコマンド `/players` を
  実行すると、そのチャンネルに紐づく Minecraft サーバーの現在の参加者一覧（人数/最大人数付き）
  を Embed で返します。サーバー未接続時はその旨を返信します
  （Bot の招待時に `applications.commands` スコープが必要です）
- **動的VC・エリア連動（himatsudoLife 連携）**: Bot は himatsudoLife プラグイン向けの HTTP API
  （PROTOCOL.md の "HTTP API"）も提供します。`/hl vcc` で作られたフレンド/パーティ/パブリックVCを
  無人になった時点で自動削除し、エリアの出入りに応じて Discord ロールを付与/剥奪します。
  Bot には **チャンネルの管理** と **ロールの管理** 権限が必要です
- **MCIDとDiscordアカウントの紐づけ（運営用）**: 運営が Discord で `/link user:<メンバー> mcid:<MCID>` を
  実行して紐づけます（解除は `/unlink`、確認は `/linkinfo`）。ゲーム内での操作は不要です。
  コマンドは「ロールの管理」権限を持つ人にだけ表示され、返信は実行者にだけ見えます。
  統合版プレイヤーは接頭辞込みの名前（例: `.Steve`）で、MCIDを変更したプレイヤーは紐づけ直してください
- **障害耐性****: Discord Bot がオフラインでもマイクラ側の処理はブロックされず、
  自動的に再接続を試みます。切断中のメッセージは破棄せず一定件数キューイングし、
  再接続時に順番に送信します
- **複数サーバー対応**: 1つの Discord Bot プロセスで複数の Minecraft サーバーを
  それぞれ別の Discord チャンネルに橋渡しできます（`server1 ↔ channel1`,
  `server2 ↔ channel2` など）

## セットアップ

### 1. Discord Bot (`discord-bot/`)

```bash
cd discord-bot
pip install -r requirements.txt
cp .env.example .env            # DISCORD_BOT_TOKEN を設定
cp config.example.yml config.yml  # チャンネルID・secret 等を設定
python main.py
```

`config.yml` で設定する項目（1サーバー構成の場合）:

- `discord.chat_channel_id` / `discord.notify_channel_id`: 対象チャンネルID
- `bridge.host` / `bridge.port`: プラグインが接続してくる待受アドレス
- `bridge.secret`: プラグイン側と一致させる共有シークレット

複数の Minecraft サーバーをそれぞれ別チャンネルに繋ぎたい場合は、上記の
`discord.*` / `bridge.secret` の代わりに `links:` セクションを使います
（`config.example.yml` にコメントアウトされた例があります）。`links:` を
設定すると `discord.chat_channel_id` 等のスカラー項目は無視されるため、
両方を混在させないでください。

```yaml
links:
  - server_id: "survival"
    secret: "CHANGE_ME_SURVIVAL_SECRET"
    chat_channel_id: 111111111111111111
    notify_channel_id: 111111111111111111
  - server_id: "creative"
    secret: "CHANGE_ME_CREATIVE_SECRET"
    chat_channel_id: 222222222222222222
    notify_channel_id: 222222222222222222
```

### 2. Minecraft プラグイン (`minecraft-plugin/`)

```bash
cd minecraft-plugin
mvn package
```

生成された `target/mc-discord-bridge-plugin-1.0.0.jar` を各 Minecraft サーバーの
`plugins/` に配置して起動すると、既定の `config.yml` が生成されます。以下を
Discord Bot 側と一致させてください:

- `bridge.host` / `bridge.port`: Discord Bot の待受アドレス
- `bridge.secret`: 対象サーバーに対応する Discord Bot 側の secret
  （1サーバー構成なら `bridge.secret`、複数サーバー構成なら該当する
  `links[].secret`）
- `server.id`: 複数サーバー構成の場合のみ、Discord Bot 側の対応する
  `links[].server_id` と完全に一致させてください（1サーバー構成では
  既定値 `"default"` のままで構いません）

### 3. テスト（Discord Bot）

```bash
cd discord-bot
pip install -r requirements-dev.txt
pytest
```

### 4. 動作確認

1. Discord Bot を先に起動する
2. Minecraft サーバーを起動する → 起動完了時に Discord へ通知が届くこと、
   プラグインログに認証成功のログが出ることを確認
3. ゲーム内チャット・Discord チャンネル双方でメッセージが転送されることを確認
4. プレイヤーがサーバーに参加/退出する → Discord へ入退室通知が届くことを確認
5. Minecraft サーバーを停止する → Discord へ停止通知が届くことを確認

## ディレクトリ構成

```
mc-discord-bridge/
├── PROTOCOL.md                 # 通信プロトコル仕様書
├── minecraft-plugin/           # Java プラグイン (Maven)
│   ├── pom.xml
│   └── src/main/java/com/mcdiscordbridge/
│       ├── DiscordBridgePlugin.java
│       ├── bridge/             # WebSocket クライアント・再接続管理
│       ├── config/             # config.yml の型付きラッパー
│       ├── listener/           # チャット・入退室イベント購読
│       └── util/                # JSON / フォーマット補助
└── discord-bot/                # Python Bot
    ├── main.py
    ├── bridge/
    │   ├── config.py            # config.yml / .env 読み込み
    │   ├── ws_server.py         # WebSocket サーバー (aiohttp)
    │   ├── discord_bot.py       # discord.py クライアント
    │   └── protocol.py          # メッセージ種別定数
    └── tests/                  # pytest によるユニットテスト
```
