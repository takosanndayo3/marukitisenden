import os
import random
import threading
from datetime import time as dt_time, timezone, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from libsql_client import create_client

# ============ 設定（環境変数から読み込み） ============
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "file:local.db")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN") or None
POST_HOUR = int(os.environ.get("POST_HOUR", 12))  # 毎日何時に投稿するか（JST）

JST = timezone(timedelta(hours=9))

# ============ Bot 初期化 ============
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ============ DB（Turso or ローカルSQLite） ============
db = None


def _make_db_client():
    if TURSO_TOKEN:
        return create_client(url=TURSO_URL, auth_token=TURSO_TOKEN)
    return create_client(url=TURSO_URL)


async def init_db():
    global db
    db = _make_db_client()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS guilds (
            guild_id        INTEGER PRIMARY KEY,
            ad_text         TEXT,
            invite_url      TEXT,
            post_channel_id INTEGER
        )
    """)


async def upsert_field(guild_id: int, column: str, value):
    """レコードがなければ作ってから、指定カラムを更新する"""
    await db.execute(
        "INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)", [guild_id]
    )
    await db.execute(
        f"UPDATE guilds SET {column} = ? WHERE guild_id = ?", [value, guild_id]
    )


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
        await interaction.response.send_message(
            "✅ 宣伝文を保存しました。", ephemeral=True
        )


# ============ スラッシュコマンド ============
@tree.command(name="招待", description="招待リンクを作成して保存します")
@app_commands.describe(channel="招待を作成するチャンネル（省略時は現在のチャンネル）")
async def cmd_invite(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message(
            "❌ テキストチャンネルを指定してください。", ephemeral=True
        )
        return
    try:
        invite = await target.create_invite(
            max_age=0, max_uses=0, unique=True, reason="鯖宣伝BOT用"
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.response.send_message(
            f"❌ 招待を作成できませんでした（{e}）。「招待を作成」権限を確認してください。",
            ephemeral=True,
        )
        return
    await upsert_field(interaction.guild_id, "invite_url", invite.url)
    await interaction.response.send_message(
        f"✅ 招待リンクを保存しました: {invite.url}", ephemeral=True
    )


@tree.command(name="宣伝文", description="宣伝文を設定します")
async def cmd_ad_text(interaction: discord.Interaction):
    await interaction.response.send_modal(AdTextModal())


@tree.command(name="設定", description="宣伝の投稿先チャンネルを設定します")
@app_commands.describe(channel="投稿先チャンネル（省略時は現在のチャンネル）")
async def cmd_set_channel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message(
            "❌ テキストチャンネルを指定してください。", ephemeral=True
        )
        return
    await upsert_field(interaction.guild_id, "post_channel_id", target.id)
    await interaction.response.send_message(
        f"✅ 投稿先を {target.mention} に設定しました。", ephemeral=True
    )


@tree.command(name="宣伝文解除", description="このサーバーの宣伝文を削除します")
async def cmd_remove_ad(interaction: discord.Interaction):
    await db.execute(
        "UPDATE guilds SET ad_text = NULL WHERE guild_id = ?",
        [interaction.guild_id],
    )
    await interaction.response.send_message(
        "✅ 宣伝文を削除しました。", ephemeral=True
    )


@tree.command(name="招待解除", description="このサーバーの招待リンクを削除します")
async def cmd_remove_invite(interaction: discord.Interaction):
    await db.execute(
        "UPDATE guilds SET invite_url = NULL WHERE guild_id = ?",
        [interaction.guild_id],
    )
    await interaction.response.send_message(
        "✅ 招待リンクを削除しました。", ephemeral=True
    )


# ============ 定時投稿（毎日1回） ============
@tasks.loop(time=dt_time(hour=POST_HOUR, minute=0, tzinfo=JST))
async def daily_ad_post():
    try:
        # 投稿先を設定しているギルド一覧
        rs = await db.execute(
            "SELECT guild_id, post_channel_id FROM guilds "
            "WHERE post_channel_id IS NOT NULL"
        )
        targets = rs.rows or []

        # 宣伝可能な広告（ad_text と invite_url が両方あるもの）
        rs = await db.execute(
            "SELECT guild_id, ad_text, invite_url FROM guilds "
            "WHERE ad_text IS NOT NULL AND invite_url IS NOT NULL"
        )
        ads = rs.rows or []

        if not ads:
            print("[daily_ad_post] 宣伝可能な広告がまだありません")
            return

        for guild_id, post_channel_id in targets:
            # 自サーバー以外の広告からランダムに1件選ぶ
            candidates = [a for a in ads if a[0] != guild_id]
            if not candidates:
                continue

            _, ad_text, invite_url = random.choice(candidates)
            channel = bot.get_channel(post_channel_id)
            if channel is None:
                print(f"[daily_ad_post] チャンネル {post_channel_id} が見つかりません")
                continue

            embed = discord.Embed(
                title="📢 本日のサーバー宣伝",
                description=ad_text,
                color=discord.Color.blue(),
            )
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
        await init_db()
        print("DB初期化完了")
    except Exception as e:
        print(f"DB初期化エラー: {e}")
        return

    try:
        synced = await tree.sync()
        print(f"スラッシュコマンド同期: {len(synced)}件")
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
