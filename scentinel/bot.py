"""Bot do Discord: loop de caça + slash commands."""

import datetime as dt
import logging

import discord
from discord import app_commands
from discord.ext import tasks

from scentinel import busca, catalogo, config, db, formato, promo
from scentinel.descoberta import Descoberta
from scentinel.fontes import montar_fontes
from scentinel.caca import Cacador

log = logging.getLogger("scentinel")


# ver canais + mandar mensagem + gerenciar mensagens (/clear) + embed + ler histórico (/clear) + mencionar @everyone
PERMISSOES = 1024 | 2048 | 8192 | 16384 | 65536 | 131072


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
        self.descoberta = None
        if cfg.descoberta.ativo:
            itens = catalogo.carregar_catalogo()
            fontes = montar_fontes(cfg.descoberta.fontes)
            if itens and fontes:
                self.descoberta = Descoberta(self.cacador, itens, fontes, enviar_aviso=self.enviar_aviso)
                self.loop_descoberta.change_interval(minutes=cfg.tick_minutos)
                horario = horario_top(cfg.descoberta.top_diario, cfg.fuso)
                if horario:
                    self.loop_top.change_interval(time=horario)
                if cfg.descoberta.vitrine_minutos:
                    self.loop_vitrine.change_interval(minutes=cfg.descoberta.vitrine_minutos)

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
        if self.descoberta:
            d = self.descoberta
            log.info("🔎 Busca automática ligada: %d perfumes em %d loja(s)", len(d.itens), len(d.fontes))
            self.loop_descoberta.start()
            if horario_top(self.cfg.descoberta.top_diario, self.cfg.fuso):
                self.loop_top.start()
            if self.cfg.descoberta.vitrine_minutos:
                self.loop_vitrine.start()

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

    async def enviar_vitrine(self, oferta):
        try:
            canal = await self.canal()
            embed = discord.Embed.from_dict(formato.embed_vitrine(oferta, self.cfg.fuso))
            await canal.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except discord.DiscordException:
            log.exception("falha ao mandar a vitrine")
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

    @tasks.loop(time=dt.time(19, 0))
    async def loop_top(self):
        try:
            await self.postar_top()
        except Exception:
            log.exception("erro no top do dia")

    @loop_top.before_loop
    async def antes_do_top(self):
        await self.wait_until_ready()

    async def postar_top(self):
        ofertas = self.descoberta.top() if self.descoberta else []
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


def registrar_comandos(tree):
    @tree.command(name="locate", description="Pesquisa um perfume nas lojas agora e mostra o mais barato")
    @app_commands.describe(
        nome="Nome do perfume (pode ser aproximado, ex.: 'sauvage edp')",
        marca="Marca (opcional, ex.: 'dior')",
    )
    @app_commands.autocomplete(nome=autocomplete_perfume, marca=autocomplete_marca)
    async def locate(interaction: discord.Interaction, nome: str, marca: str | None = None):
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
            lojas = [f.nome for f in bot.descoberta.fontes if not bot.descoberta.pausada(f)]
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

    @tree.command(name="send", description="Manda agora N ofertas no canal (só admin)")
    @app_commands.describe(numero="Quantas ofertas mandar (até 20)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def send(interaction: discord.Interaction, numero: app_commands.Range[int, 1, 20]):
        bot = interaction.client
        if not interaction.permissions.administrator:
            await interaction.response.send_message("Só admin pode usar isso 🙅", ephemeral=True)
            return
        if not bot.descoberta:
            await interaction.response.send_message("A busca automática está desligada no config.toml.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        enviadas = await bot.descoberta.mandar_varias(numero, bot.enviar_vitrine)
        if enviadas == numero:
            texto = f"🛍️ Mandei {enviadas} oferta(s) no canal."
        elif enviadas:
            texto = (f"🛍️ Mandei {enviadas} de {numero}: é o que eu conheço agora. "
                     "A varredura de hora em hora vai achando mais.")
        else:
            resumo = bot.descoberta.resumo_vitrine()
            texto = (f"😿 Não tenho oferta pra mandar agora ({resumo['conhecidas']} conhecida(s) nas últimas 24 h). "
                     "Usa o `/status` pra ver se o Mercado Livre está funcionando.")
        await interaction.followup.send(texto, ephemeral=True)

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
