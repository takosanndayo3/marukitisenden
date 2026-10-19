import os
import asyncio
import random
import threading
from datetime import time as dt_time, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
import turso_serverless as turso  # libsql_client から変更

# ============ 設定 ============
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "file:local.db")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN") or None
POST_HOUR = int(os.environ.get("POST_HOUR", 12))  # 毎日何時（JST）に投稿するか
GUILD_ID = os.environ.get("GUILD_ID")  # 開発用: 特定ギルドに即時同期したい場合にIDを指定

JST = timezone(timedelta(hours=9))

# ============ Bot 初期化 ============
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ============ DB 接続管理 ============
# turso_serverless は sqlite3 互換の同期APIを提供するため、asyncio.to_thread でラップして使用します。
_db_conn = None

def _get_conn():
    global _db_conn
    if _db_conn is None:
        # Render の無料枠では libsql:// より https:// を明示する方が安定します。
        url = TURSO_URL.replace("libsql://", "https://") if TURSO_URL.startswith("libsql://") else TURSO_URL
        if TURSO_TOKEN:
            _db_conn = turso.connect(url, auth_token=TURSO_TOKEN)
        else:
            _db_conn = turso.connect(url)
    return _db_conn

async def db_execute(sql: str, params=()):
    """同期DB呼び出しをスレッドプールで実行し、イベントループをブロックしない"""
    conn = _get_conn()
    return await asyncio.to_thread(lambda: conn.execute(sql, params).fetchall())

async def db_init():
    await db_execute("""
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id        INTEGER PRIMARY KEY,
            ad_text         TEXT,
            invite_url      TEXT,
            post_channel_id INTEGER
        )
    """)
    await asyncio.to_thread(lambda: _get_conn().commit())

async def upsert_field(guild_id: int, column: str, value):
    await db_execute("INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)", (guild_id,))
    await db_execute(f"UPDATE guilds SET {column} = ? WHERE guild_id = ?", (value, guild_id))
    await asyncio.to_thread(lambda: _get_conn().commit())

# ============ 宣伝文モーダル ============
class AdTextModal(discord.ui.Modal, title="宣伝文を設定"):
    ad_text = discord.ui.TextInput(
        label="宣伝文",
        style=discord.TextStyle.long,
        placeholder="サーバーの宣伝文を入力してください（最大1000文字）",
        max_length=1000,
        required=True,
    )
    async def on_submit(self, interaction: discord.Interaction):
        await upsert_field(interaction.guild_id, "ad_text", self.ad_text.value)
        await interaction.response.send_message("✅ 宣伝文を保存しました。", ephemeral=True)

# ============ スラッシュコマンド ============
@tree.command(name="招待", description="招待リンクを作成して保存します")
@app_commands.describe(channel="招待を作成するチャンネル（省略時は現在のチャンネル）")
async def cmd_invite(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("❌ テキストチャンネルを指定してください。", ephemeral=True)
    try:
        invite = await target.create_invite(max_age=0, max_uses=0, unique=True, reason="鯖宣伝BOT用")
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(f"❌ 招待を作成できませんでした（{e}）。権限を確認してください。", ephemeral=True)
    await upsert_field(interaction.guild_id, "invite_url", invite.url)
    await interaction.response.send_message(f"✅ 招待リンクを保存しました: {invite.url}", ephemeral=True)

@tree.command(name="宣伝文", description="宣伝文を設定します")
async def cmd_ad_text(interaction: discord.Interaction):
    await interaction.response.send_modal(AdTextModal())

@tree.command(name="設定", description="宣伝の投稿先チャンネルを設定します")
@app_commands.describe(channel="投稿先チャンネル（省略時は現在のチャンネル）")
async def cmd_set_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("❌ テキストチャンネルを指定してください。", ephemeral=True)
    await upsert_field(interaction.guild_id, "post_channel_id", target.id)
    await interaction.response.send_message(f"✅ 投稿先を {target.mention} に設定しました。", ephemeral=True)

@tree.command(name="宣伝文解除", description="このサーバーの宣伝文を削除します")
async def cmd_remove_ad(interaction: discord.Interaction):
    await db_execute("UPDATE guilds SET ad_text = NULL WHERE guild_id = ?", (interaction.guild_id,))
    await asyncio.to_thread(lambda: _get_conn().commit())
    await interaction.response.send_message("✅ 宣伝文を削除しました。", ephemeral=True)

@tree.command(name="招待解除", description="このサーバーの招待リンクを削除します")
async def cmd_remove_invite(interaction: discord.Interaction):
    await db_execute("UPDATE guilds SET invite_url = NULL WHERE guild_id = ?", (interaction.guild_id,))
    await asyncio.to_thread(lambda: _get_conn().commit())
    await interaction.response.send_message("✅ 招待リンクを削除しました。", ephemeral=True)

# ============ 定時投稿（毎日1回） ============
@tasks.loop(time=dt_time(hour=POST_HOUR, minute=0, tzinfo=JST))
async def daily_ad_post():
    try:
        # 投稿先が設定されているギルド
        targets = await db_execute("SELECT guild_id, post_channel_id FROM guilds WHERE post_channel_id IS NOT NULL")
        # 宣伝可能な広告（宣伝文と招待URLの両方があるもの）
        ads = await db_execute("SELECT guild_id, ad_text, invite_url FROM guilds WHERE ad_text IS NOT NULL AND invite_url IS NOT NULL")

        if not ads:
            return print("[daily_ad_post] 宣伝可能な広告がまだありません")

        for guild_id, post_channel_id in targets:
            candidates = [a for a in ads if a[0] != guild_id]  # 自サーバー以外から選ぶ
            if not candidates:
                continue

            _, ad_text, invite_url = random.choice(candidates)
            channel = bot.get_channel(post_channel_id)
            if channel is None:
                print(f"[daily_ad_post] チャンネル {post_channel_id} が見つかりません")
                continue

            embed = discord.Embed(title="📢 本日のサーバー宣伝", description=ad_text, color=discord.Color.blue())
            embed.add_field(name="招待リンク", value=invite_url, inline=False)

            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                print(f"[daily_ad_post] {post_channel_id} に投稿する権限がありません")
            except Exception as e:
                print(f"[daily_ad_post] 投稿エラー: {e}")
    except Exception as e:
        print(f"[daily_ad_post] 致命的エラー: {e}")

# ============ 起動処理 ============
@bot.event
async def on_ready():
    print(f"ログイン: {bot.user}")
    try:
        await db_init()
        print("DB初期化完了")
    except Exception as e:
        return print(f"DB初期化エラー: {e}")

    try:
        # GUILD_ID が設定されていればそのギルドに即時同期（開発用）、なければグローバル同期（本番用）
        if GUILD_ID:
            synced = await tree.sync(guild=discord.Object(id=int(GUILD_ID)))
            print(f"ギルド同期完了: {len(synced)}件（GUILD_ID={GUILD_ID}）")
        else:
            synced = await tree.sync()
            print(f"グローバル同期完了: {len(synced)}件（反映に最大1時間かかる場合があります）")
    except Exception as e:
        print(f"コマンド同期エラー: {e}")

    if not daily_ad_post.is_running():
        daily_ad_post.start()
        print(f"定時投稿タスク開始（毎日 {POST_HOUR}時 JST）")
    print("準備完了")

# ============ ヘルスチェック（Render Web Service用） ============
flask_app = Flask(__name__)

@flask_app.route("/")
def health_check():
    return "OK", 200

def _run_web_server():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# ============ エントリポイント ============
if __name__ == "__main__":
    threading.Thread(target=_run_web_server, daemon=True).start()
    bot.run(DISCORD_TOKEN)
