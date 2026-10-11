"""Busca automática de ponta a ponta: loja falsa -> casamento -> alerta (ou não)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from scentinel import config, db
from scentinel.caca import Cacador
from scentinel.catalogo import ItemCatalogo
from scentinel.descoberta import ERROS_PRA_PAUSAR, Descoberta
from scentinel.fontes import Achado, ResultadoBusca

ITENS = [
    ItemCatalogo("sauvage-edp", "Sauvage", "Dior", "EDP", [60, 100], "masc", "designer", 850, 100, 660,
                 busca=["sauvage dior edp"]),
    ItemCatalogo("khamrah", "Khamrah", "Lattafa", "EDP", [100], "unissex", "arabe", 280, 100, 200,
                 busca=["lattafa khamrah"], evitar=["qahwa"]),
]


class FonteFalsa:
    def __init__(self, id_, nome):
        self.id, self.nome = id_, nome
        self.categorias, self.marcas = set(), set()
        self.respostas = {}  # termo -> lista de ResultadoBusca (consome em ordem; repete a última)
        self.chamadas = []

    def programar(self, termo, *achados, erro=None, bloqueado=False):
        res = ResultadoBusca(achados=list(achados), erro=erro, bloqueado=bloqueado)
        self.respostas.setdefault(termo, []).append(res)

    varredura = None  # lista de ResultadoBusca, uma por "página"

    def tarefas_varredura(self):
        return [(f"{self.nome} pág. {i + 1}", (lambda r=r: r)) for i, r in enumerate(self.varredura or [])]

    def buscar(self, termo):
        self.chamadas.append(termo)
        fila = self.respostas.get(termo) or [ResultadoBusca()]
        return fila.pop(0) if len(fila) > 1 else fila[0]


def achado(titulo, preco, loja, url, **kw):
    return Achado(titulo=titulo, url=url, preco=preco, loja=loja, **kw)


class Relogio:
    def __init__(self):
        self.agora = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.agora


@pytest.fixture
def amb():
    cfg = config.carregar_config()
    cfg.descoberta.max_alertas_por_hora = 10
    con = db.conectar(":memory:")
    relogio = Relogio()
    alertas, avisos = [], []

    async def enviar_alerta(oferta, mencao):
        alertas.append((oferta, mencao))
        return True

    async def enviar_aviso(texto):
        avisos.append(texto)

    async def dormir(_s):
        relogio.agora += timedelta(seconds=_s)

    cac = Cacador(con, cfg, enviar_alerta, enviar_aviso, dormir=dormir, relogio=relogio)
    a, b = FonteFalsa("vtex:a", "Loja A"), FonteFalsa("vtex:b", "Loja B")
    desc = Descoberta(cac, ITENS, [a, b], enviar_aviso=enviar_aviso)

    class Amb:
        pass

    x = Amb()
    x.__dict__.update(cfg=cfg, con=con, relogio=relogio, alertas=alertas, avisos=avisos, cac=cac, a=a, b=b, desc=desc)
    return x


def rodar(coro):
    return asyncio.run(coro)


def test_promo_boa_vira_post_com_categoria(amb):
    amb.a.programar("sauvage dior edp", achado("Sauvage Dior Eau de Parfum 100ml", 599.9, "Loja A", "https://a/sauvage"))
    rodar(amb.desc.rodada())
    assert len(amb.alertas) == 1
    oferta, mencao = amb.alertas[0]
    # 850 de mercado -> 599,90 = 29% (nível 2) e abaixo do "preço bom" (660)
    assert oferta.avaliacao.nivel.nivel == 2 and oferta.avaliacao.fonte == "catalogo"
    assert oferta.bateu_alvo and oferta.origem == "descoberta" and oferta.categoria == "✨ Designer"
    assert mencao is None
    anuncio = db.anuncio_por_url(amb.con, "https://a/sauvage")
    assert anuncio["origem"] == "descoberta" and anuncio["catalogo_id"] == "sauvage-edp" and anuncio["ml"] == 100


def test_preco_normal_nao_posta(amb):
    amb.a.programar("sauvage dior edp", achado("Sauvage Dior Eau de Parfum 100ml", 830, "Loja A", "https://a/s",
                                               preco_de=1100))  # "de 1100" inflado, mas o mercado é 850
    rodar(amb.desc.rodada())
    assert amb.alertas == []


def test_lixo_e_tamanho_desconhecido_ficam_fora(amb):
    amb.a.programar(
        "sauvage dior edp",
        achado("Kit Sauvage Dior EDP 100ml + Gel de Banho", 300, "Loja A", "https://a/kit"),
        achado("Decant Sauvage Dior EDP 10ml", 90, "Loja A", "https://a/decant"),
        achado("Sauvage Dior Eau de Parfum", 300, "Loja A", "https://a/semml"),
        achado("Sauvage Dior Eau de Parfum 30ml", 300, "Loja A", "https://a/30ml"),  # 30 ml não está no catálogo
    )
    rodar(amb.desc.rodada())
    assert amb.alertas == []
    assert db.anuncio_por_url(amb.con, "https://a/kit") is None


def test_so_posta_o_mais_barato(amb):
    amb.a.programar("sauvage dior edp", achado("Sauvage Dior EDP 100ml", 580, "Loja A", "https://a/s"))
    amb.b.programar("sauvage dior edp", achado("Sauvage Dior EDP 100ml", 620, "Loja B", "https://b/s"))
    rodar(amb.desc.rodada())
    lojas = [o.loja for o, _ in amb.alertas]
    assert lojas == ["Loja A"]  # a B (mais cara) foi segurada
    # e o estado da B não mudou: se ela virar a mais barata depois, ainda pode avisar
    assert db.anuncio_por_url(amb.con, "https://b/s")["nivel_avisado"] == 0


def test_limite_de_posts_por_hora(amb):
    amb.cfg.descoberta.max_alertas_por_hora = 1
    amb.a.programar("sauvage dior edp", achado("Sauvage Dior EDP 100ml", 580, "Loja A", "https://a/s"))
    amb.a.programar("lattafa khamrah", achado("Lattafa Khamrah EDP 100ml", 190, "Loja A", "https://a/k"))
    rodar(amb.desc.rodada())
    assert len(amb.alertas) == 1


def test_nao_repete_e_respeita_intervalo(amb):
    amb.a.programar("sauvage dior edp", achado("Sauvage Dior EDP 100ml", 580, "Loja A", "https://a/s"))
    rodar(amb.desc.rodada())
    n_chamadas = len(amb.a.chamadas)
    rodar(amb.desc.rodada())  # logo em seguida: nada vencido
    assert len(amb.a.chamadas) == n_chamadas
    amb.relogio.agora += timedelta(hours=6)
    rodar(amb.desc.rodada())  # mesmo preço 6 h depois: busca de novo, mas não repete o post
    assert len(amb.a.chamadas) > n_chamadas
    assert len(amb.alertas) == 1


def test_loja_que_bloqueia_pausa_e_avisa_uma_vez(amb):
    amb.a.programar("sauvage dior edp", erro="bloqueado (HTTP 429)", bloqueado=True)
    rodar(amb.desc.rodada())
    assert len(amb.avisos) == 1 and "Loja A" in amb.avisos[0]
    # pausada nas primeiras 6 h (backoff_horas do config.toml)...
    assert all(f.id != "vtex:a" for _, f in amb.desc.pendentes(amb.relogio.agora + timedelta(hours=5)))
    # ...e volta depois
    assert any(f.id == "vtex:a" for _, f in amb.desc.pendentes(amb.relogio.agora + timedelta(hours=7)))


def test_loja_que_so_da_erro_pausa(amb):
    for termo in ("sauvage dior edp", "lattafa khamrah"):
        amb.a.programar(termo, erro="HTTP 404")
    amb.relogio.agora += timedelta(seconds=1)
    for _ in range(ERROS_PRA_PAUSAR):
        for item in ITENS:
            rodar(amb.desc.buscar_item(item, amb.a))
    assert amb.desc._fonte_pausada_ate.get("vtex:a")
    assert amb.avisos == []  # erro comum não incomoda o canal


def test_top_do_dia(amb):
    amb.a.programar("sauvage dior edp",
                    achado("Sauvage Dior EDP 100ml", 580, "Loja A", "https://a/s"),
                    achado("Sauvage Dior EDP 60ml", 520, "Loja A", "https://a/s60"))
    amb.a.programar("lattafa khamrah", achado("Lattafa Khamrah EDP 100ml", 150, "Loja A", "https://a/k"))
    rodar(amb.desc.rodada())
    top = amb.desc.top()
    # um por perfume, maior desconto primeiro (Khamrah: 280 -> 150 = 46%)
    assert [o.perfume for o in top] == ["Khamrah EDP", "Sauvage EDP"]
    assert top[0].avaliacao.pct > top[1].avaliacao.pct


def test_locate_acha_os_descobertos(amb):
    amb.a.programar("sauvage dior edp", achado("Sauvage Dior EDP 100ml", 580, "Loja A", "https://a/s"))
    amb.b.programar("sauvage dior edp", achado("Sauvage Dior EDP 100ml", 760, "Loja B", "https://b/s"))
    rodar(amb.desc.rodada())
    r = rodar(amb.cac.locate("sauvage", marca="dior"))
    # 580 contra 760 na 2ª loja mais barata = 24% abaixo
    assert r.melhor.avaliacao.fonte == "mercado" and round(r.melhor.avaliacao.pct) == 24
    assert r.melhor.loja == "Loja A" and r.em_promo
    assert [o.loja for o in r.outros] == ["Loja B"]



# ============================================================
# VARREDURA: qualquer perfume em promo, não só o catálogo
# ============================================================

def pagina(*achados):
    return ResultadoBusca(achados=list(achados))


def test_varredura_posta_perfume_fora_do_catalogo(amb):
    amb.a.varredura = [pagina(
        achado("Perfume Queening Le Parfum Feminino EDP 100ml", 199.9, "Loja A", "https://a/q",
               preco_de=399.9, imagem="https://img/q.jpg", oficial=True),
        achado("Hidratante Corporal Perfumado 400ml", 30, "Loja A", "https://a/h", preco_de=60),
        achado("Kit Perfume Good Girl + Loção", 300, "Loja A", "https://a/kit", preco_de=600),
        achado("Perfume Miniatura X EDP 5ml", 50, "Loja A", "https://a/mini", preco_de=100),
        achado("Perfume Y EDT 100ml sem desconto", 300, "Loja A", "https://a/y"),
    )]
    rodar(amb.desc.varredura())
    assert len(amb.alertas) == 1
    oferta, _ = amb.alertas[0]
    assert oferta.url == "https://a/q" and round(oferta.avaliacao.pct) == 50
    assert oferta.avaliacao.fonte == "loja" and oferta.preco_de == 399.9 and oferta.imagem == "https://img/q.jpg"
    assert oferta.perfume == "Queening Le Parfum EDP"
    # o resto nem virou anúncio (não é perfume, é kit, é miniatura, ou está a preço cheio)
    for url in ("https://a/h", "https://a/kit", "https://a/mini", "https://a/y"):
        assert db.anuncio_por_url(amb.con, url) is None


def test_varredura_posta_maiores_descontos_primeiro(amb):
    amb.cfg.descoberta.max_alertas_por_hora = 2
    amb.a.varredura = [pagina(
        achado("Perfume Um EDP 100ml", 380, "Loja A", "https://a/1", preco_de=500),    # 24%
        achado("Perfume Dois EDP 100ml", 200, "Loja A", "https://a/2", preco_de=500),  # 60%
        achado("Perfume Tres EDP 100ml", 300, "Loja A", "https://a/3", preco_de=500),  # 40%
    )]
    amb.cfg.sanidade_queda_pct = 99  # sem rechecagem nesse teste
    rodar(amb.desc.varredura())
    assert [o.url for o, _ in amb.alertas] == ["https://a/2", "https://a/3"]  # o de 24% ficou pro limite


def test_varredura_compara_entre_lojas(amb):
    # mesmo produto nas duas lojas (chave pelo título): a mais cara não posta
    amb.a.varredura = [pagina(achado("Perfume Afnan 9PM EDP Masculino 100ml", 229, "Loja A", "https://a/9", preco_de=399))]
    amb.b.varredura = [pagina(achado("Afnan 9PM Eau de Parfum 100ml Original", 319.9, "Loja B", "https://b/9", preco_de=450))]
    rodar(amb.desc.varredura())
    assert [o.loja for o, _ in amb.alertas] == ["Loja A"]
    a9 = db.anuncio_por_url(amb.con, "https://a/9")
    b9 = db.anuncio_por_url(amb.con, "https://b/9")
    assert a9["catalogo_id"] == b9["catalogo_id"]  # reconheceu que é o mesmo perfume


def test_varredura_respeita_intervalo_e_nao_repete(amb):
    amb.a.varredura = [pagina(achado("Perfume Um EDP 100ml", 200, "Loja A", "https://a/1", preco_de=500))]
    amb.cfg.sanidade_queda_pct = 99
    rodar(amb.desc.varredura())
    assert rodar(amb.desc.varredura()) == []  # antes de 1 h não varre de novo
    amb.relogio.agora += timedelta(hours=2)
    rodar(amb.desc.varredura())
    assert len(amb.alertas) == 1  # mesmo preço: não repete o post


def test_varredura_usa_catalogo_quando_reconhece(amb):
    amb.a.varredura = [pagina(achado("Sauvage Dior Eau de Parfum 100ml", 599.9, "Loja A", "https://a/s", preco_de=620))]
    rodar(amb.desc.varredura())
    oferta, _ = amb.alertas[0]
    # a loja diz só 3% OFF (620 -> 599,90) e a referência fica com o menor (620): sem nível...
    assert oferta.avaliacao.nivel is None and oferta.categoria == "✨ Designer"
    # ...mas 599,90 está abaixo do "preço bom" do catálogo (660): posta como 💸 PREÇO BOM
    assert oferta.bateu_alvo and oferta.alvo == 660


# ============================================================
# AO VIVO: /locate de qualquer perfume
# ============================================================

def test_busca_ao_vivo(amb):
    amb.a.programar("queening", achado("Queening EDP Feminino 100ml", 210, "Loja A", "https://a/q", preco_de=300),
                    achado("Body Splash Queening 200ml", 50, "Loja A", "https://a/bs"))
    amb.b.programar("queening", achado("Perfume Queening Eau de Parfum 100ml", 189.9, "Loja B", "https://b/q"),
                    achado("Outro Perfume Qualquer 100ml", 99, "Loja B", "https://b/x"))
    res = rodar(amb.desc.buscar_ao_vivo("queening"))
    assert [a.url for a, _, _ in res.ofertas] == ["https://b/q", "https://a/q"]  # mais barato primeiro, sem lixo
    assert res.lojas_ok == ["Loja A", "Loja B"]
    assert amb.alertas == []  # pesquisa não posta no canal
    assert db.anuncio_por_url(amb.con, "https://a/q") is not None  # mas guarda pro histórico


def test_busca_ao_vivo_com_marca_e_loja_fora(amb):
    amb.a.programar("sauvage dior", achado("Sauvage Dior EDP 100ml", 650, "Loja A", "https://a/s"),
                    achado("Sauvage Imitação Genérica 100ml", 80, "Loja A", "https://a/fake"))
    amb.b.programar("sauvage dior", erro="HTTP 500")
    res = rodar(amb.desc.buscar_ao_vivo("sauvage", "dior"))
    assert [a.url for a, _, _ in res.ofertas] == ["https://a/s"]
    assert res.lojas_falha == ["Loja B"]
