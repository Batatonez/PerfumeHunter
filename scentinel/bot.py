"""Bot do Discord: loop de caça + slash commands."""

import logging

import discord
from discord import app_commands
from discord.ext import tasks

from scentinel import busca, config, db, formato, promo
from scentinel.caca import Cacador

log = logging.getLogger("scentinel")


PERMISSOES = 1024 | 2048 | 16384 | 131072  # ver canais + mandar mensagem + embed + mencionar @everyone


def link_convite(application_id):
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot+applications.commands&permissions={PERMISSOES}"
    )


def montar_mencao(mencao, dono_id, pode_everyone=True):
    """Texto e menções permitidas do alerta.
    mencao: None, promo.MENCAO_DONO ou promo.MENCAO_EVERYONE.
    Sem a permissão de @everyone, marca pelo menos o dono."""
    if mencao == promo.MENCAO_EVERYONE:
        if pode_everyone:
            return "@everyone", discord.AllowedMentions(everyone=True, users=True, roles=False)
        return f"@everyone <@{dono_id}>", discord.AllowedMentions(everyone=False, users=True, roles=False)
    if mencao == promo.MENCAO_DONO:
        return f"<@{dono_id}>", discord.AllowedMentions(everyone=False, users=True, roles=False)
    return None, discord.AllowedMentions.none()


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
        self.tree = app_commands.CommandTree(self)
        self.cacador = Cacador(con, cfg, enviar_alerta=self.enviar_alerta, enviar_aviso=self.enviar_aviso)
        self.loop_caca.change_interval(minutes=cfg.tick_minutos)

    # ------------------------------------------------------------
    # ciclo de vida
    # ------------------------------------------------------------

    async def setup_hook(self):
        registrar_comandos(self.tree)
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
            embed = discord.Embed.from_dict(formato.embed_alerta(oferta, self.cfg.fuso))
            await canal.send(content=conteudo, embed=embed, allowed_mentions=permitidas)
            return True
        except discord.DiscordException:
            log.exception("falha ao mandar alerta")
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

def registrar_comandos(tree):
    @tree.command(name="locate", description="Procura um perfume em todos os anúncios e mostra a maior promo")
    @app_commands.describe(
        nome="Nome do perfume (pode ser aproximado, ex.: 'sauvage edp')",
        marca="Marca (opcional, ex.: 'dior')",
    )
    @app_commands.autocomplete(nome=autocomplete_perfume, marca=autocomplete_marca)
    async def locate(interaction: discord.Interaction, nome: str, marca: str | None = None):
        bot = interaction.client
        await interaction.response.defer(thinking=True)

        async def avisar_rechecagem(n):
            await interaction.edit_original_response(content=f"⏳ checando {n} loja(s)...")

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
        anuncios = db.listar_anuncios(bot.con)
        if not anuncios:
            await interaction.response.send_message("Nada cadastrado ainda. Use `/add` 🙂")
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
            "description": formato.cortar("\n".join(linhas).strip()),
        }
        await interaction.response.send_message(embed=discord.Embed.from_dict(embed))

    @tree.command(name="historico", description="Mín., máx., mediana e último preço de um perfume")
    @app_commands.describe(nome="Nome do perfume")
    @app_commands.autocomplete(nome=autocomplete_perfume)
    async def historico(interaction: discord.Interaction, nome: str):
        bot = interaction.client
        achados = busca.buscar(nome, db.listar_perfumes(bot.con))
        if not achados:
            await interaction.response.send_message("Não achei esse perfume 🤷")
            return
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
            linhas.append(
                f"{titulo} ({h['leituras']} leituras)\n"
                f"  mín. {fr(h['min'])} · máx. {fr(h['max'])} · mediana {bot.cfg.ref_dias}d {mediana}\n"
                f"  último: {fr(ultima['preco'])} em {quando}"
                + (" ⚪ esgotado" if ultima["esgotado"] else "")
            )
        embed = {
            "title": f"📈 Histórico: {perfume['nome']}",
            "color": formato.COR_NEUTRA,
            "description": formato.cortar("\n".join(linhas) or "Sem anúncios."),
        }
        await interaction.response.send_message(embed=discord.Embed.from_dict(embed))

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
    bot.run(segredos.token, log_handler=None)
