from datetime import UTC, datetime

from scentinel import config, db
from scentinel.formato import embed_alerta, embed_locate, formatar_data, formatar_reais
from scentinel.promo import Avaliacao, Nivel, Oferta

RASTRO = Nivel(3, "Rastro", "🟠", 30, 0xE67E22)


def oferta_exemplo(**kw):
    base = dict(
        anuncio_id=1, perfume="Sauvage EDP", loja="Loja X", url="https://x.com", ml=100,
        preco=659.90, avaliacao=Avaliacao(659.90, 999.00, "historico", 33.94, RASTRO),
        preco_pix=626.90, menor_ja_visto=659.90, recorde=True,
        quando=datetime(2026, 10, 11, 18, 30, tzinfo=UTC),
    )
    base.update(kw)
    return Oferta(**base)


def test_formatar_reais():
    assert formatar_reais(1299.9) == "R$ 1.299,90"
    assert formatar_reais(6.599) == "R$ 6,60"


def test_formatar_data_em_sao_paulo():
    assert formatar_data(datetime(2026, 10, 11, 18, 30, tzinfo=UTC)) == "11/10 15:30"


def test_embed_alerta_igual_ao_mock():
    e = embed_alerta(oferta_exemplo())
    assert e["title"] == "🟠 NÍVEL 3 · RASTRO — 34% OFF"
    assert e["color"] == 0xE67E22
    d = e["description"]
    assert "**Sauvage EDP 100ml** · Loja X" in d
    assert "R$ 659,90" in d and "**34% OFF** (ref. R$ 999,00)" in d and "R$ 6,60/ml" in d
    assert "⚡ Pix: R$ 626,90" in d
    assert "🏆 RECORDE" in d
    assert e["footer"]["text"] == "Checado em 11/10 15:30"


def test_embed_alerta_preco_da_loja_e_alvo():
    av = Avaliacao(700, 1000, "loja", 30, RASTRO)
    e = embed_alerta(oferta_exemplo(avaliacao=av, bateu_alvo=True, alvo=750))
    assert "📢 desconto anunciado pela loja" in e["description"]
    assert "🎯 Bateu seu alvo de R$ 750,00" in e["description"]


def test_porcentagem_off_em_todo_post():
    """Todo post com referência mostra quantos % OFF (no título e na linha do preço); 0% não aparece."""
    from scentinel.formato import embed_vitrine

    alvo = oferta_exemplo(avaliacao=Avaliacao(700, 760, "catalogo", 7.9, None), bateu_alvo=True, alvo=750,
                          origem="descoberta")
    e = embed_alerta(alvo)
    assert e["title"] == "💸 PREÇO BOM — 8% OFF" and "**8% OFF**" in e["description"]
    assert embed_alerta(oferta_exemplo(avaliacao=Avaliacao(700, 720, "historico", 2.8, None)))["title"] == "🏷️ 3% OFF"
    pouco = oferta_exemplo(avaliacao=Avaliacao(700, 703, "loja", 0.4, None))
    assert embed_vitrine(pouco)["title"] == "🛍️ VITRINE — achado do Scentinel"
    assert "OFF" not in embed_vitrine(pouco)["description"]
    assert embed_vitrine(oferta_exemplo(avaliacao=Avaliacao(700, 730, "loja", 4.1, None)))["title"] == "🛍️ VITRINE — 4% OFF"


def test_embed_locate():
    melhor = oferta_exemplo()
    outro = oferta_exemplo(anuncio_id=2, loja="Loja A", avaliacao=Avaliacao(880, 1000, "historico", 12, None))
    barato = oferta_exemplo(anuncio_id=3, loja="Loja C", ml=200, preco=1080)
    e = embed_locate(melhor, True, [outro], barato, ["Sauvage EDT"])
    assert e["title"] == "🏆 Maior promo: Sauvage EDP 100ml — Loja X"
    assert "**Outros:** Loja A 12% OFF" in e["description"]
    assert "Menor R$/ml:** Sauvage EDP 200ml — Loja C (R$ 5,40/ml)" in e["description"]
    assert e["footer"]["text"].startswith("também achei: Sauvage EDT")


def test_config_padrao_carrega():
    cfg = config.carregar_config()
    assert [n.nome for n in cfg.niveis] == ["Cheirinho", "Elogio", "Rastro", "Fera", "Bug de preço"]
    assert cfg.intervalo_horas >= 3
    assert cfg.niveis[3].ping and cfg.niveis[4].rechecar
    assert cfg.niveis[4].everyone and not any(n.everyone for n in cfg.niveis[:4])


def test_config_nao_deixa_baixar_de_3h(tmp_path):
    texto = (config.ARQ_CONFIG.read_text(encoding="utf-8")
             .replace("intervalo_horas = 3", "intervalo_horas = 1"))
    arq = tmp_path / "config.toml"
    arq.write_text(texto, encoding="utf-8")
    assert config.carregar_config(arq).intervalo_horas == 3


def test_comandos_registram_sem_erro():
    """Monta o bot (sem conectar) e confere os slash commands."""
    from scentinel.bot import BotScentinel, registrar_comandos

    segredos = config.Segredos("x", 1, 2, 3)
    bot = BotScentinel(config.carregar_config(), segredos, db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    nomes = {c.name for c in bot.tree.get_commands()}
    assert nomes == {"locate", "add", "remove", "lista", "historico", "checar", "teste_alerta", "top", "varrer",
                     "status", "despausar", "clear", "send", "atualizar", "alerta", "cargos", "catalogo"}


def test_bot_fora_do_servidor_mostra_link_de_convite(caplog):
    """403 Missing Access no sync -> mensagem clara com o link de convite, sem traceback gigante."""
    import asyncio
    from types import SimpleNamespace

    import discord
    import pytest

    from scentinel.bot import BotScentinel, link_convite

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 111, 2, 3), db.conectar(":memory:"))
    bot._connection.application_id = 999

    async def sync_negado(guild=None):
        resposta = SimpleNamespace(status=403, reason="Forbidden")
        raise discord.Forbidden(resposta, {"code": 50001, "message": "Missing Access"})

    bot.tree.sync = sync_negado
    with pytest.raises(SystemExit):
        asyncio.run(bot.setup_hook())
    assert link_convite(999) in caplog.text
    assert "client_id=999&scope=bot+applications.commands&permissions=268659712" in link_convite(999)


def test_montar_mencao():
    from scentinel.bot import montar_mencao

    texto, permitidas = montar_mencao("everyone", 42, pode_everyone=True)
    assert texto == "@everyone <@42>" and permitidas.everyone and permitidas.users

    # sem permissão: não finge que marcou todo mundo, mas marca o dono
    texto, permitidas = montar_mencao("everyone", 42, pode_everyone=False)
    assert "<@42>" in texto and not permitidas.everyone and permitidas.users

    texto, permitidas = montar_mencao("dono", 42)
    assert texto == "<@42>" and not permitidas.everyone

    texto, permitidas = montar_mencao(None, 42)
    assert texto is None and not permitidas.everyone and not permitidas.users



def test_comando_testar_usa_o_modulo_lojas():
    """A função do comando 'lojas' não pode esconder o módulo lojas (quebrava o 'testar')."""
    import types

    import scentinel.__main__ as cli

    assert isinstance(cli.lojas, types.ModuleType) and callable(cli.testar_lojas)


def test_locate_com_erro_nao_fica_procurando():
    """Se algo quebrar no meio, a resposta vira uma mensagem de erro (não fica em 'procurando...')."""
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    edicoes = []

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw)

    async def quebra(*a, **k):
        raise RuntimeError("loja doida")

    bot.descoberta.buscar_ao_vivo = quebra
    interacao = SimpleNamespace(client=bot, response=SimpleNamespace(defer=defer), edit_original_response=editar)
    asyncio.run(bot.tree.get_command("locate").callback(interacao, nome="sauvage"))
    assert "procurando" in edicoes[0]["content"]
    assert edicoes[-1]["content"].startswith("❌ Deu erro procurando **sauvage**")


def test_locate_com_tudo_pausado_diz_por_que():
    import asyncio
    from datetime import timedelta
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    desc = bot.descoberta
    agora = desc.cacador.relogio()
    for f in desc.fontes:
        desc._fonte_pausada_ate[f.id] = agora + timedelta(hours=1)
        desc._fonte_motivo[f.id] = "api: o ML pediu pra ir mais devagar (HTTP 429)"
    edicoes = []

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw)

    interacao = SimpleNamespace(client=bot, response=SimpleNamespace(defer=defer), edit_original_response=editar)
    asyncio.run(bot.tree.get_command("locate").callback(interacao, nome="queening"))
    assert len(edicoes) == 1
    texto = edicoes[0]["content"]
    assert "pausada" in texto and "Mercado Livre" in texto and "429" in texto and "/varrer" in texto


def test_texto_status():
    from datetime import UTC, datetime

    from scentinel.bot import texto_status

    texto = texto_status([
        {"nome": "Mercado Livre", "pausada_ate": datetime(2026, 10, 14, 18, 0, tzinfo=UTC),
         "motivo": "api: o login do ML venceu (HTTP 401)", "ultimo_ok": None, "ultimo_erro": "x"},
        {"nome": "Época", "pausada_ate": None, "motivo": None,
         "ultimo_ok": datetime(2026, 10, 14, 17, 0, tzinfo=UTC), "ultimo_erro": "HTTP 500"},
    ])
    assert "⏸️ **Mercado Livre**: pausada até 14/10 15:00 (o login do ML venceu (HTTP 401))" in texto
    assert "🟢 **Época**: funcionando · última busca boa: 14/10 14:00" in texto and "último erro: HTTP 500" in texto


def test_despausar_pelo_discord():
    import asyncio
    from datetime import timedelta
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, dono_id=42), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    desc = bot.descoberta
    ml = desc.fontes[0]
    desc._fonte_pausada_ate[ml.id] = desc.cacador.relogio() + timedelta(hours=12)
    desc._fonte_falhas[ml.id] = 2
    respostas = []

    async def responder(texto, **kw):
        respostas.append(texto)

    dono = SimpleNamespace(id=42)
    interacao = SimpleNamespace(client=bot, user=dono, response=SimpleNamespace(send_message=responder))
    asyncio.run(bot.tree.get_command("despausar").callback(interacao))
    assert not desc.pausada(ml) and desc._fonte_falhas == {}  # a próxima pausa volta a ser curta
    assert "Mercado Livre" in respostas[0]


def test_clear_so_admin_e_com_confirmacao():
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel, ConfirmarLimpeza, limpar_canal, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    cmd = bot.tree.get_command("clear")
    assert cmd.default_permissions.administrator  # escondido de quem não é admin
    respostas = []

    async def responder(texto, **kw):
        respostas.append((texto, kw))

    def interacao(admin):
        return SimpleNamespace(client=bot, permissions=SimpleNamespace(administrator=admin),
                               response=SimpleNamespace(send_message=responder))

    asyncio.run(cmd.callback(interacao(False)))
    assert "Só admin" in respostas[-1][0] and respostas[-1][1]["ephemeral"]

    async def com_admin():  # a View precisa de um loop rodando
        await cmd.callback(interacao(True), quantidade=None)

    asyncio.run(com_admin())
    texto, kw = respostas[-1]
    assert "**todas** as mensagens" in texto and "Não tem volta" in texto and kw["ephemeral"]
    assert isinstance(kw["view"], ConfirmarLimpeza)

    # a limpeza em si: apaga tudo menos as fixadas
    pedidos = {}

    class Canal:
        async def purge(self, **kw):
            pedidos.update(kw)
            msgs = [SimpleNamespace(pinned=False), SimpleNamespace(pinned=True), SimpleNamespace(pinned=False)]
            return [m for m in msgs if kw["check"](m)]

    assert asyncio.run(limpar_canal(Canal())) == 2
    assert pedidos["limit"] is None and pedidos["bulk"]


def test_embed_vitrine():
    from scentinel.formato import embed_vitrine

    sem_promo = oferta_exemplo(avaliacao=Avaliacao(659.90, None, None, 0, None))
    e = embed_vitrine(sem_promo)
    assert e["title"] == "🛍️ VITRINE — achado do Scentinel" and e["footer"]["text"].startswith("🛍️ Vitrine")
    com_desconto = oferta_exemplo(avaliacao=Avaliacao(659.90, 750, "loja", 12, None))
    assert embed_vitrine(com_desconto)["title"] == "🛍️ VITRINE — 12% OFF"
    assert embed_vitrine(oferta_exemplo())["title"] == "🟠 NÍVEL 3 · RASTRO — 34% OFF"  # promo: título normal


def test_vitrine_liga_com_o_bot():
    from scentinel.bot import BotScentinel, texto_vitrine

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    horarios = bot.loop_vitrine.time  # horário redondo: 00:00, 00:15, 00:30... (São Paulo)
    assert len(horarios) == 96 and [(h.hour, h.minute) for h in horarios[:3]] == [(0, 0), (0, 15), (0, 30)]
    assert str(horarios[0].tzinfo) == "America/Sao_Paulo"
    assert "a cada 15 min" in texto_vitrine({"minutos": 15, "conhecidas": 3, "ultimo_post": None})
    assert "desligada" in texto_vitrine({"minutos": 0, "conhecidas": 0, "ultimo_post": None})


def test_send_so_admin():
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    cmd = bot.tree.get_command("send")
    assert cmd.default_permissions.administrator
    respostas, seguintes, pedidos, filtros = [], [], [], []

    async def responder(texto, **kw):
        respostas.append(texto)

    async def defer(**kw):
        pass

    async def seguir(texto, **kw):
        seguintes.append(texto)

    async def mandar_varias(n, enviar, filtro=None):
        pedidos.append(n)
        filtros.append(filtro)
        return 2

    bot.descoberta.mandar_varias = mandar_varias

    def interacao(admin):
        return SimpleNamespace(client=bot, permissions=SimpleNamespace(administrator=admin),
                               response=SimpleNamespace(send_message=responder, defer=defer),
                               followup=SimpleNamespace(send=seguir))

    asyncio.run(cmd.callback(interacao(False), numero=5))
    assert "Só admin" in respostas[-1] and pedidos == []
    asyncio.run(cmd.callback(interacao(True), numero=5))
    assert pedidos == [5] and seguintes[-1].startswith("🛍️ Mandei 2 de 5") and filtros == [None]
    # com estilo: só árabe
    from discord import app_commands

    asyncio.run(cmd.callback(interacao(True), numero=3, estilo=app_commands.Choice(name="🕌 Árabe", value="arabe"),
                             ate=300.0))
    assert filtros[-1] is not None and "🕌 Árabe, até R$ 300,00" in seguintes[-1]
    assert {c.value for c in next(p for p in cmd.parameters if p.name == "estilo").choices} == {
        "designer", "nicho", "arabe", "nacional", "outros"}


def test_filtro_do_send():
    from scentinel.descoberta import Descoberta

    arabe = oferta_exemplo(estilo="arabe", preco=250, avaliacao=Avaliacao(250, 300, "catalogo", 16.7, None))
    sem_marca = oferta_exemplo(estilo=None, preco=150, avaliacao=Avaliacao(150, None, None, 0, None))
    assert Descoberta.filtro_oferta("arabe")(arabe) and not Descoberta.filtro_oferta("arabe")(sem_marca)
    assert Descoberta.filtro_oferta("outros")(sem_marca) and not Descoberta.filtro_oferta("outros")(arabe)
    assert not Descoberta.filtro_oferta(preco_max=200)(arabe) and Descoberta.filtro_oferta(preco_max=200)(sem_marca)
    assert Descoberta.filtro_oferta(desconto_min=15)(arabe) and not Descoberta.filtro_oferta(desconto_min=20)(arabe)
    assert not Descoberta.filtro_oferta(desconto_min=5)(sem_marca)


def test_locate_nunca_usa_preco_guardado():
    """Sem nada ao vivo, o /locate diz que não achou: nada de mostrar leitura antiga do banco."""
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel, registrar_comandos
    from scentinel.descoberta import ResultadoAoVivo

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    registrar_comandos(bot.tree)

    async def ao_vivo_vazio(nome, marca=None):
        return ResultadoAoVivo(nome, lojas_ok=["Mercado Livre"])

    async def nao_pode(*a, **k):
        raise AssertionError("não pode usar o preço guardado")

    bot.descoberta.buscar_ao_vivo = ao_vivo_vazio
    bot.cacador.locate = nao_pode
    edicoes = []

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw)

    interacao = SimpleNamespace(client=bot, response=SimpleNamespace(defer=defer), edit_original_response=editar)
    asyncio.run(bot.tree.get_command("locate").callback(interacao, nome="sauvage"))
    assert "Não achei esse perfume" in edicoes[-1]["embed"].description


def test_locate_sem_sugestao():
    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    assert not any(p.autocomplete for p in bot.tree.get_command("locate").parameters)


def test_domingo_sai_o_top_da_semana():
    import asyncio
    import datetime as dt
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    enviados = []

    async def canal():
        async def send(**kw):
            enviados.append(kw["embed"].title)
        return SimpleNamespace(send=send)

    bot.canal = canal
    anuncio = {"perfume_nome": "Khamrah", "ml": 100, "url": "https://x"}
    bot.descoberta.top_semana = lambda: [{"anuncio": anuncio, "pct": 30, "preco": 199, "quando": db.agora_utc(),
                                          "agora": None}]
    bot.descoberta.top = lambda: [oferta_exemplo()]
    assert asyncio.run(bot.postar_top(hoje=dt.date(2026, 10, 18)))  # domingo
    assert asyncio.run(bot.postar_top(hoje=dt.date(2026, 10, 19)))  # segunda
    assert enviados == ["🏆 Top da semana", "🏆 Top do dia"]


def test_alerta_pessoal_pelo_discord():
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import MAX_ALERTAS_POR_PESSOA, BotScentinel, registrar_comandos
    from scentinel.descoberta import ResultadoAoVivo
    from scentinel.fontes import Achado

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 42), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    grupo = bot.tree.get_command("alerta")
    criar, lista, remover = (grupo.get_command(n) for n in ("criar", "lista", "remover"))
    k = Achado(titulo="Perfume Lattafa Khamrah EDP 100ml", url="https://ml/k", preco=239.9, loja="Mercado Livre")

    async def ao_vivo(nome, marca=None):
        return ResultadoAoVivo(nome, ofertas=[(k, 100, 0)], lojas_ok=["Mercado Livre"])

    bot.descoberta.buscar_ao_vivo = ao_vivo
    respostas, edicoes = [], []

    async def responder(texto, **kw):
        respostas.append(texto)

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw["content"])

    def interacao(uid):
        return SimpleNamespace(client=bot, user=SimpleNamespace(id=uid), edit_original_response=editar,
                               response=SimpleNamespace(send_message=responder, defer=defer))

    asyncio.run(criar.callback(interacao(7), perfume="khamrah", preco=250.0))
    assert "Alerta #1 criado" in edicoes[-1] and "Já tá nesse preço agora: **R$ 239,90**" in edicoes[-1]
    criado = db.alerta_usuario(bot.con, 1)
    assert criado["avisado_preco"] == 239.9  # já avisou na resposta: não marca de novo
    assert criado["ultima_busca"]  # acabou de pesquisar: a próxima fica pra daqui ~3 h
    asyncio.run(criar.callback(interacao(7), perfume="Khamrah", preco=200.0))
    assert "já tem esse alerta" in respostas[-1]
    for i in range(MAX_ALERTAS_POR_PESSOA - 1):
        asyncio.run(criar.callback(interacao(7), perfume=f"perfume {i}", preco=200.0))
    asyncio.run(criar.callback(interacao(7), perfume="mais um", preco=200.0))
    assert f"o máximo é {MAX_ALERTAS_POR_PESSOA}" in respostas[-1]
    asyncio.run(lista.callback(interacao(7)))
    assert "`#1` khamrah até **R$ 250,00** · último aviso R$ 239,90" in respostas[-1]
    asyncio.run(remover.callback(interacao(8), alerta=1))  # não é dele
    assert "Não achei o alerta #1" in respostas[-1] and db.alerta_usuario(bot.con, 1)
    asyncio.run(remover.callback(interacao(7), alerta=1))
    assert "Parei o alerta #1" in respostas[-1] and db.alerta_usuario(bot.con, 1) is None


def test_embed_alerta_pessoal():
    from scentinel.fontes import Achado
    from scentinel.formato import embed_alerta_usuario

    k = Achado(titulo="Perfume Lattafa Khamrah EDP 100ml", url="https://ml/k", preco=239.9, loja="Mercado Livre",
               preco_de=299.9)
    e = embed_alerta_usuario({"id": 3, "consulta": "khamrah", "preco_max": 250, "ml": 100}, k, 100, 20)
    assert e["title"] == "🔔 khamrah 100ml chegou no seu preço" and "**20% OFF**" in e["description"]
    assert "Você pediu até R$ 250,00" in e["description"] and e["url"] == "https://ml/k"


def _guild_falsa(*nomes):
    from types import SimpleNamespace

    roles = [SimpleNamespace(id=i + 100, name=n, mention=f"<@&{i + 100}>") for i, n in enumerate(nomes)]
    criados = []

    async def create_role(name, **kw):
        criados.append((name, kw))
        roles.append(SimpleNamespace(id=len(roles) + 100, name=name, mention=f"<@&{len(roles) + 100}>"))

    return SimpleNamespace(roles=roles, create_role=create_role, criados=criados)


def test_cargos_marcados_na_promo():
    from datetime import timedelta

    from scentinel.bot import cargos_da_oferta

    guild = _guild_falsa("✨ Designer", "🕌 Árabe", "🔥 Todas as promos")
    agora, ultimos = db.agora_utc(), {}
    promo = oferta_exemplo(origem="descoberta", estilo="arabe")
    assert [c.name for c in cargos_da_oferta(guild, promo, ultimos, agora)] == ["🔥 Todas as promos", "🕌 Árabe"]
    # 10 min depois, outra promo árabe: não marca de novo (no máximo 1 vez a cada 30 min)
    assert cargos_da_oferta(guild, promo, ultimos, agora + timedelta(minutes=10)) == []
    assert [c.name for c in cargos_da_oferta(guild, promo, ultimos, agora + timedelta(minutes=31))] == [
        "🔥 Todas as promos", "🕌 Árabe"]
    # vitrine/sem nível, anúncio do /add e nível baixo não marcam ninguém
    assert cargos_da_oferta(guild, oferta_exemplo(origem="descoberta", estilo="arabe",
                                                  avaliacao=Avaliacao(1, 2, "loja", 50, None)), {}, agora) == []
    assert cargos_da_oferta(guild, oferta_exemplo(origem="manual", estilo="arabe"), {}, agora) == []
    baixo = Nivel(1, "Cheirinho", "🟢", 10, 0)
    assert cargos_da_oferta(guild, oferta_exemplo(origem="descoberta", avaliacao=Avaliacao(1, 2, "x", 12, baixo)),
                            {}, agora) == []


def test_botao_de_cargo_da_e_tira():
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import alternar_cargo

    guild = _guild_falsa("💎 Nicho")
    membro = SimpleNamespace(roles=[])
    respostas = []

    async def add_roles(cargo, **kw):
        membro.roles.append(cargo)

    async def remove_roles(cargo, **kw):
        membro.roles.remove(cargo)

    async def responder(texto, **kw):
        respostas.append((texto, kw))

    membro.add_roles, membro.remove_roles = add_roles, remove_roles
    interacao = SimpleNamespace(guild=guild, user=membro, response=SimpleNamespace(send_message=responder))
    asyncio.run(alternar_cargo(interacao, "💎 Nicho"))
    assert membro.roles and "Agora te marco" in respostas[-1][0] and respostas[-1][1]["ephemeral"]
    asyncio.run(alternar_cargo(interacao, "💎 Nicho"))
    assert membro.roles == [] and "tirei" in respostas[-1][0]
    asyncio.run(alternar_cargo(interacao, "🕌 Árabe"))
    assert "sumiu" in respostas[-1][0]


def test_comando_cargos_cria_o_que_falta_e_manda_botoes():
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import CARGOS, BotScentinel, BotoesCargos, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    cmd = bot.tree.get_command("cargos")
    assert cmd.default_permissions.administrator
    guild = _guild_falsa("✨ Designer")
    enviados, edicoes = [], []

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw["content"])

    async def send(**kw):
        enviados.append(kw)

    interacao = SimpleNamespace(client=bot, guild=guild, permissions=SimpleNamespace(administrator=True),
                                channel=SimpleNamespace(send=send), edit_original_response=editar,
                                response=SimpleNamespace(defer=defer))
    asyncio.run(cmd.callback(interacao))
    assert [n for n, _ in guild.criados] == [n for n in CARGOS.values() if n != "✨ Designer"]
    assert all(kw["mentionable"] for _, kw in guild.criados)
    assert isinstance(enviados[0]["view"], BotoesCargos) and "Criei" in edicoes[-1]
    ids = {b.custom_id for b in enviados[0]["view"].children}
    assert ids == {f"scentinel:cargo:{k}" for k in CARGOS}


def test_alerta_de_promo_marca_o_cargo():
    import asyncio
    from types import SimpleNamespace

    from scentinel.bot import BotScentinel

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), db.conectar(":memory:"))
    guild = _guild_falsa("🕌 Árabe", "🔥 Todas as promos")
    enviados = []

    async def send(**kw):
        enviados.append(kw)

    async def canal():
        return SimpleNamespace(send=send, guild=guild)

    bot.canal = canal
    assert asyncio.run(bot.enviar_alerta(oferta_exemplo(origem="descoberta", estilo="arabe"), mencao="dono"))
    kw = enviados[-1]
    assert kw["content"] == "<@3> <@&101> <@&100>"
    assert kw["allowed_mentions"].users is True and [c.name for c in kw["allowed_mentions"].roles] == [
        "🔥 Todas as promos", "🕌 Árabe"]


def test_catalogo_automatico_pelo_bot():
    import asyncio
    from types import SimpleNamespace

    from scentinel import autocatalogo
    from scentinel.bot import BotScentinel, registrar_comandos
    from scentinel.catalogo import ItemCatalogo

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 42), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    assert bot.loop_autocatalogo.time[0].hour == 20
    novo = ItemCatalogo("auto-lattafa-hayaati-edp", "Hayaati", "Lattafa", "EDP", [100.0], "masc", "arabe",
                        180, 100.0, 140, busca=["hayaati lattafa"])
    original = autocatalogo.escolher
    autocatalogo.escolher = lambda preparado, itens, removidos=(), limite=15: [(novo, 3)]
    enviados = []

    async def canal():
        async def send(**kw):
            enviados.append(kw["embed"])
        return SimpleNamespace(send=send)

    bot.canal = canal
    try:
        assert asyncio.run(bot.rodar_autocatalogo()) == [(novo, 3)]
    finally:
        autocatalogo.escolher = original
    assert novo in bot.descoberta.itens and bot.cacador.catalogo[novo.id] is novo
    assert enviados[0].title == "📚 1 perfume(s) novo(s) no catálogo"
    assert ("**Hayaati EDP** (Lattafa) · 🕌 Árabe · normal ~R$ 180,00 (100ml) · 💸 bom abaixo de R$ 140"
            in enviados[0].description)

    respostas = []

    async def responder(texto, **kw):
        respostas.append(texto)

    grupo = bot.tree.get_command("catalogo")

    def interacao(uid):
        return SimpleNamespace(client=bot, user=SimpleNamespace(id=uid),
                               response=SimpleNamespace(send_message=responder))

    asyncio.run(grupo.get_command("remover").callback(interacao(7), perfume=novo.id))
    assert "Só o dono" in respostas[-1] and novo in bot.descoberta.itens
    asyncio.run(grupo.get_command("novos").callback(interacao(42)))
    assert "Hayaati EDP (Lattafa)" in respostas[-1]
    asyncio.run(grupo.get_command("remover").callback(interacao(42), perfume=novo.id))
    assert "Tirei **Hayaati EDP**" in respostas[-1] and novo not in bot.descoberta.itens
    assert autocatalogo.itens_salvos() == [] and novo.id in autocatalogo.carregar()["removidos"]

    # reiniciou: o que estava salvo volta pro catálogo do bot
    autocatalogo.gravar([(ItemCatalogo("auto-x", "X Bravo", "Lattafa", "EDP", [100.0], "masc", "arabe", 200, 100.0,
                                       156), 2)])
    bot2 = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 42), db.conectar(":memory:"))
    assert any(i.id == "auto-x" for i in bot2.descoberta.itens)


def test_linha_de_preco_bom_em_todo_post_da_busca_automatica():
    from scentinel.formato import embed_vitrine

    # preço ainda acima do bom: a linha aparece mesmo assim (diz a partir de quanto vale a pena)
    acima = oferta_exemplo(origem="descoberta", preco=659.90, preco_bom=559, alvo=559.92, bateu_alvo=False)
    for e in (embed_alerta(acima), embed_vitrine(acima)):
        assert "💸 Abaixo de R$ 559, preço bom pra esse perfume · ⚠️ esse ainda tá acima" in e["description"]
    abaixo = oferta_exemplo(origem="descoberta", preco=499.90, preco_bom=559)
    assert "💸 Abaixo de R$ 559, preço bom pra esse perfume · ✅ esse tá bom" in embed_alerta(abaixo)["description"]
    # não sabe ainda: diz que não sabe (não fica calado)
    sem = oferta_exemplo(origem="descoberta", preco_bom=None)
    assert "💸 Ainda não sei o preço bom desse perfume" in embed_alerta(sem)["description"]
    # anúncio do /add continua com o alvo do dono
    manual = oferta_exemplo(alvo=700, bateu_alvo=True, preco_bom=None)
    d = embed_alerta(manual)["description"]
    assert "🎯 Bateu seu alvo de R$ 700,00" in d and "preço bom pra esse perfume" not in d



def test_preco_bom_no_locate_no_top_e_no_alerta_pessoal():
    from scentinel.descoberta import ResultadoAoVivo
    from scentinel.fontes import Achado
    from scentinel.formato import embed_alerta_usuario, embed_ao_vivo, embed_top, embed_top_semana

    k = Achado(titulo="Perfume Lattafa Khamrah EDP 100ml", url="https://ml/k", preco=239.9, loja="Mercado Livre")
    res = ResultadoAoVivo("khamrah", ofertas=[(k, 100, 0)], lojas_ok=["Mercado Livre"], preco_bom=200)
    assert "💸 Abaixo de R$ 200, preço bom pra esse perfume · ⚠️ esse ainda tá acima" in embed_ao_vivo(res)["description"]
    res.preco_bom = None
    assert "Ainda não sei o preço bom" in embed_ao_vivo(res)["description"]
    al = embed_alerta_usuario({"id": 1, "consulta": "khamrah", "preco_max": 250, "ml": None}, k, 100, 0, 260)
    assert "✅ esse tá bom" in al["description"]
    o = oferta_exemplo(origem="descoberta", preco=659.90, preco_bom=700)
    assert "✅ bom abaixo de R$ 700" in embed_top([o])["description"]
    sem = {"anuncio": {"perfume_nome": "X", "ml": 100, "url": "https://x"}, "pct": 30, "preco": 199,
           "quando": db.agora_utc(), "agora": oferta_exemplo(origem="descoberta", preco=199, preco_bom=None)}
    assert "preço bom: ainda não sei" in embed_top_semana([sem])["description"]
