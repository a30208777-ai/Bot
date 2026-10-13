"""
Bot anti-briga - Blackzin YT server

Funções:
  - Detecta brigas (palavras, siglas, ofensas e palavras cortadas com "-") em TODOS os membros,
    menos quem tem cargo de moderador/gerente (ou permissões de moderação)
  - Aplica timeout, manda o log no canal "log" e avisa em #briga / #🤬brigas
  - Comandos: /add palavra, /remover palavra, /lista
  - A lista de palavras fica salva numa mensagem do canal de log
    (NÃO apague essa mensagem, senão a lista volta ao padrão)

Instalação:  pip install -U discord.py
Token:       variável de ambiente DISCORD_TOKEN
"""

import json
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands

# ================== CONFIGURAÇÃO ==================
TOKEN = os.getenv("DISCORD_TOKEN", "COLOQUE_SEU_TOKEN_AQUI")

# Canais onde o bot avisa (nome só precisa CONTER o texto)
TRECHOS_CANAIS_BRIGAS = ["briga"]

# IMUNIDADE: o bot olha TODOS os cargos de quem falou. Se qualquer um deles
# tiver um destes trechos no nome (ignora acento/maiúscula), a pessoa é imune.
CARGOS_IMUNES = ["moderador", "moderadora", "gerente"]
CARGOS_IMUNES_EXATOS = {"mod", "mods", "adm"}   # só valem como palavra inteira
# Também é imune quem tem algum cargo com permissão de moderação
# (administrador, gerenciar servidor, banir, expulsar, silenciar, apagar mensagens).

# Banimento: 0 = nunca bane (só castiga com timeout).
# Ex.: 3 = bane na 3ª infração (a contagem zera se o Render reiniciar).
BANIR_APOS = 0

NOME_CANAL_LOG = "log"       # canal cujo nome contém "log"
DURACAO_MINUTOS = 10
MOTIVO = "falou algo errado"

# Lista inicial. Depois use /add e /remover no Discord.
# Regra: palavra sem espaço com até 4 letras (siglas como vsf, fdp)
# só vale como palavra inteira, pra não pegar dentro de outras palavras.
PALAVRAS_PADRAO = [
    "idiota", "burro", "otario", "lixo", "imbecil", "babaca",
    "cala a boca", "cala boca", "te odeio", "vou te pegar",
    "retardado", "inutil", "arrombado", "viado", "gayzinho", "gayzao",
    # siglas
    "vsf", "fdp", "pqp", "tnc", "krl", "vtnc", "vtmnc",
]

# "gay" usado como ofensa contra alguém ("vc é gay", "seu gay", "que gay").
# Só a palavra solta NÃO é punida, pra não punir quem fala de si mesmo.
PADROES_OFENSA = [
    re.compile(r"\b(vc|voce|tu|ce|seu|sua|que)\s+(e\s+|eh\s+)?(um\s+|uma\s+)?gay"),
    re.compile(r"\bgay\s+(de merda|idiota|burro|lixo)"),
]

MARCADOR_LISTA = "📋 PALAVRAS_BLOQUEADAS"
# ===================================================


def normalizar(texto: str) -> str:
    """minúsculo e sem acento."""
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip()


palavras = {normalizar(p) for p in PALAVRAS_PADRAO}
mensagem_lista = {}  # guild_id -> mensagem onde a lista fica salva


# Palavra cortada com "-" no fim (ex.: "seu ga-", "vc é idio-") também conta
# como xingamento. Dois jeitos de pegar:
#  1) depois de "seu/sua/vc/você/tu/que" -> qualquer pedaço cortado
#  2) pedaço cortado que é o começo de uma palavra da lista (ex.: "idio-")
RX_CORTADA_COM_INICIO = re.compile(
    r"\b(?:vc|voce|tu|ce|seu|sua|que)\s+(?:(?:e|eh|um|uma)\s+)*([a-z]{2,})-(?=\s|$|[.,!?;:])")
RX_CORTADA = re.compile(r"\b([a-z]{2,})-(?=\s|$|[.,!?;:])")
# começos de palavras normais que costumam aparecer com hífen
IGNORAR_FRAGMENTOS = {"in", "ex", "re", "co", "pre", "pos", "sub", "anti", "semi", "mega", "ultra"}


def palavra_cortada(t: str) -> bool:
    for m in RX_CORTADA_COM_INICIO.finditer(t):
        if m.group(1) not in IGNORAR_FRAGMENTOS:
            return True
    for m in RX_CORTADA.finditer(t):
        frag = m.group(1)
        if frag in IGNORAR_FRAGMENTOS:
            continue
        if any(" " not in p and len(p) > len(frag) and p.startswith(frag) for p in palavras):
            return True
    return False


def tem_briga(texto: str) -> bool:
    t = normalizar(texto).replace("–", "-").replace("—", "-")
    tokens = set(re.findall(r"[a-z0-9]+", t))
    for p in palavras:
        if " " not in p and len(p) <= 4:
            if p in tokens:          # sigla: palavra inteira
                return True
        elif p in t:                 # palavra/frase: aparece em qualquer lugar
            return True
    if any(rx.search(t) for rx in PADROES_OFENSA):
        return True
    return palavra_cortada(t)


# ---------------- bot e canais ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        try:
            await self.tree.sync()
        except discord.HTTPException as e:
            print(f"⚠️ Não consegui registrar os comandos (/add, /remover): {e}")


client = Bot()


def achar_canal_log(guild: discord.Guild):
    for c in guild.text_channels:
        if NOME_CANAL_LOG in normalizar(c.name):
            return c
    return None


def achar_canais_brigas(guild: discord.Guild):
    return [
        c for c in guild.text_channels
        if any(t in normalizar(c.name) for t in TRECHOS_CANAIS_BRIGAS)
    ]


infracoes = {}  # user_id -> quantidade de infrações (zera ao reiniciar)


def eh_imune(membro: discord.Member) -> bool:
    """Imune = dono, ou qualquer cargo de moderador/gerente/com permissão de moderação."""
    if membro.id == membro.guild.owner_id:
        return True
    for r in membro.roles:                      # olha TODOS os cargos
        if r.is_default():
            continue
        perm = r.permissions
        if (perm.administrator or perm.manage_guild or perm.ban_members
                or perm.kick_members or perm.moderate_members or perm.manage_messages):
            return True
        nome = normalizar(r.name)
        if any(t in nome for t in CARGOS_IMUNES):
            return True
        if CARGOS_IMUNES_EXATOS & set(re.findall(r"[a-z0-9]+", nome)):
            return True
    return False


# ---------------- lista salva no canal de log ----------------
async def carregar_lista(guild: discord.Guild):
    canal = achar_canal_log(guild)
    if not canal:
        return
    async for m in canal.history(limit=200):
        if m.author == client.user and m.content.startswith(MARCADOR_LISTA):
            try:
                bruto = m.content.split("```json")[1].split("```")[0]
                palavras.clear()
                palavras.update(normalizar(p) for p in json.loads(bruto))
                mensagem_lista[guild.id] = m
                print(f"📋 Lista carregada: {len(palavras)} palavras")
            except Exception as e:
                print(f"⚠️ Não consegui ler a lista salva: {e}")
            return


async def salvar_lista(guild: discord.Guild):
    canal = achar_canal_log(guild)
    if not canal:
        return False
    texto = f"{MARCADOR_LISTA}\n```json\n{json.dumps(sorted(palavras), ensure_ascii=False)}\n```"
    if len(texto) > 1990:
        return False
    msg = mensagem_lista.get(guild.id)
    try:
        try:
            if msg:
                await msg.edit(content=texto)
            else:
                mensagem_lista[guild.id] = await canal.send(texto)
        except discord.NotFound:
            mensagem_lista[guild.id] = await canal.send(texto)
    except discord.HTTPException as e:
        print(f"⚠️ Não consegui salvar a lista no canal {canal.name}: {e}")
        return False
    return True


# ---------------- comandos ----------------
def pode_gerenciar(interaction: discord.Interaction) -> bool:
    p = interaction.user.guild_permissions
    return p.administrator or p.moderate_members


@client.tree.command(name="add", description="Adiciona uma palavra ou sigla proibida")
@app_commands.describe(palavra="Palavra, sigla (3+ letras) ou frase")
@app_commands.guild_only()
async def add(interaction: discord.Interaction, palavra: str):
    if not pode_gerenciar(interaction):
        return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
    p = normalizar(palavra)
    if len(p) < 3:
        return await interaction.response.send_message(
            "❌ Use pelo menos 3 letras.", ephemeral=True)
    if p in palavras:
        return await interaction.response.send_message("⚠️ Essa já está na lista.", ephemeral=True)
    palavras.add(p)
    ok = await salvar_lista(interaction.guild)
    aviso = "" if ok else "\n⚠️ Não consegui salvar no canal de log (confira se ele existe)."
    await interaction.response.send_message(f"✅ Adicionei: **{p}**{aviso}", ephemeral=True)


@client.tree.command(name="remover", description="Remove uma palavra ou sigla da lista")
@app_commands.describe(palavra="Palavra ou sigla para remover")
@app_commands.guild_only()
async def remover(interaction: discord.Interaction, palavra: str):
    if not pode_gerenciar(interaction):
        return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
    p = normalizar(palavra)
    if p not in palavras:
        return await interaction.response.send_message("⚠️ Essa não está na lista.", ephemeral=True)
    palavras.discard(p)
    ok = await salvar_lista(interaction.guild)
    aviso = "" if ok else "\n⚠️ Não consegui salvar no canal de log."
    await interaction.response.send_message(f"✅ Removi: **{p}**{aviso}", ephemeral=True)


@client.tree.command(name="lista", description="Mostra as palavras proibidas")
@app_commands.guild_only()
async def lista(interaction: discord.Interaction):
    if not pode_gerenciar(interaction):
        return await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
    texto = ", ".join(sorted(palavras)) or "(vazia)"
    await interaction.response.send_message(f"📋 {texto}"[:1900], ephemeral=True)


async def enviar(canal, texto: str):
    try:
        await canal.send(texto)
    except discord.Forbidden:
        print(f"⚠️ Sem acesso ao canal #{canal.name}: dê ao bot Ver Canal e Enviar Mensagens nele.")
    except discord.HTTPException as e:
        print(f"⚠️ Erro ao enviar em #{canal.name}: {e}")


# ---------------- eventos ----------------
@client.event
async def on_ready():
    for g in client.guilds:
        try:
            await carregar_lista(g)
        except discord.Forbidden:
            print("⚠️ Sem acesso ao canal de log (dê ao bot: Ver Canal + Ler Histórico + Enviar Mensagens).")
    print(f"✅ Bot online como {client.user}")


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot or msg.guild is None:
        return

    membro = msg.author

    # moderadores/gerentes são imunes (qualquer cargo conta)
    if eh_imune(membro):
        return

    if not tem_briga(msg.content):
        return

    # 1) timeout
    try:
        await membro.timeout(
            timedelta(minutes=DURACAO_MINUTOS),
            reason=f"{MOTIVO} | fala: {msg.content[:100]}",
        )
        sucesso = True
    except discord.Forbidden:
        sucesso = False
        print("⚠️ Sem permissão (cargo do bot abaixo do usuário ou falta Moderate Members).")
    except discord.HTTPException as e:
        sucesso = False
        print(f"⚠️ Erro ao aplicar timeout: {e}")

    # 1b) banimento por reincidência (se BANIR_APOS > 0)
    banido = False
    infracoes[membro.id] = infracoes.get(membro.id, 0) + 1
    if BANIR_APOS and infracoes[membro.id] >= BANIR_APOS:
        try:
            await membro.ban(reason=f"{MOTIVO} ({infracoes[membro.id]} infrações)")
            banido = True
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"⚠️ Não consegui banir: {e}")

    # 2) log no canal de log
    linha = (
        f"/timeout user:{membro.name} duration:{DURACAO_MINUTOS} "
        f"reason:{MOTIVO} fala:({msg.content})"
    )
    canal_log = achar_canal_log(msg.guild)
    if canal_log:
        status = "✅ aplicado" if sucesso else "⚠️ falhou (sem permissão)"
        if banido:
            status += " • 🔨 banido"
        await enviar(canal_log, f"`{linha}`\n{status} • {datetime.now():%d/%m/%Y %H:%M:%S}"[:1990])
    else:
        print(f"⚠️ Canal de log não encontrado. Log: {linha}")

    # 3) aviso em #briga e #🤬brigas
    for canal in achar_canais_brigas(msg.guild):
        await enviar(canal, f"{canal.mention} agora {membro.mention}")


# ============ Servidor web mínimo (para o Render) ============
class _Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"bot online")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def manter_vivo():
    porta = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", porta), _Ping).serve_forever()


threading.Thread(target=manter_vivo, daemon=True).start()
# ==============================================================

client.run(TOKEN)
              
