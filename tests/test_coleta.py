import json

import pytest

from scentinel import coleta
from scentinel.coleta import achar_preco, extrair_preco_de_texto, texto_para_preco


def pagina(corpo="", head=""):
    return f"<html><head>{head}</head><body>{corpo}</body></html>"


def jsonld(dados):
    return f'<script type="application/ld+json">{json.dumps(dados)}</script>'


# ============================================================
# texto_para_preco
# ============================================================

@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("R$ 1.299,90", 1299.90),
        ("R$\xa0699,90", 699.90),
        ("699,90", 699.90),
        ("1299.90", 1299.90),
        ("1.299", 1299.0),
        ("1,299.90", 1299.90),
        (1299.9, 1299.9),
        (500, 500.0),
        ("10x de R$ 29,90", None),
        ("", None),
        (None, None),
        (0, None),
        ("abc", None),
        (True, None),
    ],
)
def test_texto_para_preco(entrada, esperado):
    assert texto_para_preco(entrada) == esperado


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("Por: R$ 699,90", 699.90),
        ("R$ 1.299,90 à vista", 1299.90),
        ("10x de R$ 29,90 sem juros", None),
        ("ou 5 x R$ 100,00", None),
        ("sem número", None),
    ],
)
def test_extrair_preco_de_texto(entrada, esperado):
    assert extrair_preco_de_texto(entrada) == esperado


# ============================================================
# achar_preco: JSON-LD
# ============================================================

def test_jsonld_simples():
    html = pagina(head=jsonld({"@type": "Product", "name": "X", "offers": {"@type": "Offer", "price": "459.90"}}))
    leitura = achar_preco(html)
    assert leitura.preco == 459.90
    assert leitura.metodo == "json-ld"
    assert not leitura.esgotado


def test_jsonld_aggregate_lowprice():
    html = pagina(head=jsonld({
        "@type": "Product",
        "offers": {"@type": "AggregateOffer", "lowPrice": 389.0, "highPrice": 520.0},
    }))
    assert achar_preco(html).preco == 389.0


def test_jsonld_graph_e_tipo_lista():
    html = pagina(head=jsonld({"@graph": [
        {"@type": "WebSite"},
        {"@type": ["Product", "Thing"], "offers": [{"price": "299,90"}]},
    ]}))
    assert achar_preco(html).preco == 299.90


def test_jsonld_esgotado():
    html = pagina(head=jsonld({
        "@type": "Product",
        "offers": {"price": 199.9, "availability": "https://schema.org/OutOfStock"},
    }))
    leitura = achar_preco(html)
    assert leitura.preco == 199.9
    assert leitura.esgotado


def test_jsonld_prefere_oferta_com_estoque():
    html = pagina(head=jsonld({
        "@type": "Product",
        "offers": [
            {"price": 150, "availability": "OutOfStock"},
            {"price": 180, "availability": "InStock"},
        ],
    }))
    leitura = achar_preco(html)
    assert leitura.preco == 180
    assert not leitura.esgotado


def test_jsonld_price_specification_com_preco_de():
    html = pagina(head=jsonld({
        "@type": "Product",
        "offers": {"priceSpecification": [
            {"price": 999.0, "priceType": "https://schema.org/ListPrice"},
            {"price": 699.0},
        ]},
    }))
    leitura = achar_preco(html)
    assert leitura.preco == 699.0
    assert leitura.preco_de == 999.0


def test_jsonld_quebrado_cai_pra_meta():
    html = pagina(head='<script type="application/ld+json">{quebrado</script>'
                  '<meta property="product:price:amount" content="349.90">')
    leitura = achar_preco(html)
    assert leitura.preco == 349.90
    assert leitura.metodo == "meta"


def test_itemprop():
    html = pagina('<span itemprop="price" content="1299.00">R$ 1.299,00</span>')
    assert achar_preco(html).preco == 1299.0


def test_seletor_tem_prioridade():
    html = pagina(
        '<div class="preco-final">Por: R$ 555,00</div>',
        head=jsonld({"@type": "Product", "offers": {"price": 999}}),
    )
    leitura = achar_preco(html, seletor=".preco-final")
    assert leitura.preco == 555.0
    assert leitura.metodo == "seletor"


def test_seletor_ignora_parcela_e_cai_no_jsonld():
    html = pagina(
        '<div class="parcela">10x de R$ 55,50</div>',
        head=jsonld({"@type": "Product", "offers": {"price": 555}}),
    )
    leitura = achar_preco(html, seletor=".parcela")
    assert leitura.preco == 555
    assert leitura.metodo == "json-ld"


def test_sem_preco():
    assert achar_preco(pagina("<p>nada aqui</p>")).preco is None


# ============================================================
# achar_preco: preço de e Pix
# ============================================================

def test_preco_de_mercado_livre():
    html = pagina(
        '<s class="andes-money-amount andes-money-amount--previous">'
        '<span class="andes-money-amount__fraction">1.099</span>'
        '<span class="andes-money-amount__cents">90</span></s>',
        head=jsonld({"@type": "Product", "offers": {"price": 799.9}}),
    )
    leitura = achar_preco(html)
    assert leitura.preco == 799.9
    assert leitura.preco_de == 1099.90


def test_preco_de_classe_generica():
    html = pagina(
        '<span class="product-old-price">De: R$ 500,00</span>',
        head=jsonld({"@type": "Product", "offers": {"price": 400}}),
    )
    assert achar_preco(html).preco_de == 500.0


def test_preco_de_menor_que_preco_e_ignorado():
    html = pagina(
        "<del>R$ 100,00</del>",
        head=jsonld({"@type": "Product", "offers": {"price": 400}}),
    )
    assert achar_preco(html).preco_de is None


def test_preco_de_vtex_state():
    html = pagina(
        '<script>window.__STATE__ = {"Price":299.9,"ListPrice":459.9}</script>',
        head='<meta property="product:price:amount" content="299.90">',
    )
    assert achar_preco(html).preco_de == 459.9


@pytest.mark.parametrize(
    "texto",
    ["R$ 626,90 no Pix", "R$ 626,90 à vista no PIX", "Pagando com Pix: R$ 626,90"],
)
def test_pix(texto):
    html = pagina(f"<p>{texto}</p>", head=jsonld({"@type": "Product", "offers": {"price": 659.9}}))
    assert achar_preco(html).preco_pix == 626.90


def test_pix_absurdo_e_ignorado():
    html = pagina("<p>R$ 10,00 no Pix</p>", head=jsonld({"@type": "Product", "offers": {"price": 659.9}}))
    assert achar_preco(html).preco_pix is None


def test_aceita_bytes():
    html = pagina(head=jsonld({"@type": "Product", "offers": {"price": 120}})).encode()
    assert achar_preco(html).preco == 120


# ============================================================
# checar_url (rede mockada)
# ============================================================

def test_checar_url_ok(monkeypatch):
    html = pagina(head=jsonld({"@type": "Product", "offers": {"price": 120}})).encode()
    monkeypatch.setattr(coleta, "baixar_pagina", lambda url: (html, None, False))
    res = coleta.checar_url("https://loja.com/x")
    assert res.leitura.preco == 120
    assert not res.erro


def test_checar_url_bloqueado(monkeypatch):
    monkeypatch.setattr(coleta, "baixar_pagina", lambda url: (None, "bloqueado (HTTP 403)", True))
    res = coleta.checar_url("https://loja.com/x")
    assert res.bloqueado


def test_checar_url_captcha(monkeypatch):
    html = pagina("<h1>Please solve this CAPTCHA</h1>").encode()
    monkeypatch.setattr(coleta, "baixar_pagina", lambda url: (html, None, False))
    res = coleta.checar_url("https://loja.com/x")
    assert res.bloqueado


def test_checar_url_sem_preco(monkeypatch):
    monkeypatch.setattr(coleta, "baixar_pagina", lambda url: (pagina("oi").encode(), None, False))
    res = coleta.checar_url("https://loja.com/x")
    assert res.erro == "preço não encontrado na página"
    assert not res.bloqueado
