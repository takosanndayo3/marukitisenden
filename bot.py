import os
import asyncio
import random
import threading
from datetime import time as dt_time, timezone, timedelta
from typing import Optional
from flask import Flask, request

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
import turso_serverless as turso

# ============ 設定 ============
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "file:local.db")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN") or None
POST_HOUR = int(os.environ.get("POST_HOUR", 12))
GUILD_ID = os.environ.get("GUILD_ID")
MIN_HUMANS = 10

JST = timezone(timedelta(hours=9))

# ============ Bot 初期化 ============
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ============ DB 接続管理 ============
_db_conn = None

def _get_conn():
    global _db_conn
    if _db_conn is None:
        url = TURSO_URL.replace("libsql://", "https://") if TURSO_URL.startswith("libsql://") else TURSO_URL
        if TURSO_TOKEN:
            _db_conn = turso.connect(url, auth_token=TURSO_TOKEN)
        else:
            _db_conn = turso.connect(url)
    return _db_conn

async def db_execute(sql: str, params=()):
    conn = _get_conn()
    try:
        return await asyncio.to_thread(lambda: conn.execute(sql, params).fetchall())
    except Exception as e:
        print(f"[DB Error] {e} - 再接続を試みます")
        global _db_conn
        _db_conn = None  # 接続をリセット
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

# ============ カスタムチェック：人間の人数制限 ============
def require_min_humans(min_count: int = MIN_HUMANS):
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("このコマンドはサーバー内でのみ実行できます。")
        human_count = sum(1 for m in interaction.guild.members if not m.bot)
        if human_count < min_count:
            raise app_commands.CheckFailure(
                f"荒らし対策のため、**人間のメンバーが{min_count}人以上**のサーバーでのみ登録できます。\n"
                f"現在の人間メンバー数: **{human_count}人**"
            )
        return True
    return app_commands.check(predicate)

# ============ 宣伝文モーダル ============
class AdTextModal(discord.ui.Modal, title="宣伝文を設定"):
    def __init__(self, edit_mode: bool = False, default_text: str = ""):
        super().__init__()
        self.edit_mode = edit_mode
        self.ad_text = discord.ui.TextInput(
            label="宣伝文",
            style=discord.TextStyle.long,
            placeholder="サーバーの宣伝文を入力してください（最大1000文字）",
            max_length=1000,
            required=True,
            default=default_text,
        )
        self.add_item(self.ad_text)

    async def on_submit(self, interaction: discord.Interaction):
        # モーダル送信時にも登録状態を再チェック（二重送信対策）
        rs = await db_execute("SELECT ad_text FROM guilds WHERE guild_id = ?", (interaction.guild_id,))
        current = rs[0][0] if rs else None

        if self.edit_mode:
            if not current:
                return await interaction.response.send_message("❌ 宣伝文が登録されていません。新規登録は `/宣伝文` から行ってください。", ephemeral=True)
        else:
            if current:
                return await interaction.response.send_message("❌ すでに宣伝文が登録されています。編集するには `/宣伝文編集` を使用してください。", ephemeral=True)

        await upsert_field(interaction.guild_id, "ad_text", self.ad_text.value)
        await interaction.response.send_message("✅ 宣伝文を保存しました。", ephemeral=True)

# ============ スラッシュコマンド（管理者限定 + 人数制限） ============

@tree.command(name="招待", description="招待リンクを新規登録します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@require_min_humans()
@app_commands.describe(channel="招待を作成するチャンネル（省略時は現在のチャンネル）")
async def cmd_invite(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    # すでに登録されているかチェック
    rs = await db_execute("SELECT invite_url FROM guilds WHERE guild_id = ?", (interaction.guild_id,))
    if rs and rs[0][0]:
        return await interaction.response.send_message("❌ すでに招待リンクが登録されています。編集するには `/招待編集` を使用してください。", ephemeral=True)

    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("❌ テキストチャンネルを指定してください。", ephemeral=True)
    try:
        invite = await target.create_invite(max_age=0, max_uses=0, unique=True, reason="鯖宣伝BOT用")
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(f"❌ 招待を作成できませんでした（{e}）。権限を確認してください。", ephemeral=True)
    await upsert_field(interaction.guild_id, "invite_url", invite.url)
    await interaction.response.send_message(f"✅ 招待リンクを登録しました: {invite.url}", ephemeral=True)

@tree.command(name="招待編集", description="登録済みの招待リンクを新しいものに差し替えます（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@require_min_humans()
@app_commands.describe(channel="招待を作成するチャンネル（省略時は現在のチャンネル）")
async def cmd_invite_edit(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    # 登録済みかチェック
    rs = await db_execute("SELECT invite_url FROM guilds WHERE guild_id = ?", (interaction.guild_id,))
    if not rs or not rs[0][0]:
        return await interaction.response.send_message("❌ 招待リンクが登録されていません。新規登録は `/招待` から行ってください。", ephemeral=True)

    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("❌ テキストチャンネルを指定してください。", ephemeral=True)
    try:
        invite = await target.create_invite(max_age=0, max_uses=0, unique=True, reason="鯖宣伝BOT用（編集）")
    except (discord.Forbidden, discord.HTTPException) as e:
        return await interaction.response.send_message(f"❌ 招待を作成できませんでした（{e}）。権限を確認してください。", ephemeral=True)
    await upsert_field(interaction.guild_id, "invite_url", invite.url)
    await interaction.response.send_message(f"✅ 招待リンクを更新しました: {invite.url}", ephemeral=True)

@tree.command(name="宣伝文", description="宣伝文を新規登録します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@require_min_humans()
async def cmd_ad_text(interaction: discord.Interaction):
    await interaction.response.send_modal(AdTextModal(edit_mode=False))

@tree.command(name="宣伝文編集", description="登録済みの宣伝文を編集します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@require_min_humans()
async def cmd_ad_text_edit(interaction: discord.Interaction):
    rs = await db_execute("SELECT ad_text FROM guilds WHERE guild_id = ?", (interaction.guild_id,))
    if not rs or not rs[0][0]:
        return await interaction.response.send_message("❌ 宣伝文が登録されていません。新規登録は `/宣伝文` から行ってください。", ephemeral=True)
    default_text = rs[0][0]
    await interaction.response.send_modal(AdTextModal(edit_mode=True, default_text=default_text))

@tree.command(name="設定", description="宣伝の投稿先チャンネルを設定します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@require_min_humans()
@app_commands.describe(channel="投稿先チャンネル（省略時は現在のチャンネル）")
async def cmd_set_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        return await interaction.response.send_message("❌ テキストチャンネルを指定してください。", ephemeral=True)
    await upsert_field(interaction.guild_id, "post_channel_id", target.id)
    await interaction.response.send_message(f"✅ 投稿先を {target.mention} に設定しました。", ephemeral=True)

@tree.command(name="宣伝文解除", description="このサーバーの宣伝文を削除します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_ad(interaction: discord.Interaction):
    await db_execute("UPDATE guilds SET ad_text = NULL WHERE guild_id = ?", (interaction.guild_id,))
    await asyncio.to_thread(lambda: _get_conn().commit())
    await interaction.response.send_message("✅ 宣伝文を削除しました。", ephemeral=True)

@tree.command(name="招待解除", description="このサーバーの招待リンクを削除します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_remove_invite(interaction: discord.Interaction):
    await db_execute("UPDATE guilds SET invite_url = NULL WHERE guild_id = ?", (interaction.guild_id,))
    await asyncio.to_thread(lambda: _get_conn().commit())
    await interaction.response.send_message("✅ 招待リンクを削除しました。", ephemeral=True)

# ============ 確認用コマンド（管理者限定） ============
@tree.command(name="宣伝確認", description="現在設定されている宣伝文と招待リンクを確認します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_check_ad(interaction: discord.Interaction):
    rs = await db_execute("SELECT ad_text, invite_url FROM guilds WHERE guild_id = ?", (interaction.guild_id,))
    if not rs:
        return await interaction.response.send_message("❌ まだ何も設定されていません。", ephemeral=True)
    ad_text, invite_url = rs[0]
    if not ad_text and not invite_url:
        return await interaction.response.send_message("❌ 宣伝文も招待リンクも設定されていません。", ephemeral=True)
    embed = discord.Embed(title="📋 現在の設定確認", color=discord.Color.green())
    embed.add_field(name="宣伝文", value=ad_text if ad_text else "未設定", inline=False)
    embed.add_field(name="招待リンク", value=invite_url if invite_url else "未設定", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="場所確認", description="宣伝が投稿されるチャンネルを確認します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_check_channel(interaction: discord.Interaction):
    rs = await db_execute("SELECT post_channel_id FROM guilds WHERE guild_id = ?", (interaction.guild_id,))
    if not rs or not rs[0][0]:
        return await interaction.response.send_message("❌ 投稿先チャンネルが設定されていません。`/設定` で設定してください。", ephemeral=True)
    channel_id = rs[0][0]
    channel = bot.get_channel(channel_id)
    if channel:
        await interaction.response.send_message(f"📌 現在の投稿先チャンネルは {channel.mention} です。", ephemeral=True)
    else:
        await interaction.response.send_message("⚠️ 投稿先チャンネルが設定されていますが、チャンネルが見つかりません（削除された可能性があります）。再設定してください。", ephemeral=True)

@tree.command(name="テスト送信", description="定時投稿の処理を手動で実行します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_test_post(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await daily_ad_post()
        await interaction.followup.send("✅ テスト送信を実行しました。ログを確認してください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)

# ============ ヘルプコマンド（管理者限定） ============
@tree.command(name="ヘルプ", description="使用できるコマンドの一覧を表示します（管理者専用）")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 コマンド一覧",
        description="鯖宣伝BOTで使用できるコマンドの一覧です（すべて管理者専用）。",
        color=discord.Color.purple()
    )
    embed.add_field(name="/招待 [チャンネル]", value=f"招待リンクを新規登録します（人間{MIN_HUMANS}人以上のサーバー限定）。", inline=False)
    embed.add_field(name="/招待編集 [チャンネル]", value="登録済みの招待リンクを新しいものに差し替えます。", inline=False)
    embed.add_field(name="/宣伝文", value=f"宣伝文を新規登録します（人間{MIN_HUMANS}人以上のサーバー限定）。", inline=False)
    embed.add_field(name="/宣伝文編集", value="登録済みの宣伝文を編集します。", inline=False)
    embed.add_field(name="/設定 [チャンネル]", value=f"宣伝の投稿先チャンネルを設定します（人間{MIN_HUMANS}人以上のサーバー限定）。", inline=False)
    embed.add_field(name="/宣伝確認", value="現在設定されている宣伝文と招待リンクを確認します。", inline=False)
    embed.add_field(name="/場所確認", value="宣伝が投稿されるチャンネルを確認します。", inline=False)
    embed.add_field(name="/宣伝文解除", value="このサーバーの宣伝文を削除します。", inline=False)
    embed.add_field(name="/招待解除", value="このサーバーの招待リンクを削除します。", inline=False)
    embed.set_footer(text=f"※ すべて管理者専用。登録系コマンドは人間{MIN_HUMANS}人以上のサーバーでのみ実行可能です。")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============ エラーハンドリング ============
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ このコマンドを実行するには「管理者」権限が必要です。",
            ephemeral=True
        )
    elif isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(str(error), ephemeral=True)
    else:
        print(f"[Command Error] {error}")
        try:
            await interaction.response.send_message(
                "⚠️ コマンドの実行中にエラーが発生しました。",
                ephemeral=True
            )
        except discord.InteractionResponded:
            pass

# ============ 定時投稿（毎日1回） ============
@tasks.loop(time=dt_time(hour=POST_HOUR, minute=0, tzinfo=JST))
async def daily_ad_post():
    try:
        targets = await db_execute("SELECT guild_id, post_channel_id FROM guilds WHERE post_channel_id IS NOT NULL")
        ads = await db_execute("SELECT guild_id, ad_text, invite_url FROM guilds WHERE ad_text IS NOT NULL AND invite_url IS NOT NULL")

        if not ads:
            return print("[daily_ad_post] 宣伝可能な広告がまだありません")

        for guild_id, post_channel_id in targets:
            candidates = [a for a in ads if a[0] != guild_id]
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

    await bot.change_presence(
        activity=discord.Game(name="サーバーを宣伝中 | /ヘルプ")
    )


    print(f"ログイン: {bot.user}")
    try:
        await db_init()
        print("DB初期化完了")
    except Exception as e:
        return print(f"DB初期化エラー: {e}")

    try:
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

# ============ ヘルスチェック ============
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
