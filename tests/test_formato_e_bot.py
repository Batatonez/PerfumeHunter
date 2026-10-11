from datetime import UTC, datetime

from cacador import config, db
from cacador.formato import embed_alerta, embed_locate, formatar_data, formatar_reais
from cacador.promo import Avaliacao, Nivel, Oferta

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


def test_config_nao_deixa_baixar_de_3h(tmp_path):
    texto = (config.ARQ_CONFIG.read_text(encoding="utf-8")
             .replace("intervalo_horas = 3", "intervalo_horas = 1"))
    arq = tmp_path / "config.toml"
    arq.write_text(texto, encoding="utf-8")
    assert config.carregar_config(arq).intervalo_horas == 3


def test_comandos_registram_sem_erro():
    """Monta o bot (sem conectar) e confere os slash commands."""
    from cacador.bot import BotCacador, registrar_comandos

    segredos = config.Segredos("x", 1, 2, 3)
    bot = BotCacador(config.carregar_config(), segredos, db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    nomes = {c.name for c in bot.tree.get_commands()}
    assert nomes == {"locate", "add", "remove", "lista", "historico", "checar", "teste_alerta"}
