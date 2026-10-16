"""Bot do Discord: loop de caça + slash commands."""

import asyncio
import datetime as dt
import io
import logging

import discord
from discord import app_commands
from discord.ext import tasks

from scentinel import atualizar, autocatalogo, busca, catalogo, config, db, formato, grafico, navegador, promo
from scentinel.descoberta import Descoberta
from scentinel.fontes import montar_fontes
from scentinel.caca import Cacador

log = logging.getLogger("scentinel")


# ver canais + mandar mensagem + gerenciar mensagens (/clear) + embed + ler histórico (/clear) + mencionar @everyone
# + gerenciar cargos (/cargos)
PERMISSOES = 1024 | 2048 | 8192 | 16384 | 65536 | 131072 | 268435456

# Cargos de estilo (fileira de cima do /cargos) + a partir de quantos % OFF marcar (fileira de baixo).
# O nome do cargo é o rótulo do estilo. Quem marca é o bot, pessoa por pessoa, com o que ela escolheu nos botões
# (o Discord não sabe marcar "quem tem o cargo Árabe E quer 30%+").
CARGO_TODAS = "todas"
CARGOS = {**catalogo.CATEGORIAS, CARGO_TODAS: "🔥 Todas as promos"}
PCTS = (10, 20, 30, 40, 50)
PCT_PADRAO = 20  # sem escolher a %: a partir de 20% OFF (onde o bot já chama de promo)
PING_INTERVALO = dt.timedelta(minutes=30)  # cada pessoa no máximo 1 marcação a cada 30 min (promo maior passa)
PING_MESMO_ANUNCIO = dt.timedelta(hours=24)  # a vitrine pode repostar a promo: não marca de novo por ela
MAX_MENCOES = 80  # a mensagem do Discord tem 2000 caracteres


def link_convite(application_id):
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot+applications.commands&permissions={PERMISSOES}"
    )


def montar_mencao(mencao, dono_id, pode_everyone=True):
    """Texto e menções permitidas do alerta.
    mencao: None, promo.MENCAO_DONO ou promo.MENCAO_EVERYONE.
    O dono vai junto no @everyone: quem silencia @everyone no servidor ainda é avisado.
    Sem a permissão de @everyone, marca pelo menos o dono."""
    if mencao == promo.MENCAO_EVERYONE:
        if pode_everyone:
            return f"@everyone <@{dono_id}>", discord.AllowedMentions(everyone=True, users=True, roles=False)
        return f"@everyone <@{dono_id}>", discord.AllowedMentions(everyone=False, users=True, roles=False)
    if mencao == promo.MENCAO_DONO:
        return f"<@{dono_id}>", discord.AllowedMentions(everyone=False, users=True, roles=False)
    return None, discord.AllowedMentions.none()


def horario_top(texto, fuso_nome):
    """'19:00' -> time(19, 0, tz=São Paulo). Vazio ou inválido -> None (top desligado)."""
    try:
        hora, minuto = (int(x) for x in str(texto).strip().split(":"))
        return dt.time(hora, minuto, tzinfo=formato.fuso(fuso_nome))
    except (ValueError, TypeError):
        return None


def horarios_vitrine(minutos, fuso_nome):
    """Horários redondos da vitrine: a cada 15 min -> 00:00, 00:15, 00:30... (no fuso do config)."""
    passo = max(1, int(round(minutos)))
    tz = formato.fuso(fuso_nome)
    return [dt.time(m // 60, m % 60, tzinfo=tz) for m in range(0, 24 * 60, passo)]


def estilos_do_membro(membro):
    """Chaves dos estilos que a pessoa tem como cargo agora."""
    nomes = {getattr(c, "name", None) for c in getattr(membro, "roles", [])}
    return [chave for chave, nome in CARGOS.items() if nome in nomes]


def texto_preferencia(estilos, pct):
    if not estilos:
        return ("🔕 Você não está em nenhum estilo agora: não te marco. Escolhe um na fileira de cima "
                "(ou 🔥 Todas as promos).")
    nomes = ", ".join(CARGOS[e] for e in CARGOS if e in estilos)
    return (f"🔔 Te marco em promo de **{pct}% OFF ou mais** de: {nomes}.\n"
            "Estilo: fileira de cima (clica de novo pra sair) · %: fileira de baixo.")


def quem_marcar(prefs, oferta, ultimos, agora, excluir=()):
    """IDs de quem pediu pra ser marcado nessa oferta: estilo dela (ou 🔥 Todas) e % OFF >= o mínimo da pessoa.
    Cada pessoa no máximo 1 vez a cada 30 min (promo maior que a da última marcação passa na frente) e nunca 2
    vezes pelo mesmo anúncio em 24 h. `ultimos` (id -> última marcação) é atualizado."""
    if oferta.origem != "descoberta" or not formato.tem_desconto(oferta):
        return []
    pct = oferta.avaliacao.pct
    ids = []
    for p in prefs:
        uid = p["usuario_id"]
        estilos = set(filter(None, (p["estilos"] or "").split(",")))
        if uid in excluir or (CARGO_TODAS not in estilos and oferta.estilo not in estilos):
            continue
        if pct < (p["pct_min"] or PCT_PADRAO):
            continue
        u = ultimos.setdefault(uid, {"quando": None, "pct": 0, "anuncios": {}})
        u["anuncios"] = {a: q for a, q in u["anuncios"].items() if agora - q < PING_MESMO_ANUNCIO}
        if oferta.anuncio_id in u["anuncios"]:
            continue
        if u["quando"] and agora - u["quando"] < PING_INTERVALO and pct <= u["pct"]:
            continue
        u.update(quando=agora, pct=pct)
        u["anuncios"][oferta.anuncio_id] = agora
        ids.append(uid)
        if len(ids) >= MAX_MENCOES:
            break
    return ids


async def alternar_cargo(interaction, chave):
    """Botão de estilo: tem o cargo -> tira; não tem -> dá. Guarda a escolha pra marcar certo."""
    nome = CARGOS[chave]
    cargo = discord.utils.get(interaction.guild.roles, name=nome) if interaction.guild else None
    if not cargo:
        await interaction.response.send_message("Esse cargo sumiu 🤔 Um admin pode recriar com `/cargos`.",
                                                ephemeral=True)
        return
    membro = interaction.user
    estilos = set(estilos_do_membro(membro))
    try:
        if any(c.id == cargo.id for c in getattr(membro, "roles", [])):
            await membro.remove_roles(cargo, reason="botão de cargos do Scentinel")
            estilos.discard(chave)
            texto = f"🔕 Tirei o **{nome}**.\n"
        else:
            await membro.add_roles(cargo, reason="botão de cargos do Scentinel")
            estilos.add(chave)
            texto = f"✅ Te dei o **{nome}**.\n"
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Me falta a permissão **Gerenciar cargos** (ou o meu cargo está abaixo desse no servidor). "
            "Avisa um admin.", ephemeral=True)
        return
    pref = db.salvar_preferencia(interaction.client.con, membro.id, estilos=estilos)
    await interaction.response.send_message(texto + texto_preferencia(estilos, pref["pct_min"]), ephemeral=True)


async def escolher_pct(interaction, pct):
    """Botão de %: a partir de quanto OFF marcar (os estilos vêm dos cargos que a pessoa já tem)."""
    estilos = estilos_do_membro(interaction.user)
    db.salvar_preferencia(interaction.client.con, interaction.user.id, estilos=estilos, pct_min=pct)
    await interaction.response.send_message(texto_preferencia(estilos, pct), ephemeral=True)


async def tirar_cargos(interaction):
    """Botão "tirar meus cargos": tira todos os cargos de estilo da pessoa e para de marcar ela."""
    membro = interaction.user
    nomes = set(CARGOS.values())
    meus = [c for c in getattr(membro, "roles", []) if getattr(c, "name", None) in nomes]
    try:
        if meus:
            await membro.remove_roles(*meus, reason="botão de cargos do Scentinel")
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Me falta a permissão **Gerenciar cargos** (ou o meu cargo está abaixo desses no servidor). "
            "Avisa um admin.", ephemeral=True)
        return
    db.salvar_preferencia(interaction.client.con, membro.id, estilos=[])
    texto = (f"🔕 Tirei {', '.join(c.name for c in meus)}. Não te marco mais em nada." if meus
             else "🔕 Você não tinha nenhum cargo de estilo. Não te marco em nada.")
    await interaction.response.send_message(texto, ephemeral=True)


class BotaoTirarCargos(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Tirar meus cargos", emoji="🔕", style=discord.ButtonStyle.danger,
                         custom_id="scentinel:cargo:tirar-todos", row=2)

    async def callback(self, interaction: discord.Interaction):
        await tirar_cargos(interaction)


class BotaoCargo(discord.ui.Button):
    def __init__(self, chave):
        super().__init__(label=CARGOS[chave], style=discord.ButtonStyle.secondary,
                         custom_id=f"scentinel:cargo:{chave}", row=0)
        self.chave = chave

    async def callback(self, interaction: discord.Interaction):
        await alternar_cargo(interaction, self.chave)


class BotaoPct(discord.ui.Button):
    def __init__(self, pct):
        super().__init__(label=f"{pct}%+ OFF", style=discord.ButtonStyle.primary,
                         custom_id=f"scentinel:pct:{pct}", row=1)
        self.pct = pct

    async def callback(self, interaction: discord.Interaction):
        await escolher_pct(interaction, self.pct)


class BotoesCargos(discord.ui.View):
    """Botões fixos (continuam funcionando depois que o bot reinicia: custom_id fixo + timeout=None).
    Fileira de cima: estilos. Fileira de baixo: a partir de quantos % OFF."""

    def __init__(self):
        super().__init__(timeout=None)
        for chave in CARGOS:
            self.add_item(BotaoCargo(chave))
        for pct in PCTS:
            self.add_item(BotaoPct(pct))
        self.add_item(BotaoTirarCargos())


def embed_cargos():
    return {
        "title": "🔔 Quer ser marcado nas promos?",
        "color": formato.COR_ALVO,
        "description": (
            "**1. Em cima, os estilos que você curte** (clica de novo pra sair):\n"
            "✨ **Designer**: Dior, Chanel, Armani, Paco Rabanne...\n"
            "💎 **Nicho**: Creed, Parfums de Marly, Xerjoff, Initio...\n"
            "🕌 **Árabe**: Lattafa, Armaf, Afnan, Al Haramain...\n"
            "🇧🇷 **Nacional**: O Boticário, Natura, Eudora...\n"
            "🔥 **Todas as promos**: qualquer estilo\n\n"
            f"**2. Embaixo, a partir de quantos % OFF** te marco (se não escolher: {PCT_PADRAO}%).\n"
            "🔕 **Tirar meus cargos**: sai de tudo de uma vez.\n\n"
            "Aí, quando sair post com esse desconto num estilo seu, eu te marco. No máximo 1 marcação a cada "
            "30 min (promo maior passa na frente), e nunca 2 vezes pelo mesmo anúncio."
        ),
    }


class BotScentinel(discord.Client):
    def __init__(self, cfg, segredos, con):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            # economiza RAM: o bot não precisa guardar membros nem mensagens
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.cfg = cfg
        self.segredos = segredos
        self.con = con
        self.reiniciar = False  # /atualizar: sai com erro de propósito pro systemd religar na versão nova
        self.versao = None  # commit rodando (o rodar() preenche ao ligar)
        self._ping_usuario = {}  # quem já foi marcado, quando e por qual anúncio (quem_marcar)
        self.trava_atualizar = asyncio.Lock()
        self.trava_autocatalogo = asyncio.Lock()
        self.tree = app_commands.CommandTree(self)
        self.cacador = Cacador(con, cfg, enviar_alerta=self.enviar_alerta, enviar_aviso=self.enviar_aviso)
        self.loop_caca.change_interval(minutes=cfg.tick_minutos)
        self.descoberta = None
        if cfg.descoberta.ativo:
            itens = catalogo.carregar_catalogo()
            itens += autocatalogo.itens_salvos({i.id for i in itens})  # o que o catálogo automático já pôs
            fontes = montar_fontes(cfg.descoberta.fontes)
            if itens and fontes:
                self.descoberta = Descoberta(self.cacador, itens, fontes, enviar_aviso=self.enviar_aviso)
                self.loop_descoberta.change_interval(minutes=cfg.tick_minutos)
                horario = horario_top(cfg.descoberta.top_diario, cfg.fuso)
                if horario:
                    self.loop_top.change_interval(time=horario)
                if cfg.descoberta.vitrine_minutos:
                    # em horário redondo (10:00, 10:15, 10:30...), não "15 min depois de ligar"
                    self.loop_vitrine.change_interval(time=horarios_vitrine(cfg.descoberta.vitrine_minutos, cfg.fuso))
                horario_auto = horario_top(cfg.descoberta.catalogo_auto, cfg.fuso)
                if horario_auto:
                    self.loop_autocatalogo.change_interval(time=horario_auto)

    # ------------------------------------------------------------
    # ciclo de vida
    # ------------------------------------------------------------

    async def setup_hook(self):
        registrar_comandos(self.tree)
        self.add_view(BotoesCargos())  # os botões do /cargos voltam a funcionar depois de reiniciar
        guild = discord.Object(id=self.segredos.guild_id)
        # sincroniza só no servidor do Davi: aparece na hora (global demora)
        self.tree.copy_global_to(guild=guild)
        try:
            await self.tree.sync(guild=guild)
        except discord.Forbidden:
            # 50001 Missing Access: o bot não está nesse servidor (ou entrou sem applications.commands)
            log.error(
                "❌ O bot não tem acesso ao servidor %s (GUILD_ID no .env).\n"
                "   Convide ele com este link e rode de novo:\n   %s",
                self.segredos.guild_id, link_convite(self.application_id),
            )
            raise SystemExit(1)
        self.loop_caca.start()
        if self.descoberta:
            d = self.descoberta
            log.info("🔎 Busca automática ligada: %d perfumes em %d loja(s)", len(d.itens), len(d.fontes))
            if any(getattr(f, "navegador", False) for f in d.fontes):
                # Chromium do /locate: baixa sozinho na 1ª vez (em segundo plano, não atrasa o bot)
                self._instalando_navegador = asyncio.create_task(asyncio.to_thread(navegador.garantir_instalado))
            self.loop_descoberta.start()
            self.loop_alertas.start()
            if horario_top(self.cfg.descoberta.top_diario, self.cfg.fuso):
                self.loop_top.start()
            if self.cfg.descoberta.vitrine_minutos:
                self.loop_vitrine.start()
            if horario_top(self.cfg.descoberta.catalogo_auto, self.cfg.fuso):
                self.loop_autocatalogo.start()

    async def on_ready(self):
        log.info("✅ Bot online como %s", self.user)

    # ------------------------------------------------------------
    # canal
    # ------------------------------------------------------------

    async def canal(self):
        canal = self.get_channel(self.segredos.canal_alertas_id)
        if canal is None:
            canal = await self.fetch_channel(self.segredos.canal_alertas_id)
        return canal

    def pode_marcar_everyone(self, canal):
        try:
            eu = canal.guild.me
            return bool(eu and canal.permissions_for(eu).mention_everyone)
        except AttributeError:
            return False

    async def enviar_alerta(self, oferta, mencao=None):
        try:
            canal = await self.canal()
            pode = True
            if mencao == promo.MENCAO_EVERYONE:
                pode = self.pode_marcar_everyone(canal)
                if not pode:
                    log.warning(
                        "⚠️ Sem permissão de marcar @everyone no canal de alertas: marquei só o dono. "
                        "Reconvide o bot com: %s", link_convite(self.application_id),
                    )
            conteudo, permitidas = montar_mencao(mencao, self.segredos.dono_id, pode)
            ja_marcado = {self.segredos.dono_id} if conteudo else set()
            conteudo, permitidas = self._com_pessoas(oferta, conteudo, permitidas, ja_marcado)
            embed = discord.Embed.from_dict(formato.embed_alerta(oferta, self.cfg.fuso))
            await canal.send(content=conteudo, embed=embed, allowed_mentions=permitidas)
            return True
        except discord.DiscordException:
            log.exception("falha ao mandar alerta")
            return False

    def _com_pessoas(self, oferta, conteudo, permitidas, excluir=()):
        """Junta na mensagem quem pediu (/cargos) pra ser marcado nessa oferta."""
        ids = quem_marcar(db.preferencias_ativas(self.con), oferta, self._ping_usuario, db.agora_utc(), excluir)
        if not ids:
            return conteudo, permitidas
        conteudo = " ".join([conteudo or ""] + [f"<@{i}>" for i in ids]).strip()
        if permitidas.users is not True:  # True já deixa marcar todo mundo que está no texto
            permitidas = permitidas.merge(discord.AllowedMentions(users=[discord.Object(id=i) for i in ids]))
        return conteudo, permitidas

    async def enviar_vitrine(self, oferta):
        try:
            canal = await self.canal()
            embed = discord.Embed.from_dict(formato.embed_vitrine(oferta, self.cfg.fuso))
            conteudo, permitidas = self._com_pessoas(oferta, None, discord.AllowedMentions.none())
            await canal.send(content=conteudo, embed=embed, allowed_mentions=permitidas)
            return True
        except discord.DiscordException:
            log.exception("falha ao mandar a vitrine")
            return False

    async def enviar_alerta_usuario(self, alerta, achado, ml, pct):
        """/alerta: marca só a pessoa que pediu, no canal."""
        try:
            canal = await self.canal()
            uid = alerta["usuario_id"]
            bom = self.descoberta.preco_bom_do_achado(achado, ml) if self.descoberta else None
            embed = discord.Embed.from_dict(formato.embed_alerta_usuario(alerta, achado, ml, pct, bom))
            await canal.send(content=f"<@{uid}> 🔔 chegou em **{formato.formatar_reais(achado.preco)}**",
                             embed=embed, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=uid)]))
            return True
        except discord.DiscordException:
            log.exception("falha ao mandar o /alerta #%s", alerta.get("id"))
            return False

    async def enviar_aviso(self, texto):
        try:
            canal = await self.canal()
            await canal.send(texto)
        except discord.DiscordException:
            log.exception("falha ao mandar aviso")

    # ------------------------------------------------------------
    # loop
    # ------------------------------------------------------------

    @tasks.loop(minutes=10)
    async def loop_caca(self):
        try:
            await self.cacador.rodada()
        except Exception:
            log.exception("erro na rodada")

    @loop_caca.before_loop
    async def antes_do_loop(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=10)
    async def loop_descoberta(self):
        try:
            await self.descoberta.varredura()  # de hora em hora: qualquer perfume em promo
        except Exception:
            log.exception("erro na varredura")
        try:
            await self.descoberta.rodada(max_segundos=30 * 60)  # catálogo, sem travar a próxima varredura
        except Exception:
            log.exception("erro na busca do catálogo")

    @loop_descoberta.before_loop
    async def antes_da_descoberta(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=15)
    async def loop_vitrine(self):
        try:
            await self.descoberta.postar_vitrine(self.enviar_vitrine)
        except Exception:
            log.exception("erro na vitrine")

    @loop_vitrine.before_loop
    async def antes_da_vitrine(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=60)
    async def loop_alertas(self):
        try:
            await self.descoberta.checar_alertas_usuarios(self.enviar_alerta_usuario)
        except Exception:
            log.exception("erro nos /alerta")

    @loop_alertas.before_loop
    async def antes_dos_alertas(self):
        await self.wait_until_ready()
        await asyncio.sleep(5 * 60)  # deixa a primeira varredura começar antes

    @tasks.loop(time=dt.time(19, 0))
    async def loop_top(self):
        try:
            await self.postar_top()
        except Exception:
            log.exception("erro no top do dia")

    @loop_top.before_loop
    async def antes_do_top(self):
        await self.wait_until_ready()

    @tasks.loop(time=dt.time(20, 0))
    async def loop_autocatalogo(self):
        try:
            await self.rodar_autocatalogo()
        except Exception:
            log.exception("erro no catálogo automático")

    @loop_autocatalogo.before_loop
    async def antes_do_autocatalogo(self):
        await self.wait_until_ready()

    async def rodar_autocatalogo(self, anunciar=True):
        """Põe no catálogo os perfumes novos da semana e conta no canal. Devolve [(ItemCatalogo, leituras)].
        Uma análise por vez (o horário das 20:00 e o /catalogo rodar não se atropelam)."""
        d = self.descoberta
        if not d:
            return []
        async with self.trava_autocatalogo:
            for item_id in autocatalogo.expirar(self.con):  # quem sumiu do ML há 30 dias sai
                d.remover_item(item_id)
            vagas = autocatalogo.vagas()
            if not vagas:
                log.info("📚 Catálogo automático: cheio (%d itens)", autocatalogo.MAX_TOTAL)
                return []
            preparado = autocatalogo.preparar(self.con, d.itens)  # banco: na thread do bot
            removidos = autocatalogo.carregar()["removidos"]
            novos = await asyncio.to_thread(autocatalogo.escolher, preparado, list(d.itens), removidos, vagas)
            novos = autocatalogo.gravar(novos)
            if not novos:
                log.info("📚 Catálogo automático: nada novo hoje")
                return []
            d.adicionar_itens([item for item, _ in novos])
        if anunciar:
            canal = await self.canal()
            await canal.send(embed=discord.Embed.from_dict(formato.embed_catalogo_novo([i for i, _ in novos])),
                             allowed_mentions=discord.AllowedMentions.none())
        return novos

    async def postar_top(self, hoje=None):
        """Top do dia; no domingo, o 🏆 Top da semana (se a semana teve promo; senão, o do dia)."""
        if not self.descoberta:
            return False
        hoje = hoje or dt.datetime.now(formato.fuso(self.cfg.fuso)).date()
        if self.cfg.descoberta.top_semanal and hoje.weekday() == 6:
            itens = self.descoberta.top_semana()
            if itens:
                canal = await self.canal()
                await canal.send(embed=discord.Embed.from_dict(formato.embed_top_semana(itens, self.cfg.fuso)))
                return True
        ofertas = self.descoberta.top()
        if not ofertas:
            return False  # dia fraco: não posta "nada hoje" no canal
        canal = await self.canal()
        await canal.send(embed=discord.Embed.from_dict(formato.embed_top(ofertas, nome_fuso=self.cfg.fuso)))
        return True

    def eh_dono(self, interaction):
        return interaction.user.id == self.segredos.dono_id


# ============================================================
# AUTOCOMPLETE
# ============================================================

async def autocomplete_perfume(interaction, atual):
    bot = interaction.client
    marca = getattr(interaction.namespace, "marca", None)  # se a marca já foi preenchida, filtra por ela
    perfumes = busca.filtrar_por_marca(db.listar_perfumes(bot.con), marca)
    if atual.strip():
        achados = [p for p, _ in busca.buscar(atual, perfumes, limite=60, janela=100)]
    else:
        achados = perfumes
    return [app_commands.Choice(name=p["nome"][:100], value=p["nome"][:100]) for p in achados[:25]]


async def autocomplete_marca(interaction, atual):
    bot = interaction.client
    marcas = sorted({p["marca"] for p in db.listar_perfumes(bot.con) if p.get("marca")}, key=str.lower)
    if atual.strip():
        marcas = [m for m in marcas if busca.marca_bate(atual, m)]
    return [app_commands.Choice(name=m[:100], value=m[:100]) for m in marcas[:25]]


async def autocomplete_anuncio(interaction, atual):
    bot = interaction.client
    anuncios = db.listar_anuncios(bot.con)
    alvo = busca.normalizar(atual)
    escolhas = []
    for a in anuncios:
        rotulo = f"#{a['id']} {a['perfume_nome']}"
        if a["ml"]:
            rotulo += f" {a['ml']:g}ml"
        rotulo += f" — {a['loja']}"
        if alvo and alvo not in busca.normalizar(rotulo) and str(atual).strip("#") != str(a["id"]):
            continue
        escolhas.append(app_commands.Choice(name=rotulo[:100], value=a["id"]))
    return escolhas[:25]


# ============================================================
# COMANDOS
# ============================================================

async def limpar_canal(canal, quantidade=None):
    """Apaga as mensagens do canal (as fixadas ficam). Mensagem com mais de 14 dias o Discord só deixa
    apagar uma por uma, então canal antigo e grande demora. Devolve quantas apagou."""
    apagadas = await canal.purge(limit=quantidade, check=lambda m: not m.pinned, bulk=True,
                                 reason="/clear de um admin")
    return len(apagadas)


class ConfirmarLimpeza(discord.ui.View):
    """Botões "Apagar" / "Cancelar" (só quem pediu vê: a mensagem é efêmera)."""

    def __init__(self, quantidade=None):
        super().__init__(timeout=60)
        self.quantidade = quantidade

    @discord.ui.button(label="Apagar", style=discord.ButtonStyle.danger, emoji="🧹")
    async def apagar(self, interaction: discord.Interaction, botao: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="🧹 Apagando... (canal grande pode demorar)", view=None)
        try:
            n = await limpar_canal(interaction.channel, self.quantidade)
        except discord.Forbidden:
            await interaction.edit_original_response(content=(
                "❌ Me falta permissão pra apagar. No Discord: Configurações do servidor → Cargos → Scentinel → "
                "liga **Gerenciar mensagens** e **Ver histórico de mensagens**. Ou abre de novo o link de "
                f"convite pra atualizar: {link_convite(interaction.client.application_id)}"))
            return
        await interaction.edit_original_response(content=f"🧹 Apaguei {n} mensagem(ns). As fixadas ficaram.")

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, botao: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Beleza, não apaguei nada.", view=None)


def _motivo_curto(texto):
    return formato.cortar(str(texto or "?").removeprefix("api: ").removeprefix("página: "), 160)


def texto_status(estados, nome_fuso="America/Sao_Paulo"):
    linhas = ["**Lojas do Scentinel**"]
    for e in estados:
        if e["pausada_ate"]:
            linhas.append(f"⏸️ **{e['nome']}**: pausada até {formato.formatar_data(e['pausada_ate'], nome_fuso)}"
                          f" ({_motivo_curto(e['motivo'])})")
        else:
            ok = formato.formatar_data(e["ultimo_ok"], nome_fuso) if e["ultimo_ok"] else "ainda nenhuma"
            linhas.append(f"🟢 **{e['nome']}**: funcionando · última busca boa: {ok}")
        if e["ultimo_erro"] and not e["pausada_ate"]:
            linhas.append(f"   último erro: {_motivo_curto(e['ultimo_erro'])}")
    linhas.append("O dono pode usar `/varrer` pra tentar de novo agora.")
    return "\n".join(linhas)


def texto_vitrine(resumo, nome_fuso="America/Sao_Paulo"):
    if not resumo["minutos"]:
        return "🛍️ Vitrine desligada (vitrine_minutos = 0 no config.toml)"
    ultimo = formato.formatar_data(resumo["ultimo_post"], nome_fuso) if resumo["ultimo_post"] else "nenhum ainda"
    return (f"🛍️ Vitrine: 1 post a cada {resumo['minutos']:g} min · {resumo['conhecidas']} oferta(s) conhecida(s) "
            f"nas últimas 24 h · último post no canal: {ultimo}")


def texto_pausadas(estados, nome_fuso="America/Sao_Paulo"):
    pausadas = [e for e in estados if e["pausada_ate"]]
    linhas = ["⏸️ Não dá pra pesquisar agora: " + ("a loja está pausada." if len(pausadas) == 1 else
                                                    "as lojas estão pausadas.")]
    for e in pausadas:
        linhas.append(f"**{e['nome']}** volta {formato.formatar_data(e['pausada_ate'], nome_fuso)} "
                      f"({_motivo_curto(e['motivo'])})")
    linhas.append("O dono pode usar `/varrer` pra tentar de novo agora.")
    return "\n".join(linhas)


MAX_ALERTAS_POR_PESSOA = 5
MAX_ALERTAS_DONO = 20
MAX_ALERTAS_TOTAL = 60  # cada alerta é uma pesquisa no ML a cada ~3 h: com educação


def _nome_alerta(alerta):
    return alerta["consulta"] + (f" {alerta['ml']:g}ml" if alerta.get("ml") else "")


def texto_alerta_criado(alerta, res, melhor):
    fr = formato.formatar_reais
    texto = (f"🔔 Alerta #{alerta['id']} criado: **{_nome_alerta(alerta)}** até **{fr(alerta['preco_max'])}**.\n"
             "Eu te marco no canal quando chegar nesse preço (confiro a cada ~3 h).")
    if melhor and melhor[0].preco <= alerta["preco_max"]:
        texto += f"\n🎉 Já tá nesse preço agora: **{fr(melhor[0].preco)}** · [ver anúncio]({melhor[0].url})"
    elif melhor:
        texto += f"\nAgora o mais barato tá **{fr(melhor[0].preco)}** · [ver anúncio]({melhor[0].url})"
    elif res.lojas_ok:
        texto += ("\n⚠️ Agora não achei esse perfume no catálogo do Mercado Livre. Confere o nome com o `/locate`"
                  " (o alerta fica valendo mesmo assim).")
    else:
        texto += "\n⚠️ O Mercado Livre não respondeu agora; eu confiro mais tarde."
    return texto


def texto_alertas(alertas):
    if not alertas:
        return "Você não tem nenhum alerta. Cria um com `/alerta criar` 🙂"
    fr = formato.formatar_reais
    linhas = ["🔔 **Seus alertas**"]
    for a in alertas:
        extra = f" · último aviso {fr(a['avisado_preco'])}" if a["avisado_preco"] is not None else ""
        linhas.append(f"`#{a['id']}` {_nome_alerta(a)} até **{fr(a['preco_max'])}**{extra}")
    linhas.append("Pra parar um: `/alerta remover`")
    return "\n".join(linhas)


async def autocomplete_meus_alertas(interaction, atual):
    alertas = db.alertas_do_usuario(interaction.client.con, interaction.user.id)
    alvo = busca.normalizar(atual)
    escolhas = []
    for a in alertas:
        rotulo = f"#{a['id']} {_nome_alerta(a)} até {formato.formatar_reais(a['preco_max'])}"
        if alvo and alvo not in busca.normalizar(rotulo):
            continue
        escolhas.append(app_commands.Choice(name=rotulo[:100], value=a["id"]))
    return escolhas[:25]


def texto_catalogo_auto(itens):
    if not itens:
        return "📚 O catálogo automático ainda não pôs nenhum perfume."
    linhas = [f"📚 **Catálogo automático: {len(itens)} perfume(s)**"]
    for i in itens[-40:]:
        linhas.append(f"• {i.nome_exibicao} ({i.marca}) · {i.rotulo_categoria} · normal ~"
                      f"{formato.formatar_reais(i.preco_normal)} ({i.ml_ref:g}ml)")
    linhas.append("Pra tirar um: `/catalogo remover`")
    return "\n".join(linhas)


async def autocomplete_catalogo_auto(interaction, atual):
    alvo = busca.normalizar(atual)
    escolhas = []
    for i in reversed(autocatalogo.itens_salvos()):
        rotulo = f"{i.nome_exibicao} ({i.marca})"
        if alvo and alvo not in busca.normalizar(rotulo):
            continue
        escolhas.append(app_commands.Choice(name=rotulo[:100], value=i.id[:100]))
    return escolhas[:25]


async def _editar_sem_falhar(interaction, texto):
    """Resposta que pode falhar sem atrapalhar o resto (ex.: o /atualizar demorou mais que o Discord espera)."""
    try:
        await interaction.edit_original_response(content=texto)
    except discord.HTTPException:
        log.warning("não deu pra editar a resposta: %s", texto[:200])


ESCOLHAS_ESTILO = [app_commands.Choice(name=rotulo, value=valor) for valor, rotulo in catalogo.CATEGORIAS.items()]
ESCOLHAS_ESTILO.append(app_commands.Choice(name="🧴 Outras marcas", value=catalogo.OUTROS))


def texto_send(enviadas, numero, estilo=None, ate=None, desconto=None, resumo=None):
    filtros = [x for x in (estilo, f"até {formato.formatar_reais(ate)}" if ate else None,
                           f"{desconto}%+ OFF" if desconto else None) if x]
    com = f" ({', '.join(filtros)})" if filtros else ""
    if enviadas == numero:
        return f"🛍️ Mandei {enviadas} oferta(s){com} no canal."
    if enviadas:
        return (f"🛍️ Mandei {enviadas} de {numero}{com}: é o que eu conheço agora. "
                "A varredura de hora em hora vai achando mais.")
    conhecidas = (resumo or {}).get("conhecidas", 0)
    if filtros:
        return (f"🔍 Nenhuma oferta{com} agora ({conhecidas} conhecida(s) nas últimas 24 h, de todos os estilos). "
                "Tenta sem filtro ou mais tarde.")
    return (f"🔍 Não tenho oferta pra mandar agora ({conhecidas} conhecida(s) nas últimas 24 h). "
            "Usa o `/status` pra ver se o Mercado Livre está funcionando.")


def registrar_comandos(tree):
    @tree.command(name="locate", description="Pesquisa um perfume nas lojas agora e mostra o mais barato")
    @app_commands.describe(
        nome="Nome do perfume (pode ser aproximado, ex.: 'sauvage edp')",
        marca="Marca (opcional, ex.: 'dior')",
    )
    async def locate(interaction: discord.Interaction, nome: str, marca: str | None = None):
        # sem sugestão de nome: o /locate pesquisa do zero toda vez, nada vem do que já foi guardado
        await interaction.response.defer(thinking=True)
        try:
            await _locate(interaction, nome, marca)
        except Exception as e:  # nunca deixa a resposta parada em "procurando..."
            log.exception("erro no /locate %r %r", nome, marca)
            await interaction.edit_original_response(
                content=f"❌ Deu erro procurando **{nome}** ({e.__class__.__name__}). Tenta de novo daqui a pouco.",
                embed=None,
            )

    async def _locate(interaction, nome, marca):
        bot = interaction.client

        async def avisar_rechecagem(n):
            await interaction.edit_original_response(content=f"⏳ checando {n} loja(s)...")

        if bot.descoberta:
            lojas = [f.nome for f in bot.descoberta.fontes if not bot.descoberta.pausada_pro_locate(f)]
            if not lojas and bot.descoberta.fontes:  # tudo pausado: diz por quê, em vez de "não achei"
                await interaction.edit_original_response(
                    content=formato.cortar(texto_pausadas(bot.descoberta.estado_fontes(), bot.cfg.fuso), 1900))
                return
            await interaction.edit_original_response(content="🔎 procurando...")
            # só o que achou AGORA: nunca preço guardado de antes (se não achou, diz por quê)
            ao_vivo = await bot.descoberta.buscar_ao_vivo(nome, marca)
            await interaction.edit_original_response(
                content=None, embed=discord.Embed.from_dict(formato.embed_ao_vivo(ao_vivo))
            )
            return

        # busca automática desligada: só os anúncios cadastrados com /add
        resposta = await bot.cacador.locate(nome, ao_rechecar=avisar_rechecagem, marca=marca)
        if resposta.texto:
            await interaction.edit_original_response(content=resposta.texto)
            return
        embed = formato.embed_locate(
            resposta.melhor, resposta.em_promo, resposta.outros, resposta.menor_por_ml,
            resposta.outras_variacoes, bot.cfg.fuso,
        )
        await interaction.edit_original_response(content=None, embed=discord.Embed.from_dict(embed))

    @tree.command(name="add", description="Cadastra um anúncio pra vigiar (só o dono)")
    @app_commands.describe(
        url="Link do anúncio",
        nome="Nome do perfume, ex.: 'Sauvage EDP' (o tamanho vai no campo ml)",
        ml="Tamanho do frasco em ml",
        alvo="Preço alvo opcional: avisa quando ficar igual ou abaixo",
        marca="Marca (opcional, ajuda na busca)",
        seletor="Seletor CSS do preço (plano B, só se a leitura automática falhar)",
    )
    @app_commands.autocomplete(marca=autocomplete_marca)
    async def add(
        interaction: discord.Interaction,
        url: str,
        nome: str,
        ml: app_commands.Range[float, 1, 5000],
        alvo: app_commands.Range[float, 1, 100000] | None = None,
        marca: str | None = None,
        seletor: str | None = None,
    ):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return
        if not url.startswith(("http://", "https://")):
            await interaction.response.send_message("Esse link não parece válido 🤔", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            anuncio, chk = await bot.cacador.adicionar(url, nome, ml=ml, alvo=alvo, marca=marca, seletor=seletor)
        except ValueError as e:
            await interaction.edit_original_response(content=f"❌ {e}")
            return

        cabecalho = f"✅ Cadastrado **#{anuncio['id']} {anuncio['perfume_nome']} {ml:g}ml** — {anuncio['loja']}"
        if chk.oferta:
            embed = formato.embed_alerta(chk.oferta, bot.cfg.fuso)
            embed["title"] = "🧪 Leitura de teste: " + embed["title"]
            await interaction.edit_original_response(content=cabecalho, embed=discord.Embed.from_dict(embed))
        else:
            await interaction.edit_original_response(
                content=f"{cabecalho}\n⚠️ Mas não consegui ler o preço agora: **{chk.erro}**.\n"
                "Ele continua cadastrado e eu tento de novo na próxima rodada. Se falhar sempre, "
                "dá pra remover e cadastrar de novo com um `seletor`."
            )

    @tree.command(name="remove", description="Para de vigiar um anúncio (só o dono)")
    @app_commands.describe(anuncio="Qual anúncio")
    @app_commands.autocomplete(anuncio=autocomplete_anuncio)
    async def remove(interaction: discord.Interaction, anuncio: int):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return
        removido = db.remover_anuncio(bot.con, anuncio)
        if not removido:
            await interaction.response.send_message(f"Não achei o anúncio #{anuncio}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🗑️ Removido: #{removido['id']} {removido['perfume_nome']} — {removido['loja']}"
        )

    @tree.command(name="lista", description="Lista os perfumes e anúncios vigiados")
    async def lista(interaction: discord.Interaction):
        bot = interaction.client
        todos = db.listar_anuncios(bot.con)
        anuncios = [a for a in todos if (a.get("origem") or "manual") == "manual"]
        automaticos = len(todos) - len(anuncios)
        rodape_auto = (f"\n\n🔎 + {automaticos} anúncio(s) achados pela busca automática (veja com `/locate` e `/top`)"
                       if automaticos else "")
        if not anuncios:
            await interaction.response.send_message("Nenhum anúncio cadastrado à mão. Use `/add` 🙂" + rodape_auto)
            return
        linhas = []
        atual = None
        for a in anuncios:
            if a["perfume_nome"] != atual:
                atual = a["perfume_nome"]
                linhas.append(f"\n**{atual}**")
            ultima = db.ultima_leitura(bot.con, a["id"])
            preco = formato.formatar_reais(ultima["preco"]) if ultima else "sem leitura"
            extras = []
            if a["ml"]:
                extras.append(f"{a['ml']:g}ml")
            if a["alvo"]:
                extras.append(f"🎯 {formato.formatar_reais(a['alvo'])}")
            if a["bloqueado"]:
                extras.append("🚫 bloqueado")
            if ultima and ultima["esgotado"]:
                extras.append("⚪ esgotado")
            linhas.append(f"`#{a['id']}` [{a['loja']}]({a['url']}) · {preco} · " + " · ".join(extras))
        embed = {
            "title": f"🧴 Vigiando {len(anuncios)} anúncio(s)",
            "color": formato.COR_NEUTRA,
            "description": formato.cortar("\n".join(linhas).strip() + rodape_auto),
        }
        await interaction.response.send_message(embed=discord.Embed.from_dict(embed))

    @tree.command(name="historico", description="Mín., máx., mediana e último preço de um perfume")
    @app_commands.describe(nome="Nome do perfume")
    @app_commands.autocomplete(nome=autocomplete_perfume)
    async def historico(interaction: discord.Interaction, nome: str):
        bot = interaction.client
        achados = busca.buscar(nome, db.listar_perfumes(bot.con))
        if not achados:
            await interaction.response.send_message("Não achei esse perfume 🔍")
            return
        await interaction.response.defer(thinking=True)
        perfume = achados[0][0]
        fr = formato.formatar_reais
        linhas = []
        for h in bot.cacador.historico(perfume["id"]):
            a = h["anuncio"]
            titulo = f"**{a['loja']}**" + (f" {a['ml']:g}ml" if a["ml"] else "")
            if not h["leituras"]:
                linhas.append(f"{titulo}: sem leitura ainda")
                continue
            ultima = h["ultima"]
            quando = formato.formatar_data(db.de_texto(ultima["quando"]), bot.cfg.fuso)
            mediana = fr(h["mediana"]) if h["mediana"] else "—"
            linha = (
                f"{titulo} ({h['leituras']} leituras)\n"
                f"  mín. {fr(h['min'])} · máx. {fr(h['max'])} · mediana {bot.cfg.ref_dias}d {mediana}\n"
                f"  último: {fr(ultima['preco'])} em {quando}"
                + (" ⚪ esgotado" if ultima["esgotado"] else "")
            )
            if (a.get("origem") or "manual") == "descoberta":
                atual = bot.cacador.oferta_atual(a)
                if atual:
                    linha += "\n  " + formato.curto_preco_bom(atual.preco, atual.preco_bom)
            linhas.append(linha)
        embed = {
            "title": f"📈 Histórico: {perfume['nome']}",
            "color": formato.COR_NEUTRA,
            "description": formato.cortar("\n".join(linhas) or "Sem anúncios."),
        }
        arquivos = []
        dados = grafico.dados_do_grafico(bot.con, perfume, nome_fuso=bot.cfg.fuso)
        png = await asyncio.to_thread(grafico.desenhar, *dados) if dados else None
        if png:
            arquivos.append(discord.File(io.BytesIO(png), filename="historico.png"))
            embed["image"] = {"url": "attachment://historico.png"}
        elif linhas:
            embed["footer"] = {"text": "📈 O gráfico aparece quando tiver leitura de pelo menos 2 dias."}
        await interaction.edit_original_response(embed=discord.Embed.from_dict(embed), attachments=arquivos)

    @tree.command(name="top", description="As melhores promos das últimas 24 h, uma por perfume")
    async def top(interaction: discord.Interaction):
        bot = interaction.client
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.")
            return
        ofertas = bot.descoberta.top()
        embed = formato.embed_top(ofertas, titulo="🏆 Melhores promos agora", nome_fuso=bot.cfg.fuso)
        await interaction.response.send_message(embed=discord.Embed.from_dict(embed))

    @tree.command(name="varrer", description="Varre as ofertas de perfume das lojas agora (só o dono)")
    async def varrer(interaction: discord.Interaction):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.")
            return
        if bot.descoberta.trava_varredura.locked():
            await interaction.response.send_message("🧹 Já tem uma varredura rodando agora. Os posts chegam no canal.")
            return
        await interaction.response.defer(thinking=True)
        bot.descoberta.despausar()  # o dono pediu: tenta de novo agora, mesmo nas lojas pausadas
        linhas = await bot.descoberta.varredura(forcar=True)
        postados = sum(1 for linha in linhas if linha.startswith("🔥"))
        problemas = [linha for linha in linhas if linha.startswith(("🚫", "❌", "⏸️"))]
        texto = f"🧹 Varredura feita: {postados} promo(s) postada(s) no canal."
        if problemas:
            texto += "\n" + "\n".join(problemas[:8])
        await interaction.edit_original_response(content=formato.cortar(texto, 1900))

    @tree.command(name="clear", description="Apaga as mensagens deste canal (só admin)")
    @app_commands.describe(quantidade="Quantas mensagens apagar, das mais novas pra trás (vazio = todas)")
    @app_commands.default_permissions(administrator=True)  # nem aparece pra quem não é admin
    @app_commands.guild_only()
    async def clear(interaction: discord.Interaction, quantidade: app_commands.Range[int, 1, 10000] | None = None):
        if not interaction.permissions.administrator:  # confere de novo (o servidor pode mudar quem vê)
            await interaction.response.send_message("Só admin pode usar isso 🙅", ephemeral=True)
            return
        alvo = f"as últimas **{quantidade}** mensagens" if quantidade else "**todas** as mensagens"
        await interaction.response.send_message(
            f"Apagar {alvo} deste canal? As fixadas ficam. **Não tem volta.**",
            view=ConfirmarLimpeza(quantidade), ephemeral=True)

    @tree.command(name="cargos", description="Cria os cargos de estilo e manda os botões pra galera escolher (só admin)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def cargos(interaction: discord.Interaction):
        if not interaction.permissions.administrator:
            await interaction.response.send_message("Só admin pode usar isso 🙅", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        criados = []
        try:
            for nome in CARGOS.values():
                if not discord.utils.get(interaction.guild.roles, name=nome):
                    await interaction.guild.create_role(name=nome, mentionable=True, reason="/cargos do Scentinel")
                    criados.append(nome)
            await interaction.channel.send(embed=discord.Embed.from_dict(embed_cargos()), view=BotoesCargos())
        except discord.Forbidden:
            await interaction.edit_original_response(content=(
                "❌ Me falta a permissão **Gerenciar cargos**. Abre de novo o link de convite pra atualizar: "
                f"{link_convite(interaction.client.application_id)}\n"
                "(ou: Configurações do servidor → Cargos → Scentinel → liga Gerenciar cargos)"))
            return
        texto = f"✅ Criei: {', '.join(criados)}." if criados else "✅ Os cargos já existiam."
        await interaction.edit_original_response(
            content=texto + " Mandei os botões aqui no canal: fixa a mensagem pra galera achar fácil 📌")

    @tree.command(name="send", description="Manda agora N ofertas no canal (só admin)")
    @app_commands.describe(
        numero="Quantas ofertas mandar (até 20)",
        estilo="Só um estilo (vazio = qualquer um)",
        ate="Só até esse preço, em R$ (opcional)",
        desconto="Só com pelo menos X% OFF (opcional)",
    )
    @app_commands.choices(estilo=ESCOLHAS_ESTILO)
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def send(interaction: discord.Interaction, numero: app_commands.Range[int, 1, 20],
                   estilo: app_commands.Choice[str] | None = None,
                   ate: app_commands.Range[float, 10, 100000] | None = None,
                   desconto: app_commands.Range[int, 1, 90] | None = None):
        bot = interaction.client
        if not interaction.permissions.administrator:
            await interaction.response.send_message("Só admin pode usar isso 🙅", ephemeral=True)
            return
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        valor_estilo = estilo.value if estilo else None
        filtro = None
        if valor_estilo or ate or desconto:
            filtro = bot.descoberta.filtro_oferta(valor_estilo, ate, desconto)
        enviadas = await bot.descoberta.mandar_varias(numero, bot.enviar_vitrine, filtro=filtro)
        await interaction.followup.send(texto_send(enviadas, numero, estilo.name if estilo else None, ate, desconto,
                                                   bot.descoberta.resumo_vitrine()), ephemeral=True)

    @tree.command(name="despausar", description="Tira a pausa das lojas agora (só o dono)")
    async def despausar(interaction: discord.Interaction):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.", ephemeral=True)
            return
        pausadas = [e["nome"] for e in bot.descoberta.estado_fontes() if e["pausada_ate"]]
        bot.descoberta.despausar()
        texto = (f"▶️ Tirei a pausa: {', '.join(pausadas)}. Pode usar o `/locate`." if pausadas
                 else "Nenhuma loja estava pausada 👍")
        await interaction.response.send_message(texto, ephemeral=True)

    @tree.command(name="status", description="Como estão as lojas: funcionando, pausadas e por quê")
    async def status(interaction: discord.Interaction):
        bot = interaction.client
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.", ephemeral=True)
            return
        texto = texto_status(bot.descoberta.estado_fontes(), bot.cfg.fuso)
        texto += "\n" + texto_vitrine(bot.descoberta.resumo_vitrine(), bot.cfg.fuso)
        await interaction.response.send_message(formato.cortar(texto, 1900), ephemeral=True)

    grupo_alerta = app_commands.Group(name="alerta", description="Alerta de preço só seu: o bot te marca quando baixar",
                                      guild_only=True)

    @grupo_alerta.command(name="criar", description="Me marca quando esse perfume ficar abaixo de um preço")
    @app_commands.describe(
        perfume="Nome do perfume (ex.: 'sauvage edp', 'khamrah')",
        preco="Avisa quando ficar igual ou abaixo disso, em R$",
        ml="Tamanho do frasco em ml (vazio = qualquer tamanho)",
    )
    async def alerta_criar(interaction: discord.Interaction, perfume: str,
                           preco: app_commands.Range[float, 10, 100000],
                           ml: app_commands.Range[float, 1, 1000] | None = None):
        bot = interaction.client
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.", ephemeral=True)
            return
        consulta = " ".join(perfume.split())[:80]
        if not busca.normalizar(consulta):
            await interaction.response.send_message("Escreve o nome do perfume 🙂", ephemeral=True)
            return
        meus = db.alertas_do_usuario(bot.con, interaction.user.id)
        limite = MAX_ALERTAS_DONO if bot.eh_dono(interaction) else MAX_ALERTAS_POR_PESSOA
        if len(meus) >= limite:
            await interaction.response.send_message(
                f"Você já tem {len(meus)} alertas (o máximo é {limite}). Tira um com `/alerta remover`.", ephemeral=True)
            return
        if db.contar_alertas_usuario(bot.con) >= MAX_ALERTAS_TOTAL:
            await interaction.response.send_message(
                "O servidor já tem alertas demais agora (cada um é uma pesquisa no ML). Tenta mais tarde.",
                ephemeral=True)
            return
        repetido = next((a for a in meus if busca.normalizar(a["consulta"]) == busca.normalizar(consulta)
                         and a["ml"] == ml), None)
        if repetido:
            await interaction.response.send_message(
                f"Você já tem esse alerta (#{repetido['id']}, até {formato.formatar_reais(repetido['preco_max'])}). "
                "Pra mudar o preço, remove com `/alerta remover` e cria de novo.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        res = await bot.descoberta.buscar_ao_vivo(consulta)
        melhor = bot.descoberta.melhor_do_alerta(res, ml)
        ja = melhor[0].preco if melhor and melhor[0].preco <= preco else None  # já tá no preço: avisa aqui mesmo
        alerta = db.criar_alerta_usuario(bot.con, interaction.user.id, consulta, preco, ml, avisado_preco=ja,
                                         buscado=bool(res.lojas_ok))
        await interaction.edit_original_response(content=formato.cortar(texto_alerta_criado(alerta, res, melhor), 1900))

    @grupo_alerta.command(name="lista", description="Mostra os seus alertas de preço")
    async def alerta_lista(interaction: discord.Interaction):
        alertas = db.alertas_do_usuario(interaction.client.con, interaction.user.id)
        await interaction.response.send_message(formato.cortar(texto_alertas(alertas), 1900), ephemeral=True)

    @grupo_alerta.command(name="remover", description="Para um alerta de preço seu")
    @app_commands.describe(alerta="Qual alerta")
    @app_commands.autocomplete(alerta=autocomplete_meus_alertas)
    async def alerta_remover(interaction: discord.Interaction, alerta: int):
        removido = db.remover_alerta_usuario(interaction.client.con, alerta, interaction.user.id)
        texto = (f"🗑️ Parei o alerta #{removido['id']} ({_nome_alerta(removido)})." if removido
                 else f"Não achei o alerta #{alerta} entre os seus (veja com `/alerta lista`).")
        await interaction.response.send_message(texto, ephemeral=True)

    tree.add_command(grupo_alerta)

    grupo_catalogo = app_commands.Group(name="catalogo", description="Catálogo automático do bot (só o dono)",
                                        guild_only=True)

    async def _so_dono_com_busca(interaction):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return False
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.", ephemeral=True)
            return False
        return True

    @grupo_catalogo.command(name="novos", description="Perfumes que o catálogo automático pôs (só o dono)")
    async def catalogo_novos(interaction: discord.Interaction):
        if not await _so_dono_com_busca(interaction):
            return
        itens = autocatalogo.itens_salvos()
        await interaction.response.send_message(formato.cortar(texto_catalogo_auto(itens), 1900), ephemeral=True)

    @grupo_catalogo.command(name="remover", description="Tira um perfume do catálogo automático pra sempre (só o dono)")
    @app_commands.describe(perfume="Qual perfume")
    @app_commands.autocomplete(perfume=autocomplete_catalogo_auto)
    async def catalogo_remover(interaction: discord.Interaction, perfume: str):
        if not await _so_dono_com_busca(interaction):
            return
        removido = autocatalogo.remover(perfume)
        if not removido:
            await interaction.response.send_message("Não achei esse no catálogo automático 🔍", ephemeral=True)
            return
        interaction.client.descoberta.remover_item(perfume)
        nome = autocatalogo.item_de_dict(removido).nome_exibicao
        await interaction.response.send_message(f"🗑️ Tirei **{nome}** do catálogo. Ele não volta mais sozinho.",
                                                ephemeral=True)

    @grupo_catalogo.command(name="rodar", description="Roda a análise do catálogo automático agora (só o dono)")
    async def catalogo_rodar(interaction: discord.Interaction):
        if not await _so_dono_com_busca(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        novos = await interaction.client.rodar_autocatalogo()
        texto = (f"📚 Entraram {len(novos)} perfume(s) (mandei a lista no canal)." if novos else
                 "📚 Nada novo agora: nenhum masculino/unissex de marca conhecida visto mais de uma vez na semana.")
        await interaction.edit_original_response(content=texto)

    tree.add_command(grupo_catalogo)

    @tree.command(name="atualizar", description="Baixa a versão nova do bot, testa e reinicia (só o dono)")
    async def atualizar_cmd(interaction: discord.Interaction):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return
        if bot.trava_atualizar.locked():
            await interaction.response.send_message("⏳ Já estou atualizando, espera.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with bot.trava_atualizar:
            await _editar_sem_falhar(interaction, "🔄 Baixando e testando a versão nova (leva ~1 min)...")
            res = await asyncio.to_thread(atualizar.atualizar, rodando=bot.versao)
            try:
                texto = res.texto + ("\n🔄 Reiniciando... volto em ~1 min." if res.reiniciar else "")
                await _editar_sem_falhar(interaction, formato.cortar(texto, 1900))
            finally:
                if res.reiniciar:  # mesmo se o Discord falhar na resposta: código novo não fica parado no disco
                    log.info("🔄 /atualizar: reiniciando pra versão nova")
                    bot.reiniciar = True
                    await bot.close()

    @tree.command(name="checar", description="Força uma rodada de checagem agora (só o dono)")
    async def checar(interaction: discord.Interaction):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return
        if bot.cacador.lock.locked():
            await interaction.response.send_message("⏳ Já tem uma rodada rodando, espera ela terminar.")
            return
        await interaction.response.defer(thinking=True)
        checagens = await bot.cacador.rodada(forcar=True)
        if not checagens:
            minutos = int(bot.cfg.checar_forcado_min_minutos)
            texto = f"Nada pra checar: tudo foi checado nos últimos {minutos} min (ou está em pausa por bloqueio)."
        else:
            texto = "\n".join(c.linha for c in checagens)
        await interaction.edit_original_response(content=formato.cortar(texto, 1900))

    @tree.command(name="teste_alerta", description="Manda um alerta de mentira no canal (só o dono)")
    async def teste_alerta(interaction: discord.Interaction):
        bot = interaction.client
        if not bot.eh_dono(interaction):
            await interaction.response.send_message("Só o dono pode usar isso 🙅", ephemeral=True)
            return
        from scentinel.promo import Avaliacao, Oferta

        nivel = sorted(bot.cfg.niveis, key=lambda n: n.nivel)[min(2, len(bot.cfg.niveis) - 1)]
        oferta = Oferta(
            anuncio_id=0, perfume="Sauvage EDP (TESTE)", loja="Loja X", url="https://example.com",
            ml=100, preco=659.90, avaliacao=Avaliacao(659.90, 999.00, "historico", 33.9, nivel),
            preco_pix=626.90, menor_ja_visto=659.90, recorde=True, quando=db.agora_utc(),
        )
        ok = await bot.enviar_alerta(oferta, mencao=None)
        await interaction.response.send_message(
            "✅ Alerta de teste enviado no canal!" if ok else "❌ Não consegui mandar. Confere o CANAL_ALERTAS_ID.",
            ephemeral=True,
        )

    @tree.error
    async def ao_errar(interaction: discord.Interaction, erro: app_commands.AppCommandError):
        log.exception("erro no comando", exc_info=erro)
        texto = "💥 Deu erro aqui. Olha o log (`journalctl -u scentinel -f`)."
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=texto)
            else:
                await interaction.response.send_message(texto, ephemeral=True)
        except discord.DiscordException:
            pass


def rodar():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    # o bot não usa voz: esconde os avisos "PyNaCl/davey is not installed"
    logging.getLogger("discord.client").addFilter(lambda r: "voice will NOT be supported" not in r.getMessage())
    cfg = config.carregar_config()
    segredos = config.carregar_segredos()
    con = db.conectar(config.ARQ_DB)
    bot = BotScentinel(cfg, segredos, con)
    bot.versao = atualizar.registrar_versao()
    bot.run(segredos.token, log_handler=None)
    if bot.reiniciar:
        # saída com erro de propósito: o systemd (Restart=on-failure) liga de novo, já com o código novo
        raise SystemExit(atualizar.SAIDA_PRA_REINICIAR)
