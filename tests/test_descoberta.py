"""Busca automática de ponta a ponta: loja falsa -> casamento -> alerta (ou não)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from scentinel import config, db
from scentinel.caca import Cacador
from scentinel.catalogo import ItemCatalogo
from scentinel.coleta import Leitura, Resultado
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
    kw.setdefault("oficial", True)  # loja confiável (o padrão do bot é só postar loja oficial)
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

    def sem_rede(url, seletor=None):  # teste nunca baixa página de verdade
        return Resultado(erro="sem rede no teste")

    cac = Cacador(con, cfg, enviar_alerta, enviar_aviso, baixar=sem_rede, dormir=dormir, relogio=relogio)
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
    # só 1 outra loja: não dá pra confiar no "mercado" (um vendedor caro inventaria desconto);
    # fica com o preço normal do catálogo (850): 580 = 32% abaixo
    assert r.melhor.avaliacao.fonte == "catalogo" and round(r.melhor.avaliacao.pct) == 32
    assert r.melhor.loja == "Loja A" and r.em_promo
    assert [o.loja for o in r.outros] == ["Loja B"]



# ============================================================
# VARREDURA: qualquer perfume em promo, não só o catálogo
# ============================================================

def pagina(*achados):
    return ResultadoBusca(achados=list(achados))


def test_varredura_posta_perfume_fora_do_catalogo(amb):
    amb.a.varredura = [pagina(
        achado("Perfume Lumina Queening Le Parfum Masculino EDP 100ml", 199.9, "Loja A", "https://a/q",
               preco_de=399.9, imagem="https://img/q.jpg", oficial=True),
        achado("Hidratante Corporal Perfumado 400ml", 30, "Loja A", "https://a/h", preco_de=60),
        achado("Kit Perfume Good Girl + Loção", 300, "Loja A", "https://a/kit", preco_de=600),
        achado("Perfume Miniatura X EDP 5ml", 50, "Loja A", "https://a/mini", preco_de=100),
        achado("Perfume Y EDT 100ml sem desconto", 300, "Loja A", "https://a/y"),
    )]
    rodar(amb.desc.varredura())
    assert len(amb.alertas) == 1
    oferta, mencao = amb.alertas[0]
    assert oferta.url == "https://a/q" and round(oferta.avaliacao.pct) == 50
    assert oferta.avaliacao.fonte == "loja" and oferta.preco_de == 399.9 and oferta.imagem == "https://img/q.jpg"
    # só o "de R$" da própria loja confirma os 50%: nível limitado a 3 (sem marcar ninguém)
    assert oferta.avaliacao.nivel.nivel == 3 and mencao is None
    assert oferta.perfume == "Lumina Queening Le Parfum EDP"
    # o resto nem virou anúncio (não é perfume, é kit, é miniatura)
    for url in ("https://a/h", "https://a/kit", "https://a/mini"):
        assert db.anuncio_por_url(amb.con, url) is None
    # o perfume a preço cheio fica guardado (estoque da vitrine e histórico), mas não vira post de promo
    assert db.anuncio_por_url(amb.con, "https://a/y") is not None


def test_varredura_posta_maiores_descontos_primeiro(amb):
    amb.cfg.descoberta.max_alertas_por_hora = 2
    amb.a.varredura = [pagina(
        achado("Perfume Lumina Um EDP 100ml", 380, "Loja A", "https://a/1", preco_de=500),    # 24%
        achado("Perfume Lumina Dois EDP 100ml", 200, "Loja A", "https://a/2", preco_de=500),  # 60%
        achado("Perfume Lumina Tres EDP 100ml", 300, "Loja A", "https://a/3", preco_de=500),  # 40%
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
    amb.a.varredura = [pagina(achado("Perfume Lumina Um EDP 100ml", 200, "Loja A", "https://a/1", preco_de=500))]
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
    amb.a.programar("queening", achado("Lumina Queening EDP Masculino 100ml", 210, "Loja A", "https://a/q", preco_de=300),
                    achado("Body Splash Queening 200ml", 50, "Loja A", "https://a/bs"))
    amb.b.programar("queening", achado("Perfume Lumina Queening Eau de Parfum 100ml", 189.9, "Loja B", "https://b/q"),
                    achado("Outro Perfume Qualquer 100ml", 99, "Loja B", "https://b/x"))

    async def buscar_e_esperar_gravacao():
        r = await amb.desc.buscar_ao_vivo("queening")
        await asyncio.gather(*amb.desc._em_segundo_plano)  # a gravação roda depois da resposta
        return r

    res = rodar(buscar_e_esperar_gravacao())
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


# ============================================================
# Cenários da revisão (não podem voltar)
# ============================================================

def test_vendedor_caro_sozinho_nao_infla_a_referencia(amb):
    # Khamrah: catálogo 280. A a 170 sozinho e um vendedor a 554 em B. Antes isso virava 💎 com @everyone.
    amb.b.varredura = [pagina(achado("Lattafa Khamrah EDP 100ml", 554, "Loja B", "https://b/k"))]
    amb.a.varredura = [pagina(achado("Lattafa Khamrah EDP 100ml", 170, "Loja A", "https://a/k"))]
    rodar(amb.desc.varredura())
    oferta, mencao = [x for x in amb.alertas if x[0].url == "https://a/k"][0]
    assert oferta.avaliacao.referencia == 280 and oferta.avaliacao.fonte == "catalogo"
    assert mencao != "everyone"  # sem histórico, @everyone não sai


def test_lote_posta_so_o_mais_barato_mesmo_com_de_inflado(amb):
    amb.a.varredura = [pagina(
        achado("Perfume Lumina Aurora EDP 100ml", 300, "Loja A", "https://a/caro", preco_de=900),   # 67% "de mentira"
        achado("Perfume Lumina Aurora EDP 100ml", 250, "Loja A", "https://a/barato", preco_de=400),  # 37%
    )]
    amb.cfg.sanidade_queda_pct = 99
    rodar(amb.desc.varredura())
    assert [o.url for o, _ in amb.alertas] == ["https://a/barato"]


def test_nao_reposta_o_mesmo_preco_quando_o_nivel_reseta(amb):
    amb.cfg.sanidade_queda_pct = 99
    amb.a.varredura = [pagina(achado("Perfume Lumina Um EDP 100ml", 200, "Loja A", "https://a/1", preco_de=500))]
    rodar(amb.desc.varredura())
    assert len(amb.alertas) == 1
    # a loja tira o "de" (nível cai e o estado zera)...
    amb.relogio.agora += timedelta(hours=2)
    amb.a.varredura = [pagina(achado("Perfume Lumina Um EDP 100ml", 200, "Loja A", "https://a/1"))]
    rodar(amb.desc.varredura())
    # ...e põe de volta: mesmo preço, não reposta
    amb.relogio.agora += timedelta(hours=2)
    amb.a.varredura = [pagina(achado("Perfume Lumina Um EDP 100ml", 200, "Loja A", "https://a/1", preco_de=500))]
    rodar(amb.desc.varredura())
    assert len(amb.alertas) == 1


def test_everyone_so_com_historico_e_no_maximo_2_por_dia(amb):
    amb.cfg.sanidade_queda_pct = 99
    from scentinel import promo

    # 3 perfumes DIFERENTES, cada um com 5 leituras de histórico a preço cheio (300) e agora a 120 (60% OFF)
    urls = []
    for i, loja in enumerate(("A1", "A2", "A3")):
        url = f"https://{loja}/p{i}"
        urls.append(url)
        anuncio = db.salvar_anuncio_descoberto(amb.con, db.criar_perfume(amb.con, f"P{i}", f"p{i}")["id"],
                                               loja, url, 100, None, f"t:perfume {i}|m|100", "vtex:a", f"Perfume {i} EDP 100ml", True)
        for h in range(5):
            db.registrar_preco(amb.con, anuncio["id"], Leitura(300, "x"), amb.relogio.agora - timedelta(hours=10 + h))
    mencoes = []
    for url in urls:
        anuncio = db.anuncio_por_url(amb.con, url)
        rodar(amb.cac.processar_leitura(anuncio, Leitura(120, "x"), amb.relogio.agora))
        mencoes.append(amb.alertas[-1][1])
    assert mencoes == [promo.MENCAO_EVERYONE, promo.MENCAO_EVERYONE, promo.MENCAO_DONO]


def test_loja_nao_oficial_nao_entra(amb):
    amb.a.varredura = [pagina(achado("Perfume Lumina Um EDP 100ml", 200, "Loja A", "https://a/1",
                                     preco_de=500, oficial=False))]
    rodar(amb.desc.varredura())
    assert amb.alertas == [] and db.anuncio_por_url(amb.con, "https://a/1") is None


def test_busca_automatica_nao_mexe_em_anuncio_do_add(amb):
    amb.a.programar("sauvage dior edp", 600.0)
    anuncio_manual, _ = rodar(amb.cac.adicionar("https://a/s", "Sauvage EDP", ml=100, alvo=500))
    amb.a.varredura = [pagina(achado("Sauvage Dior Eau de Parfum 60ml", 400, "Loja A", "https://a/s"))]
    rodar(amb.desc.varredura())
    depois = db.anuncio_por_url(amb.con, "https://a/s")
    assert depois["origem"] == "manual" and depois["ml"] == 100 and depois["alvo"] == 500


def test_contratipo_e_refil_nao_entram(amb):
    amb.a.varredura = [pagina(
        achado("Perfume Referência Olfativa Sauvage Dior 100ml", 90, "Loja A", "https://a/ref", preco_de=300),
        achado("Refil Malbec Desodorante Colônia 100ml", 90, "Loja A", "https://a/refil", preco_de=200),
        achado("Sauvage Dior Eau de Parfum 100ml", 150, "Loja A", "https://a/barato-demais"),  # < 35% do normal
    )]
    rodar(amb.desc.varredura())
    assert amb.alertas == []


# ============================================================
# Canal só de perfume masculino (publico = "masculino")
# ============================================================

GOOD_GIRL = ItemCatalogo("good-girl", "Good Girl", "Carolina Herrera", "EDP", [80], "fem", "designer", 700, 80, 520,
                         busca=["good girl carolina herrera"])


def test_canal_masculino_nao_posta_feminino(amb):
    assert amb.cfg.descoberta.publico == "masculino"  # padrão do config.toml
    amb.a.varredura = [pagina(
        achado("Perfume Lumina Aurora Feminino EDP 100ml", 150, "Loja A", "https://a/fem", preco_de=400),
        # o título não diz, mas a loja diz que é feminino (categoria/especificação)
        achado("Perfume Lumina Dama EDP 100ml", 150, "Loja A", "https://a/fem2", preco_de=400, genero="f"),
        achado("Perfume Lumina Bravo Masculino EDP 100ml", 150, "Loja A", "https://a/masc", preco_de=400),
        achado("Perfume Lumina Duo Unissex EDP 100ml", 150, "Loja A", "https://a/uni", preco_de=400),
        achado("Perfume Lumina Sem Genero EDP 100ml", 150, "Loja A", "https://a/sei", preco_de=400),
    )]
    rodar(amb.desc.varredura())
    postados = sorted(o.url for o, _ in amb.alertas)
    assert postados == ["https://a/masc", "https://a/sei", "https://a/uni"]
    assert db.anuncio_por_url(amb.con, "https://a/fem") is None and db.anuncio_por_url(amb.con, "https://a/fem2") is None


def test_canal_masculino_nem_procura_item_feminino_do_catalogo(amb):
    amb.desc.itens.append(GOOD_GIRL)
    amb.desc.cacador.catalogo[GOOD_GIRL.id] = GOOD_GIRL
    amb.a.programar("good girl carolina herrera", achado("Good Girl Carolina Herrera EDP 80ml", 300, "Loja A", "https://a/gg"))
    rodar(amb.desc.rodada())
    assert "good girl carolina herrera" not in amb.a.chamadas
    assert all(item.id != "good-girl" for item, _ in amb.desc.pendentes(amb.relogio.agora))
    # e se aparecer na varredura (catálogo diz feminino, mesmo sem "feminino" no título), não entra
    amb.a.varredura = [pagina(achado("Good Girl Carolina Herrera EDP 80ml", 300, "Loja A", "https://a/gg"))]
    rodar(amb.desc.varredura(forcar=True))
    assert amb.alertas == [] and db.anuncio_por_url(amb.con, "https://a/gg") is None


def test_publico_todos_posta_feminino(amb):
    amb.cfg.descoberta.publico = "todos"
    amb.a.varredura = [pagina(achado("Perfume Lumina Aurora Feminino EDP 100ml", 150, "Loja A", "https://a/fem",
                                     preco_de=400))]
    rodar(amb.desc.varredura())
    assert [o.url for o, _ in amb.alertas] == ["https://a/fem"]


def test_locate_mostra_feminino_mas_nao_guarda(amb):
    amb.a.programar("aurora", achado("Perfume Lumina Aurora Feminino EDP 100ml", 150, "Loja A", "https://a/fem"))

    async def buscar_e_esperar_gravacao():
        r = await amb.desc.buscar_ao_vivo("aurora")
        await asyncio.gather(*amb.desc._em_segundo_plano)
        return r

    res = rodar(buscar_e_esperar_gravacao())
    assert [a.url for a, _, _ in res.ofertas] == ["https://a/fem"]  # pesquisa mostra tudo
    assert db.anuncio_por_url(amb.con, "https://a/fem") is None  # mas o canal masculino não rastreia


def test_top_do_dia_sem_feminino(amb):
    amb.cfg.descoberta.publico = "todos"
    amb.a.varredura = [pagina(
        achado("Perfume Lumina Aurora Feminino EDP 100ml", 150, "Loja A", "https://a/fem", preco_de=400),
        achado("Perfume Lumina Bravo Masculino EDP 100ml", 160, "Loja A", "https://a/masc", preco_de=400),
    )]
    rodar(amb.desc.varredura())
    assert len(amb.desc.top()) == 2
    amb.cfg.descoberta.publico = "masculino"  # mudou o config: o top já não mostra o que ficou guardado
    assert [o.url for o in amb.desc.top()] == ["https://a/masc"]


def test_fonte_recebe_o_publico_do_canal(amb):
    class FonteComPublico(FonteFalsa):
        publico = "todos"

    f = FonteComPublico("ml", "ML")
    Descoberta(amb.cac, ITENS, [f])
    assert f.publico == "masculino"


# ============================================================
# /locate não fica preso esperando loja lenta
# ============================================================

def test_locate_nao_espera_loja_lenta(amb, monkeypatch):
    import time as _time

    from scentinel import descoberta as mod

    monkeypatch.setattr(mod, "TEMPO_MAX_AO_VIVO_S", 0.3)
    amb.a.programar("sauvage dior", achado("Sauvage Dior EDP 100ml", 650, "Loja A", "https://a/s"))
    lenta = amb.b.buscar
    amb.b.buscar = lambda termo: (_time.sleep(1.5), lenta(termo))[1]
    async def medir():
        inicio = _time.monotonic()
        r = await amb.desc.buscar_ao_vivo("sauvage", "dior")
        return r, _time.monotonic() - inicio

    res, levou = rodar(medir())
    assert levou < 1.2
    assert [a.url for a, _, _ in res.ofertas] == ["https://a/s"] and res.lojas_falha == ["Loja B"]


def test_pulado_nao_zera_o_castigo_da_loja(amb):
    # loja bloqueou -> 1ª pausa; depois uma tarefa "pulada" (não falou com a loja) não pode zerar a contagem
    amb.a.varredura = [ResultadoBusca(erro="bloqueado (HTTP 429)", bloqueado=True)]
    rodar(amb.desc.varredura())
    assert amb.desc._fonte_falhas["vtex:a"] == 1
    amb.relogio.agora += timedelta(hours=7)
    amb.a.varredura = [ResultadoBusca(pulado=True)]
    rodar(amb.desc.varredura(forcar=True))
    assert amb.desc._fonte_falhas["vtex:a"] == 1
    amb.a.varredura = [ResultadoBusca(erro="bloqueado (HTTP 429)", bloqueado=True)]
    rodar(amb.desc.varredura(forcar=True))
    assert amb.desc._fonte_falhas["vtex:a"] == 2 and len(amb.avisos) == 1  # pausa dobra, avisa uma vez só


def test_checagem_de_sanidade_rele_so_o_produto(amb):
    class FonteQueRele(FonteFalsa):
        relidos = []

        def sabe_reler(self, a):
            return a.url.startswith("https://a/")

        def reler(self, a):
            self.relidos.append(a.url)
            return ResultadoBusca(achados=[a])

    f = FonteQueRele("vtex:a", "Loja A")
    amb.desc.fontes = [f]
    # 💎: 60% abaixo do catálogo -> rechecagem de sanidade antes de postar
    f.varredura = [pagina(achado("Sauvage Dior Eau de Parfum 100ml", 340, "Loja A", "https://a/s"))]
    rodar(amb.desc.varredura())
    assert f.relidos == ["https://a/s"]


def test_feminino_em_tamanho_fora_do_catalogo_tambem_fica_de_fora(amb):
    amb.desc.itens.append(GOOD_GIRL)
    # 30 ml não está no catálogo (só 80): o item é solto, mas o público dele ainda vale
    amb.a.varredura = [pagina(achado("Good Girl Carolina Herrera EDP 30ml", 150, "Loja A", "https://a/gg30",
                                     preco_de=400))]
    rodar(amb.desc.varredura())
    assert amb.alertas == [] and db.anuncio_por_url(amb.con, "https://a/gg30") is None


def test_rede_caindo_pausa_pouco(amb):
    from scentinel.descoberta import ERROS_PRA_PAUSAR

    amb.a.programar("sauvage dior edp", erro="erro de conexão: ConnectionError")
    for _ in range(ERROS_PRA_PAUSAR):
        rodar(amb.desc.buscar_item(ITENS[0], amb.a))
    pausa = amb.desc._fonte_pausada_ate["vtex:a"] - amb.relogio.agora
    assert timedelta(minutes=59) < pausa <= timedelta(hours=1)  # rede: volta em 1 h
    amb.b.programar("sauvage dior edp", erro="HTTP 404")
    for _ in range(ERROS_PRA_PAUSAR):
        rodar(amb.desc.buscar_item(ITENS[0], amb.b))
    assert amb.desc._fonte_pausada_ate["vtex:b"] - amb.relogio.agora == timedelta(hours=24)  # busca não existe


def test_login_vencido_depois_de_limite_ainda_avisa(amb):
    amb.a.varredura = [ResultadoBusca(erro="api: devagar (HTTP 429)", bloqueado=True, pausa_horas=0.5)]
    rodar(amb.desc.varredura())
    assert amb.avisos == []  # limite de velocidade: só no log
    amb.relogio.agora += timedelta(hours=2)
    amb.a.varredura = [ResultadoBusca(erro="api: o login do ML venceu (HTTP 401)", bloqueado=True)]
    rodar(amb.desc.varredura(forcar=True))
    assert len(amb.avisos) == 1 and "401" in amb.avisos[0]


def test_vendedor_bem_avaliado_vai_pro_canal_comum_nao(amb):
    amb.a.varredura = [pagina(
        achado("Perfume Lumina Bravo Masculino EDP 100ml", 150, "Loja A", "https://a/top", preco_de=400,
               oficial=False, bem_avaliado=True),
        achado("Perfume Lumina Forte Masculino EDP 100ml", 150, "Loja A", "https://a/qualquer", preco_de=400,
               oficial=False),
    )]
    rodar(amb.desc.varredura())
    assert [o.url for o, _ in amb.alertas] == ["https://a/top"]


def test_limite_de_velocidade_pausa_no_maximo_2h(amb):
    for _ in range(8):  # 0,5 -> 1 -> 2 -> 2 -> ... (antes ia até 48 h)
        amb.a.varredura = [ResultadoBusca(erro="api: devagar (HTTP 429)", bloqueado=True, pausa_horas=0.5)]
        rodar(amb.desc.varredura(forcar=True))
        amb.relogio.agora += timedelta(hours=3)
    estado = {e["nome"]: e for e in amb.desc.estado_fontes(amb.relogio.agora - timedelta(hours=3))}
    assert estado["Loja A"]["pausada_ate"] - (amb.relogio.agora - timedelta(hours=3)) == timedelta(hours=2)
    assert "429" in estado["Loja A"]["motivo"]
    # funcionou de novo: motivo some e marca a última busca boa
    amb.a.varredura = [pagina()]
    rodar(amb.desc.varredura(forcar=True))
    estado = {e["nome"]: e for e in amb.desc.estado_fontes()}
    assert estado["Loja A"]["pausada_ate"] is None and estado["Loja A"]["motivo"] is None
    assert estado["Loja A"]["ultimo_ok"] == amb.relogio.agora


def test_despausar(amb):
    amb.a.varredura = [ResultadoBusca(erro="bloqueado (HTTP 403)", bloqueado=True)]
    rodar(amb.desc.varredura())
    assert amb.desc.pausada(amb.a)
    amb.desc.despausar()
    assert not amb.desc.pausada(amb.a)


def test_locate_explica_por_que_nao_achou(amb):
    from scentinel.formato import embed_ao_vivo

    amb.a.respostas["queening"] = [ResultadoBusca(detalhe="nenhum produto com esse nome no catálogo do ML")]
    amb.b.programar("queening", achado("Perfume Outra Coisa Masculino 100ml", 99, "Loja B", "https://b/x"))
    res = rodar(amb.desc.buscar_ao_vivo("queening"))
    assert res.ofertas == []
    assert res.detalhes == [("Loja A", "nenhum produto com esse nome no catálogo do ML"),
                            ("Loja B", "1 anúncio(s) com outro nome/tamanho")]
    texto = embed_ao_vivo(res)["description"]
    assert "• Loja A: nenhum produto com esse nome no catálogo do ML" in texto
    assert "fora do catálogo" in texto  # explica o limite da API do ML


# ============================================================
# VITRINE: um post a cada 15 min
# ============================================================

def _vitrine_com(amb, *achados):
    amb.a.varredura = [pagina(*achados)]
    rodar(amb.desc.varredura(forcar=True))
    amb.alertas.clear()


def test_vitrine_posta_a_melhor_e_nao_repete(amb):
    amb.cfg.descoberta.max_alertas_por_hora = 0  # a varredura não posta nada: só a vitrine
    _vitrine_com(
        amb,
        achado("Perfume Lumina Bravo Masculino EDP 100ml", 300, "Loja A", "https://a/bravo"),  # preço cheio
        achado("Perfume Lumina Forte Masculino EDP 100ml", 250, "Loja A", "https://a/forte", preco_de=300),  # −17%
        achado("Sauvage Dior Eau de Parfum 100ml", 840, "Loja A", "https://a/sauvage"),  # catálogo, preço normal
    )
    amb.cfg.descoberta.max_alertas_por_hora = 10
    postados = []

    async def enviar(oferta):
        postados.append(oferta.url)
        return True

    for _ in range(4):
        amb.relogio.agora += timedelta(minutes=15)
        rodar(amb.desc.postar_vitrine(enviar))
    # maior desconto primeiro, depois o do catálogo, depois o resto; sem repetir; e para quando acaba
    assert postados == ["https://a/forte", "https://a/sauvage", "https://a/bravo"]
    # 12 h depois, sem nada novo: repete o que saiu há mais tempo ("tentar não repetir, mas sla")
    amb.relogio.agora += timedelta(hours=12)
    rodar(amb.desc.varredura(forcar=True))  # leituras novas (a vitrine só usa o que foi lido nas últimas 24 h)
    rodar(amb.desc.postar_vitrine(enviar))
    assert postados[-1] == "https://a/forte"


def test_vitrine_espera_se_acabou_de_sair_promo(amb):
    _vitrine_com(amb, achado("Perfume Lumina Bravo Masculino EDP 100ml", 300, "Loja A", "https://a/bravo"))
    anuncio = db.anuncio_por_url(amb.con, "https://a/bravo")
    db.registrar_alerta(amb.con, anuncio["id"], "nivel", 2, 300, amb.relogio.agora - timedelta(minutes=5))

    async def enviar(oferta):
        raise AssertionError("não era pra postar")

    assert rodar(amb.desc.postar_vitrine(enviar)) is None


def test_vitrine_sem_feminino_e_desligavel(amb):
    _vitrine_com(amb, achado("Perfume Lumina Aurora Feminino EDP 100ml", 150, "Loja A", "https://a/fem"))
    assert amb.desc.escolher_vitrine() is None  # canal masculino: feminino não entra nem na vitrine
    amb.cfg.descoberta.vitrine_minutos = 0

    async def enviar(oferta):
        raise AssertionError("desligada")

    assert rodar(amb.desc.postar_vitrine(enviar)) is None


def test_vitrine_registra_post_sem_nivel(amb):
    amb.cfg.descoberta.max_alertas_por_hora = 0
    _vitrine_com(amb, achado("Perfume Lumina Bravo Masculino EDP 100ml", 300, "Loja A", "https://a/bravo"))
    amb.cfg.descoberta.max_alertas_por_hora = 10

    async def enviar(oferta):
        return True

    oferta = rodar(amb.desc.postar_vitrine(enviar))
    alerta = db.ultimo_alerta(amb.con, oferta.anuncio_id, amb.relogio.agora - timedelta(minutes=1))
    assert alerta["tipo"] == "vitrine" and alerta["nivel"] is None  # não conta no limite de @everyone
    resumo = amb.desc.resumo_vitrine()
    assert resumo["minutos"] == 15 and resumo["conhecidas"] == 1 and resumo["ultimo_post"] == amb.relogio.agora


def test_send_manda_varias_novas_primeiro_sem_repetir_perfume_no_lote(amb):
    amb.cfg.descoberta.max_alertas_por_hora = 0
    _vitrine_com(
        amb,
        achado("Perfume Lumina Bravo Masculino EDP 100ml", 300, "Loja A", "https://a/bravo"),
        achado("Perfume Lumina Forte Masculino EDP 100ml", 250, "Loja A", "https://a/forte", preco_de=300),
        achado("Sauvage Dior Eau de Parfum 100ml", 840, "Loja A", "https://a/sauvage"),
        achado("Sauvage Dior Eau de Parfum 100ml", 845, "Loja A", "https://a/sauvage2"),  # mesmo perfume
    )
    amb.cfg.descoberta.max_alertas_por_hora = 10
    postados, pausas = [], []

    async def enviar(oferta):
        postados.append(oferta.url)
        return True

    async def pausa(s):
        pausas.append(s)

    assert rodar(amb.desc.mandar_varias(2, enviar, pausa)) == 2
    assert postados == ["https://a/forte", "https://a/sauvage"] and pausas == [1.5]
    # pediu mais do que tem de novo: manda o que falta e depois repete as que já saíram (mais antiga primeiro)
    postados.clear()
    amb.relogio.agora += timedelta(minutes=1)
    assert rodar(amb.desc.mandar_varias(10, enviar, pausa)) == 3
    assert postados == ["https://a/bravo", "https://a/forte", "https://a/sauvage"]


def test_send_para_se_o_discord_falhar(amb):
    _vitrine_com(amb, achado("Perfume Lumina Bravo Masculino EDP 100ml", 300, "Loja A", "https://a/bravo"),
                 achado("Perfume Lumina Forte Masculino EDP 100ml", 250, "Loja A", "https://a/forte"))

    async def falha(oferta):
        return False

    async def pausa(s):
        pass

    assert rodar(amb.desc.mandar_varias(5, falha, pausa)) == 0
    novas, repetidas = amb.desc.ranking_vitrine()
    assert len(novas) == 2 and repetidas == []  # o que não saiu não conta como postado


def test_locate_so_mostra_perfume(amb):
    amb.a.programar("god of fire",
                    achado("God Of War - Fire Troll # 271 - Pop Funko", 450, "Loja A", "https://a/funko"),
                    achado("Perfume Stephane Humbert Lucas God Of Fire Parfum 50ml", 1800, "Loja A", "https://a/gof"))
    res = rodar(amb.desc.buscar_ao_vivo("god of fire"))
    assert [a.url for a, _, _ in res.ofertas] == ["https://a/gof"]
