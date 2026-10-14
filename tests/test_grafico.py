"""Gráfico do /historico."""

from datetime import UTC, date, datetime, timedelta

from scentinel import db, grafico
from scentinel.coleta import Leitura


def _banco_com_precos():
    con = db.conectar(":memory:")
    p = db.criar_perfume(con, "Sauvage EDP", "sauvage edp", marca="Dior")
    a100 = db.criar_anuncio(con, p["id"], "Mercado Livre", "https://x/1", ml=100)
    b100 = db.criar_anuncio(con, p["id"], "Mercado Livre", "https://x/2", ml=100)
    a60 = db.criar_anuncio(con, p["id"], "Mercado Livre", "https://x/3", ml=60)
    agora = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
    for dia, preco_a, preco_b in ((3, 800, 790), (2, 780, 820), (1, 760, 750)):
        quando = agora - timedelta(days=dia)
        db.registrar_preco(con, a100["id"], Leitura(preco_a, "teste"), quando)
        db.registrar_preco(con, b100["id"], Leitura(preco_b, "teste"), quando + timedelta(hours=1))
    db.registrar_preco(con, a100["id"], Leitura(100, "teste", esgotado=True), agora)  # esgotado não conta
    db.registrar_preco(con, a60["id"], Leitura(600, "teste"), agora)
    db.registrar_preco(con, a100["id"], Leitura(999, "teste"), agora - timedelta(days=40))  # fora dos 30 dias
    return con, p, agora


def test_serie_menor_preco_por_dia_do_tamanho_com_mais_dias():
    con, p, agora = _banco_com_precos()
    ml, pontos = grafico.serie_do_perfume(con, p["id"], 30, agora)
    assert ml == 100
    assert pontos == [(date(2026, 10, 11), 790), (date(2026, 10, 12), 780), (date(2026, 10, 13), 750)]


def test_desenha_png():
    con, p, agora = _banco_com_precos()
    titulo, pontos = grafico.dados_do_grafico(con, p, agora=agora)
    assert titulo == "Sauvage EDP 100ml · menor preço por dia (30 dias)"
    png = grafico.desenhar(titulo, pontos)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000
    assert grafico.desenhar(titulo, pontos, fontes=())[:4] == b"\x89PNG"  # sem fonte com acento: ainda desenha
    assert grafico.desenhar(titulo, pontos[:1]) is None  # 1 dia só: sem gráfico


def test_historico_manda_o_grafico():
    import asyncio
    from types import SimpleNamespace

    from scentinel import config
    from scentinel.bot import BotScentinel, registrar_comandos

    con, p, agora = _banco_com_precos()
    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 3), con)
    registrar_comandos(bot.tree)
    edicoes = []

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw)

    original = db.agora_utc
    db.agora_utc = lambda: agora
    try:
        interacao = SimpleNamespace(client=bot, response=SimpleNamespace(defer=defer), edit_original_response=editar)
        asyncio.run(bot.tree.get_command("historico").callback(interacao, nome="sauvage"))
    finally:
        db.agora_utc = original
    kw = edicoes[-1]
    assert kw["embed"].image.url == "attachment://historico.png" and kw["attachments"][0].filename == "historico.png"
