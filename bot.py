import os
import discord
from discord.ext import commands
from flask import Flask
import threading

# --- BOTの設定 ---
intents = discord.Intents.default()
intents.message_content = True  # メッセージを読むために必須
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'ログインしました: {bot.user}')

@bot.command()
async def hello(ctx):
    """!hello と送ると挨拶を返します"""
    await ctx.send(f'こんにちは、{ctx.author.name}さん！')

# --- ヘルスチェック用のWebサーバー ---
# RenderのWeb Serviceとして動かすため、HTTPリクエストに応答するサーバーを別スレッドで起動します。
app = Flask(__name__)

@app.route('/')
def health_check():
    return "OK", 200

def run_web_server():
    # Renderは'PORT'環境変数でポート番号を指定してくるため、それを使います。
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# Webサーバーをバックグラウンドで開始
threading.Thread(target=run_web_server, daemon=True).start()

# --- BOTを起動 ---
bot.run(os.environ['DISCORD_TOKEN'])