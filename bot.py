"""
Bot anti-briga - Blackzin YT server

Funções:
  - Detecta brigas (palavras, siglas, ofensas e palavras cortadas com "-") em TODOS os membros,
    menos quem tem cargo de moderador/gerente (ou permissões de moderação)
  - Apaga a mensagem e pede para a pessoa ir para #briga / #🤬brigas (até 4 avisos);
    no 5º xingamento aplica timeout de 10 min e manda o log no canal "log"
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
import time
import urllib.request
import unicodedata
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands

# ================== CONFIGURAÇÃO ==================
TOKEN = os.getenv("DISCORD_TOKEN", "").strip().strip("\"'")
if len(TOKEN) < 50:
    print("❌ DISCORD_TOKEN vazio ou errado. No Render: Environment > DISCORD_TOKEN = "
          "token do bot (Portal do Discord > Bot > Redefinir token). "
          "Não é o ID do aplicativo nem a chave pública.")

# Canais onde o bot avisa (nome só precisa CONTER o texto)
TRECHOS_CANAIS_BRIGAS = ["briga"]

# IMUNIDADE: o bot olha TODOS os cargos de quem falou. Se qualquer um deles
# tiver um destes trechos no nome (ignora acento/maiúscula), a pessoa é imune.
CARGOS_IMUNES = ["moderador", "moderadora", "gerente"]
CARGOS_IMUNES_EXATOS = {"mod", "mods", "adm"}   # só valem como palavra inteira
# Também é imune quem tem algum cargo com permissão de moderação
# (administrador, gerenciar servidor, banir, expulsar, silenciar, apagar mensagens).

# Banimento: 0 = nunca bane (só castiga com timeout).
# Ex.: 3 = bane no 3º castigo (a contagem zera se o Render reiniciar).
BANIR_APOS = 0

# Só estes usuários do Discord (nome de usuário único, sem @) usam os comandos
# /add /remover /lista /ban /unban /verbans
DONOS_COMANDOS = {
    "arthurdevpromax",
    "frezennew_ofc",
    "pedrotvzin72_25",
    "vinicius3774235",
    "felpzsouls",
}

# Banidos AUTOMATICAMENTE assim que entrarem no servidor.
# Pode ser o nome de usuário (sem @) ou o ID numérico (o ID é mais seguro,
# porque o nome de usuário pode ser trocado).
BANIR_AO_ENTRAR = {"iamantientrosa", "warlenking0005"}
MOTIVO_BAN_ENTRADA = (
    "Raid: vocês fizeram raids com seu bot, que é proibido. "
    "Vocês não são bem-vindos nesse servidor nunca mais."
)

NOME_CANAL_LOG = "log"       # canal cujo nome contém "log"
DURACAO_MINUTOS = 10
MOTIVO = "não seguir o que estava sendo mandado e xingar no servidor"

# Quantas vezes o bot PEDE para a pessoa ir para o canal de briga antes de castigar.
# Aviso 1, 2, 3 e 4 = só pede (e apaga a mensagem). No 5º xingamento = castigo.
LIMITE_AVISOS = 4
# Xingar DENTRO de #briga / #🤬brigas não conta (é pra lá que a pessoa é mandada).
PERMITIR_NOS_CANAIS_BRIGA = True

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


infracoes = {}  # user_id -> quantidade de CASTIGOS (zera ao reiniciar)
avisos = {}      # user_id -> quantos avisos já levou (zera ao reiniciar)
historico = {}   # user_id -> últimas falas proibidas (usadas no motivo do /ban)


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
    """Só quem está em DONOS_COMANDOS (compara o nome de usuário, que é único)."""
    donos = {normalizar(n) for n in DONOS_COMANDOS}
    return normalizar(interaction.user.name) in donos


NEGADO = "❌ Você não tem permissão para usar este comando."


@client.tree.command(name="add", description="Adiciona uma palavra ou sigla proibida")
@app_commands.describe(palavra="Palavra, sigla (3+ letras) ou frase")
@app_commands.guild_only()
async def add(interaction: discord.Interaction, palavra: str):
    if not pode_gerenciar(interaction):
        return await interaction.response.send_message(NEGADO, ephemeral=True)
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
        return await interaction.response.send_message(NEGADO, ephemeral=True)
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
        return await interaction.response.send_message(NEGADO, ephemeral=True)
    texto = ", ".join(sorted(palavras)) or "(vazia)"
    await interaction.response.send_message(f"📋 {texto}"[:1900], ephemeral=True)


@client.tree.command(name="list", description="Mostra as palavras proibidas (igual a /lista)")
@app_commands.guild_only()
async def list_(interaction: discord.Interaction):
    await lista.callback(interaction)


@client.tree.command(name="remove", description="Remove uma palavra da lista (igual a /remover)")
@app_commands.describe(palavra="Palavra ou sigla para remover")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, palavra: str):
    await remover.callback(interaction, palavra)


@client.tree.command(name="ban", description="Bane um usuário do servidor")
@app_commands.describe(
    user="Quem banir (escolha na lista ou cole o ID)",
    motivo="Motivo. Se vazio: 'fala indevida de' + todas as falas registradas",
)
@app_commands.guild_only()
async def ban(interaction: discord.Interaction, user: discord.User, motivo: str = None):
    if not pode_gerenciar(interaction):
        return await interaction.response.send_message(NEGADO, ephemeral=True)
    if user.id in (interaction.user.id, client.user.id):
        return await interaction.response.send_message("❌ Escolha outra pessoa.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    falas = historico.get(user.id, [])
    if not motivo:
        motivo = "fala indevida" + (" de: " + " | ".join(falas) if falas else "")
    motivo = motivo[:500]   # o Discord aceita até 512 caracteres

    try:
        await interaction.guild.ban(user, reason=motivo)
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ Não consegui banir. O bot precisa da permissão **Banir Membros** "
            "e do cargo acima do cargo da pessoa.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"❌ Erro ao banir: {e}", ephemeral=True)

    for d in (historico, avisos, infracoes):
        d.pop(user.id, None)
    canal_log = achar_canal_log(interaction.guild)
    if canal_log:
        await enviar(canal_log, f"`/ban user:{user.name} motivo:{motivo}`\n"
                                f"🔨 banido por {interaction.user.name} • "
                                f"{datetime.now():%d/%m/%Y %H:%M:%S}"[:1990])
    await interaction.followup.send(f"🔨 Banido: **{user.name}**\nMotivo: {motivo}", ephemeral=True)


@client.tree.command(name="unban", description="Desbane um usuário")
@app_commands.describe(user="Quem desbanir (escolha na lista ou cole o ID)")
@app_commands.guild_only()
async def unban(interaction: discord.Interaction, user: discord.User):
    if not pode_gerenciar(interaction):
        return await interaction.response.send_message(NEGADO, ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.guild.unban(user, reason=f"unban por {interaction.user.name}")
    except discord.NotFound:
        return await interaction.followup.send("⚠️ Essa pessoa não está banida.", ephemeral=True)
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ Não consegui desbanir. O bot precisa da permissão **Banir Membros**.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"❌ Erro ao desbanir: {e}", ephemeral=True)

    canal_log = achar_canal_log(interaction.guild)
    if canal_log:
        await enviar(canal_log, f"`/unban user:{user.name}`\n"
                                f"✅ desbanido por {interaction.user.name} • "
                                f"{datetime.now():%d/%m/%Y %H:%M:%S}"[:1990])
    await interaction.followup.send(f"✅ Desbanido: **{user.name}**", ephemeral=True)


@client.tree.command(name="verbans", description="Mostra quem está banido e o motivo")
@app_commands.guild_only()
async def verbans(interaction: discord.Interaction):
    if not pode_gerenciar(interaction):
        return await interaction.response.send_message(NEGADO, ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    linhas = []
    try:
        async for e in interaction.guild.bans(limit=200):
            linhas.append(f"• **{e.user.name}** ({e.user.id}) — {e.reason or 'sem motivo'}")
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ O bot precisa da permissão **Banir Membros** para ver a lista.", ephemeral=True)
    if not linhas:
        return await interaction.followup.send("✅ Ninguém está banido.", ephemeral=True)

    pedacos, atual = [], f"🔨 {len(linhas)} banido(s):\n"
    for l in linhas:
        l = l[:400]
        if len(atual) + len(l) + 1 > 1900:
            pedacos.append(atual)
            atual = ""
        atual += l + "\n"
    pedacos.append(atual)
    for parte in pedacos[:6]:
        await interaction.followup.send(parte, ephemeral=True)


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
async def on_member_join(membro: discord.Member):
    alvos = {normalizar(n) for n in BANIR_AO_ENTRAR}
    if normalizar(membro.name) not in alvos and str(membro.id) not in alvos:
        return

    try:                                   # tenta avisar por mensagem privada
        await membro.send(f"⛔ {MOTIVO_BAN_ENTRADA}")
    except (discord.Forbidden, discord.HTTPException):
        pass                               # DM fechada: segue o baile

    try:
        await membro.guild.ban(membro, reason=MOTIVO_BAN_ENTRADA[:500])
        banido = True
    except (discord.Forbidden, discord.HTTPException) as e:
        banido = False
        print(f"⚠️ Não consegui banir {membro.name} ao entrar: {e}")

    canal_log = achar_canal_log(membro.guild)
    if canal_log:
        status = ("🔨 banido automaticamente ao entrar" if banido
                  else "⚠️ falhou (o bot precisa de Banir Membros e cargo acima)")
        await enviar(canal_log, f"`/ban user:{membro.name} motivo:{MOTIVO_BAN_ENTRADA}`\n"
                                f"{status} • {datetime.now():%d/%m/%Y %H:%M:%S}"[:1990])


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot or msg.guild is None:
        return

    membro = msg.author

    if not tem_briga(msg.content):
        return

    canais_brigas = achar_canais_brigas(msg.guild)

    # xingar dentro dos canais de briga é permitido
    if PERMITIR_NOS_CANAIS_BRIGA and msg.channel in canais_brigas:
        return

    # moderadores/gerentes são imunes (qualquer cargo conta)
    if eh_imune(membro):
        print(f"ℹ️ {membro} falou algo proibido, mas é imune (cargo de moderação).")
        return

    # guarda a fala (vira o motivo do /ban)
    falas = historico.setdefault(membro.id, [])
    falas.append(msg.content[:80])
    del falas[:-15]

    # apaga a mensagem
    apagada = False
    try:
        await msg.delete()
        apagada = True
    except discord.Forbidden:
        print("⚠️ Não consegui apagar a mensagem: dê ao bot a permissão Gerenciar Mensagens.")
    except discord.HTTPException as e:
        print(f"⚠️ Erro ao apagar a mensagem: {e}")

    mencoes = " ou ".join(c.mention for c in canais_brigas) or "#brigas"
    canal_log = achar_canal_log(msg.guild)
    agora = f"{datetime.now():%d/%m/%Y %H:%M:%S}"
    status_msg = "🗑️ msg apagada" if apagada else "⚠️ msg não apagada"

    # ---------- AVISO: ainda dentro do limite ----------
    n = avisos.get(membro.id, 0)
    if n < LIMITE_AVISOS:
        n += 1
        avisos[membro.id] = n
        await enviar(msg.channel, f"{mencoes} agora {membro.mention} (aviso {n}/{LIMITE_AVISOS})")
        for c in canais_brigas:   # e também dentro dos canais de briga
            await enviar(c, f"{c.mention} agora {membro.mention} "
                            f"(xingou em {msg.channel.mention} • aviso {n}/{LIMITE_AVISOS})")
        linha = f"/aviso user:{membro.name} aviso:{n}/{LIMITE_AVISOS} fala:({msg.content})"
        if canal_log:
            await enviar(canal_log, f"`{linha}`\n⚠️ só avisou • {status_msg} • {agora}"[:1990])
        else:
            print(f"⚠️ Canal de log não encontrado. Log: {linha}")
        return

    # ---------- CASTIGO: passou do limite de avisos ----------
    avisos[membro.id] = 0   # recomeça a contagem depois do castigo

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

    if sucesso:
        await enviar(
            msg.channel,
            f"{membro.mention} levou {DURACAO_MINUTOS} minutos de castigo por {MOTIVO}.",
        )
        for c in canais_brigas:
            await enviar(c, f"{membro.mention} levou {DURACAO_MINUTOS} minutos de castigo "
                            f"por {MOTIVO} (em {msg.channel.mention}).")

    # banimento por reincidência (se BANIR_APOS > 0)
    banido = False
    infracoes[membro.id] = infracoes.get(membro.id, 0) + 1
    if BANIR_APOS and infracoes[membro.id] >= BANIR_APOS:
        try:
            await membro.ban(reason=f"{MOTIVO} ({infracoes[membro.id]} castigos)")
            banido = True
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"⚠️ Não consegui banir: {e}")

    linha = (
        f"/timeout user:{membro.name} duration:{DURACAO_MINUTOS} "
        f"reason:{MOTIVO} fala:({msg.content})"
    )
    if canal_log:
        status = "✅ aplicado" if sucesso else "⚠️ falhou (sem permissão)"
        if banido:
            status += " • 🔨 banido"
        await enviar(canal_log, f"`{linha}`\n{status} • {status_msg} • {agora}"[:1990])
    else:
        print(f"⚠️ Canal de log não encontrado. Log: {linha}")


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


# ---- Auto-ping: o bot "entra e sai" da própria URL a cada 15 segundos ----
# O Render coloca a URL pública do serviço em RENDER_EXTERNAL_URL.
PING_URL = os.getenv("RENDER_EXTERNAL_URL", "https://bot-1-efe3.onrender.com/")
PING_INTERVALO = 15   # segundos


def auto_ping():
    falhou = False
    while True:
        time.sleep(PING_INTERVALO)
        try:
            urllib.request.urlopen(PING_URL, timeout=10).read()
            if falhou:
                print("✅ Auto-ping voltou a funcionar")
                falhou = False
        except Exception as e:
            if not falhou:               # avisa só na 1ª falha, sem encher o log
                print(f"⚠️ Auto-ping falhou: {e}")
                falhou = True


threading.Thread(target=auto_ping, daemon=True).start()
# ==============================================================

client.run(TOKEN)
