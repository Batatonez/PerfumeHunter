"""Testes do caçador inteiro: banco em memória, rede e relógio falsos."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from scentinel import config, db
from scentinel.caca import Cacador, limpar_nome, ranquear
from scentinel.coleta import Leitura, Resultado


class Relogio:
    def __init__(self):
        self.agora = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.agora

    def avancar(self, **kw):
        self.agora += timedelta(**kw)


class Rede:
    """Fila de respostas por URL. Um float vira uma leitura com aquele preço."""

    def __init__(self):
        self.respostas = {}
        self.ultima = {}
        self.chamadas = []

    def programar(self, url, *respostas):
        self.respostas.setdefault(url, []).extend(respostas)

    def __call__(self, url, seletor=None):
        """Consome a fila; com a fila vazia repete a última resposta."""
        self.chamadas.append(url)
        fila = self.respostas.get(url)
        if fila:
            self.ultima[url] = fila.pop(0)
        r = self.ultima.get(url, Resultado(erro="sem resposta programada"))
        if isinstance(r, (int, float)):
            return Resultado(leitura=Leitura(float(r), "json-ld"))
        if isinstance(r, Leitura):
            return Resultado(leitura=r)
        return r


@pytest.fixture
def ambiente():
    cfg = config.carregar_config()
    con = db.conectar(":memory:")
    rede = Rede()
    relogio = Relogio()
    alertas, avisos, dormidas = [], [], []

    async def enviar_alerta(oferta, mencao):
        alertas.append((oferta, mencao))
        return True

    async def enviar_aviso(texto):
        avisos.append(texto)

    async def dormir(s):
        dormidas.append(s)

    cacador = Cacador(con, cfg, enviar_alerta, enviar_aviso, baixar=rede, dormir=dormir, relogio=relogio)

    class Amb:
        pass

    amb = Amb()
    amb.__dict__.update(locals())
    return amb


def rodar(coro):
    return asyncio.run(coro)


def cadastrar(amb, url, nome="Sauvage EDP", ml=100, alvo=None, preco_inicial=1000.0):
    amb.rede.programar(url, preco_inicial)
    anuncio, chk = rodar(amb.cacador.adicionar(url, nome, ml=ml, alvo=alvo))
    return anuncio, chk


def rodada_depois(amb, horas=3):
    amb.relogio.avancar(hours=horas)
    return rodar(amb.cacador.rodada())


# ============================================================
# /add
# ============================================================

def test_limpar_nome():
    assert limpar_nome("Sauvage EDP 100ml") == "Sauvage EDP"
    assert limpar_nome("Kaiak 100 ML - Natura") == "Kaiak - Natura"


def test_adicionar_testa_na_hora_e_nao_avisa(ambiente):
    url = "https://www.mercadolivre.com.br/sauvage"
    anuncio, chk = cadastrar(ambiente, url, nome="Sauvage EDP 100ml")
    assert anuncio["loja"] == "Mercado Livre"
    assert anuncio["perfume_nome"] == "Sauvage EDP"
    assert chk.oferta.preco == 1000.0
    assert ambiente.alertas == []
    with pytest.raises(ValueError):
        rodar(ambiente.cacador.adicionar(url, "outro", ml=50))


def test_mesmo_perfume_reaproveita_cadastro(ambiente):
    a1, _ = cadastrar(ambiente, "https://loja-a.com/x", "Sauvage EDP")
    a2, _ = cadastrar(ambiente, "https://loja-b.com/x", "sauvage edp 200ml", ml=200)
    assert a1["perfume_id"] == a2["perfume_id"]
    assert len(db.listar_perfumes(ambiente.con)) == 1


# ============================================================
# rodada + níveis + anti-spam
# ============================================================

def construir_historico(amb, url, preco=1000.0, n=5):
    """Cadastra e faz n leituras (3 h entre elas) no mesmo preço."""
    anuncio, _ = cadastrar(amb, url, preco_inicial=preco)
    for _ in range(n - 1):
        amb.rede.programar(url, preco)
        rodada_depois(amb)
    return anuncio


def test_respeita_intervalo_de_3h(ambiente):
    url = "https://loja.com/a"
    cadastrar(ambiente, url)
    ambiente.relogio.avancar(hours=1)
    assert rodar(ambiente.cacador.rodada()) == []
    ambiente.rede.programar(url, 1000.0)
    assert len(rodada_depois(ambiente, horas=2)) == 1


def test_fluxo_completo_de_niveis(ambiente):
    url = "https://loja.com/a"
    construir_historico(ambiente, url, 1000.0, n=5)
    assert ambiente.alertas == []

    # 15% off -> nível 1
    ambiente.rede.programar(url, 850.0)
    rodada_depois(ambiente)
    assert len(ambiente.alertas) == 1
    oferta, mencao = ambiente.alertas[-1]
    assert oferta.avaliacao.nivel.nivel == 1 and mencao is None

    # continua 15% -> não repete
    ambiente.rede.programar(url, 850.0)
    rodada_depois(ambiente)
    assert len(ambiente.alertas) == 1

    # 42% -> nível 4, marca só o dono
    ambiente.rede.programar(url, 580.0)
    rodada_depois(ambiente)
    assert len(ambiente.alertas) == 2
    oferta, mencao = ambiente.alertas[-1]
    assert oferta.avaliacao.nivel.nivel == 4 and mencao == "dono"
    assert oferta.recorde

    # volta pro preço cheio -> reset
    ambiente.rede.programar(url, 1000.0)
    rodada_depois(ambiente)
    anuncio = db.anuncio_por_url(ambiente.con, url)
    assert anuncio["nivel_avisado"] == 0

    # nova queda avisa de novo
    ambiente.rede.programar(url, 850.0)
    rodada_depois(ambiente)
    assert len(ambiente.alertas) == 3

    tipos = [l["tipo"] for l in ambiente.con.execute("SELECT tipo FROM alertas")]
    assert tipos == ["nivel", "nivel", "nivel"]


def test_sem_historico_usa_preco_de_da_loja(ambiente):
    url = "https://loja.com/a"
    cadastrar(ambiente, url, preco_inicial=1000.0)
    ambiente.rede.programar(url, Leitura(700.0, "json-ld", preco_de=1000.0))
    rodada_depois(ambiente)
    oferta, _ = ambiente.alertas[-1]
    assert oferta.avaliacao.fonte == "loja"
    assert oferta.avaliacao.nivel.nivel == 3


def test_esgotado_nao_avisa(ambiente):
    url = "https://loja.com/a"
    construir_historico(ambiente, url)
    ambiente.rede.programar(url, Leitura(500.0, "json-ld", esgotado=True))
    rodada_depois(ambiente)
    assert ambiente.alertas == []


def test_alvo(ambiente):
    url = "https://loja.com/a"
    cadastrar(ambiente, url, alvo=950.0, preco_inicial=1000.0)
    ambiente.rede.programar(url, 940.0)
    rodada_depois(ambiente)
    oferta, _ = ambiente.alertas[-1]
    assert oferta.bateu_alvo and oferta.avaliacao.nivel is None
    ambiente.rede.programar(url, 930.0)
    rodada_depois(ambiente)
    assert len(ambiente.alertas) == 1  # não repete


def test_falha_no_discord_nao_marca_como_avisado(ambiente):
    url = "https://loja.com/a"
    construir_historico(ambiente, url)

    async def falha(oferta, mencao):
        return False

    ambiente.cacador.enviar_alerta = falha
    ambiente.rede.programar(url, 850.0)
    rodada_depois(ambiente)
    assert db.anuncio_por_url(ambiente.con, url)["nivel_avisado"] == 0


def test_delay_entre_requisicoes(ambiente):
    for i in range(3):
        cadastrar(ambiente, f"https://loja.com/{i}")
    ambiente.dormidas.clear()
    for i in range(3):
        ambiente.rede.programar(f"https://loja.com/{i}", 1000.0)
    rodada_depois(ambiente)
    assert len(ambiente.dormidas) == 3
    assert all(0 < s <= 15 for s in ambiente.dormidas)


# ============================================================
# sanidade
# ============================================================

def test_queda_grande_confirmada_avisa(ambiente):
    url = "https://loja.com/a"
    construir_historico(ambiente, url)
    ambiente.dormidas.clear()
    ambiente.rede.programar(url, 300.0, 300.0)
    rodada_depois(ambiente)
    assert 120 in ambiente.dormidas
    oferta, mencao = ambiente.alertas[-1]
    assert oferta.avaliacao.nivel.nivel == 5 and mencao == "everyone"


def test_queda_grande_nao_confirmada_nao_avisa(ambiente):
    url = "https://loja.com/a"
    construir_historico(ambiente, url)
    ambiente.rede.programar(url, 99.0, 990.0)  # parser leu errado da 1ª vez
    rodada_depois(ambiente)
    assert ambiente.alertas == []
    ultima = db.ultima_leitura(ambiente.con, db.anuncio_por_url(ambiente.con, url)["id"])
    assert ultima["preco"] == 990.0


# ============================================================
# bloqueio
# ============================================================

def test_bloqueio_avisa_uma_vez_e_faz_backoff(ambiente):
    url = "https://loja.com/a"
    cadastrar(ambiente, url)
    ambiente.rede.programar(url, Resultado(erro="bloqueado (HTTP 403)", bloqueado=True))
    rodada_depois(ambiente)
    assert len(ambiente.avisos) == 1
    anuncio = db.anuncio_por_url(ambiente.con, url)
    assert anuncio["bloqueado"] == 1

    # dentro do backoff (6 h) não tenta
    n = len(ambiente.rede.chamadas)
    rodada_depois(ambiente, horas=4)
    assert len(ambiente.rede.chamadas) == n

    # depois do backoff tenta, bloqueia de novo, mas não avisa de novo
    rodada_depois(ambiente, horas=3)
    assert len(ambiente.rede.chamadas) == n + 1
    assert len(ambiente.avisos) == 1
    assert db.anuncio_por_url(ambiente.con, url)["falhas_bloqueio"] == 2

    # desbloqueou
    ambiente.rede.programar(url, 1000.0)
    rodada_depois(ambiente, horas=13)
    assert db.anuncio_por_url(ambiente.con, url)["bloqueado"] == 0


# ============================================================
# /locate
# ============================================================

def test_locate_maior_promo_e_variacoes(ambiente):
    construir_historico(ambiente, "https://loja-a.com/edp", 1000.0)
    construir_historico(ambiente, "https://loja-b.com/edp", 1000.0)
    cadastrar(ambiente, "https://loja-c.com/edp200", "Sauvage EDP", ml=200, preco_inicial=1100.0)
    cadastrar(ambiente, "https://loja-d.com/edt", "Sauvage EDT", preco_inicial=800.0)

    ambiente.rede.programar("https://loja-a.com/edp", 880.0)
    ambiente.rede.programar("https://loja-b.com/edp", 690.0)
    ambiente.rede.programar("https://loja-c.com/edp200", 1100.0)
    ambiente.rede.programar("https://loja-d.com/edt", 800.0)
    rodada_depois(ambiente)

    r = rodar(ambiente.cacador.locate("sauvage"))
    assert r.em_promo
    assert r.melhor.loja == "Loja-b"
    assert round(r.melhor.avaliacao.pct) == 31
    assert r.outros[0].loja == "Loja-a"
    assert r.menor_por_ml.loja == "Loja-c"  # 5,50/ml
    assert r.outras_variacoes == ["Sauvage EDT"]

    r2 = rodar(ambiente.cacador.locate("sauvage edt"))
    assert not r2.em_promo
    assert r2.melhor.perfume == "Sauvage EDT"
    assert r2.outras_variacoes == []


def test_locate_nao_achou(ambiente):
    r = rodar(ambiente.cacador.locate("one million"))
    assert "Não achei" in r.texto


def test_locate_rechecagem_de_leitura_velha(ambiente):
    url = "https://loja.com/a"
    cadastrar(ambiente, url, preco_inicial=1000.0)
    ambiente.relogio.avancar(hours=7)
    ambiente.rede.programar(url, 900.0)
    avisos = []

    async def ao_rechecar(n):
        avisos.append(n)

    r = rodar(ambiente.cacador.locate("sauvage", ao_rechecar=ao_rechecar))
    assert avisos == [1]
    assert r.melhor.preco == 900.0


def test_ranquear_empate_ganha_menor_preco(ambiente):
    from scentinel.promo import Avaliacao, Oferta

    n1 = ambiente.cfg.niveis[0]

    def oferta(i, preco, pct):
        return Oferta(i, "X", f"L{i}", "u", 100, preco, Avaliacao(preco, 1, "historico", pct, n1))

    melhor, em_promo, outros, _ = ranquear([oferta(1, 500, 15), oferta(2, 400, 15), oferta(3, 300, 12)])
    assert melhor.anuncio_id == 2 and em_promo
    assert [o.anuncio_id for o in outros] == [1, 3]
