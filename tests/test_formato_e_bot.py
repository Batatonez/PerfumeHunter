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
    assert "R$ 659,90" in d and "(ref. R$ 999,00)" in d and "R$ 6,60/ml" in d
    assert "⚡ Pix: R$ 626,90" in d
    assert "🏆 RECORDE" in d
    assert e["footer"]["text"] == "Checado em 11/10 15:30"


def test_embed_alerta_preco_da_loja_e_alvo():
    av = Avaliacao(700, 1000, "loja", 30, RASTRO)
    e = embed_alerta(oferta_exemplo(avaliacao=av, bateu_alvo=True, alvo=750))
    assert "📢 desconto anunciado pela loja" in e["description"]
    assert "🎯 Bateu seu alvo de R$ 750,00" in e["description"]


def test_embed_locate():
    melhor = oferta_exemplo()
    outro = oferta_exemplo(anuncio_id=2, loja="Loja A", avaliacao=Avaliacao(880, 1000, "historico", 12, None))
    barato = oferta_exemplo(anuncio_id=3, loja="Loja C", ml=200, preco=1080)
    e = embed_locate(melhor, True, [outro], barato, ["Sauvage EDT"])
    assert e["title"] == "🏆 Maior promo: Sauvage EDP 100ml — Loja X"
    assert "**Outros:** Loja A −12%" in e["description"]
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
                     "status", "despausar", "clear", "send"}


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
    assert "client_id=999&scope=bot+applications.commands&permissions=224256" in link_convite(999)


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
    assert bot.loop_vitrine.minutes == 15
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
    respostas, seguintes, pedidos = [], [], []

    async def responder(texto, **kw):
        respostas.append(texto)

    async def defer(**kw):
        pass

    async def seguir(texto, **kw):
        seguintes.append(texto)

    async def mandar_varias(n, enviar):
        pedidos.append(n)
        return 2

    bot.descoberta.mandar_varias = mandar_varias

    def interacao(admin):
        return SimpleNamespace(client=bot, permissions=SimpleNamespace(administrator=admin),
                               response=SimpleNamespace(send_message=responder, defer=defer),
                               followup=SimpleNamespace(send=seguir))

    asyncio.run(cmd.callback(interacao(False), numero=5))
    assert "Só admin" in respostas[-1] and pedidos == []
    asyncio.run(cmd.callback(interacao(True), numero=5))
    assert pedidos == [5] and seguintes[-1].startswith("🛍️ Mandei 2 de 5")


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
    assert edicoes[-1]["embed"].description.startswith("Não achei **sauvage**")
