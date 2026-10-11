"""Leitura das respostas das lojas (formatos da VTEX e do Mercado Livre), sem rede."""

from scentinel.fontes import ResultadoBusca, fonte_busca_item, montar_fontes
from scentinel.fontes import mercadolivre as ml
from scentinel.fontes import vtex

PRODUTO_VTEX = {
    "productId": "123",
    "productName": "Sauvage Dior Eau de Parfum - Perfume Masculino",
    "brand": "Dior",
    "linkText": "sauvage-dior-eau-de-parfum-perfume-masculino",
    "link": "https://www.loja.com.br/sauvage-dior-eau-de-parfum-perfume-masculino/p",
    "items": [
        {
            "itemId": "1001",
            "name": "60ml",
            "nameComplete": "Sauvage Dior Eau de Parfum - Perfume Masculino 60ml",
            "sellers": [{
                "sellerId": "1", "sellerName": "Loja", "sellerDefault": True,
                "commertialOffer": {
                    "Price": 599.9, "ListPrice": 749.9, "PriceWithoutDiscount": 749.9, "AvailableQuantity": 5,
                    "Installments": [
                        {"PaymentSystemName": "Visa", "Value": 59.99, "NumberOfInstallments": 10},
                        {"PaymentSystemName": "Pix", "Value": 569.9, "NumberOfInstallments": 1},
                    ],
                },
            }],
        },
        {
            "itemId": "1002",
            "name": "100ml",
            "nameComplete": "Sauvage Dior Eau de Parfum - Perfume Masculino 100ml",
            "sellers": [
                {"sellerId": "mkt9", "sellerName": "Outro", "sellerDefault": False,
                 "commertialOffer": {"Price": 650.0, "ListPrice": 650.0, "AvailableQuantity": 2}},
                {"sellerId": "1", "sellerName": "Loja", "sellerDefault": True,
                 "commertialOffer": {"Price": 0, "ListPrice": 0, "AvailableQuantity": 0}},
            ],
        },
        {
            "itemId": "1003",
            "name": "200ml",
            "nameComplete": "Sauvage Dior Eau de Parfum - Perfume Masculino 200ml",
            "sellers": [{"sellerId": "1", "sellerDefault": True,
                         "commertialOffer": {"Price": 1200.0, "ListPrice": 1200.0, "AvailableQuantity": 0}}],
        },
    ],
}


def test_vtex_um_achado_por_sku():
    achados = vtex.achados_do_produto(PRODUTO_VTEX, "www.loja.com.br", "Loja")
    assert len(achados) == 3
    a60, a100, a200 = achados

    assert a60.preco == 599.9 and a60.preco_de == 749.9 and a60.preco_pix == 569.9
    assert a60.marca == "Dior" and a60.oficial and a60.disponivel
    assert a60.url.endswith("/p?skuId=1001")
    assert "60ml" in a60.titulo

    # SKU de 100ml: a loja não tem, mas um vendedor parceiro tem -> entra, mas não é "oficial"
    assert a100.preco == 650.0 and not a100.oficial and a100.preco_de is None

    # 200ml sem estoque
    assert not a200.disponivel


def test_vtex_link_relativo_e_marca_padrao():
    produto = dict(PRODUTO_VTEX, link="/malbec/p", brand=None, productName="Malbec Desodorante Colônia")
    a = vtex.achados_do_produto(produto, "www.boticario.com.br", "O Boticário", marca_padrao="O Boticário")[0]
    assert a.url.startswith("https://www.boticario.com.br/malbec/p")
    assert a.marca == "O Boticário"


def test_vtex_busca_tenta_a_segunda_api(monkeypatch):
    chamadas = []

    def falso(url, params=None, headers=None, timeout=None):
        chamadas.append(url)
        if "catalog_system" in url:
            return None, "HTTP 404", False
        return {"products": [PRODUTO_VTEX]}, None, False

    monkeypatch.setattr(vtex, "baixar_json", falso)
    fonte = vtex.FonteVTEX("www.loja.com.br", nome="Loja")
    res = fonte.buscar("sauvage dior")
    assert len(res.achados) == 3 and not res.erro
    # da próxima vez já vai direto na que funcionou
    chamadas.clear()
    fonte.buscar("sauvage dior")
    assert len(chamadas) == 1 and "intelligent-search" in chamadas[0]


def test_vtex_bloqueio(monkeypatch):
    monkeypatch.setattr(vtex, "baixar_json", lambda *a, **k: (None, "bloqueado (HTTP 429)", True))
    res = vtex.FonteVTEX("www.loja.com.br").buscar("x")
    assert res.bloqueado


# ============================================================
# Mercado Livre
# ============================================================

API_ML = {"results": [
    {"title": "Perfume Dior Sauvage Eau De Parfum 100ml", "price": 689.9, "original_price": 899.0,
     "permalink": "https://produto.mercadolivre.com.br/MLB-1-sauvage#pos=1", "available_quantity": 3,
     "official_store_id": 1234, "condition": "new",
     "attributes": [{"id": "BRAND", "value_name": "Dior"}]},
    {"title": "Sauvage Dior EDP 100ml Lacrado", "price": 350.0, "permalink": "https://x/MLB-2",
     "official_store_id": None, "condition": "new"},
    {"title": "Sauvage Usado", "price": 100.0, "permalink": "https://x/MLB-3", "official_store_id": 9, "condition": "used"},
]}


def test_ml_api_so_loja_oficial():
    achados = ml.achados_da_api(API_ML)
    assert len(achados) == 1
    a = achados[0]
    assert a.preco == 689.9 and a.preco_de == 899.0 and a.marca == "Dior" and a.oficial
    assert a.url == "https://produto.mercadolivre.com.br/MLB-1-sauvage"
    # sem o filtro, entra o não oficial também (mas nunca o usado)
    assert len(ml.achados_da_api(API_ML, so_oficial=False)) == 2


HTML_ML = """
<ol>
 <li class="ui-search-layout__item"><div class="poly-card">
  <a class="poly-component__title" href="https://www.mercadolivre.com.br/sauvage/p/MLB1?pdp_filters=x">Perfume Sauvage Dior Eau de Parfum 100ml</a>
  <span class="poly-component__seller">Vendido por Dior Loja oficial</span>
  <s class="andes-money-amount andes-money-amount--previous"><span class="andes-money-amount__fraction">899</span></s>
  <div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__fraction">689</span><span class="andes-money-amount__cents">90</span></span></div>
  <span>R$ 655,40 no Pix</span>
 </div></li>
 <li class="ui-search-layout__item"><div class="poly-card">
  <a class="poly-component__title" href="https://x/MLB-2">Sauvage Dior 100ml Lacrado Original</a>
  <div class="poly-price__current"><span class="andes-money-amount"><span class="andes-money-amount__fraction">350</span></span></div>
 </div></li>
</ol>
"""


def test_ml_html():
    achados = ml.achados_do_html(HTML_ML)
    assert len(achados) == 1
    a = achados[0]
    assert a.preco == 689.9 and a.preco_de == 899.0 and a.preco_pix == 655.4 and a.oficial
    assert a.url == "https://www.mercadolivre.com.br/sauvage/p/MLB1"
    assert len(ml.achados_do_html(HTML_ML, so_oficial=False)) == 2


def test_ml_cai_pra_pagina_quando_api_pede_login(monkeypatch):
    monkeypatch.setattr(ml, "baixar_json", lambda *a, **k: (None, "bloqueado (HTTP 403)", True))
    monkeypatch.setattr(ml.coleta, "baixar_pagina", lambda url: (HTML_ML.encode(), None, False))
    fonte = ml.FonteMercadoLivre()
    res = fonte.buscar("sauvage dior")
    assert len(res.achados) == 1 and not res.erro


# ============================================================
# config das lojas
# ============================================================

def test_montar_fontes_com_filtros():
    from scentinel.catalogo import ItemCatalogo

    fontes = montar_fontes([
        {"tipo": "vtex", "dominio": "https://www.sephora.com.br/", "nome": "Sephora", "categorias": ["designer"]},
        {"tipo": "vtex", "dominio": "www.boticario.com.br", "marcas": ["O Boticário"], "marca_padrao": "O Boticário"},
        {"tipo": "mercadolivre", "ativo": False},
    ])
    assert [f.id for f in fontes] == ["vtex:www.sephora.com.br", "vtex:www.boticario.com.br"]
    sauvage = ItemCatalogo("s", "Sauvage", "Dior", "EDT", categoria="designer")
    malbec = ItemCatalogo("m", "Malbec", "O Boticário", categoria="nacional")
    assert fonte_busca_item(fontes[0], sauvage) and not fonte_busca_item(fontes[0], malbec)
    assert fonte_busca_item(fontes[1], malbec) and not fonte_busca_item(fontes[1], sauvage)
    assert isinstance(ResultadoBusca(), ResultadoBusca)



# ============================================================
# varredura e login do ML
# ============================================================

def test_vtex_varredura_pede_maior_desconto(monkeypatch):
    pedidos = []

    def falso(url, params=None, headers=None, timeout=None):
        pedidos.append((url, params))
        return [PRODUTO_VTEX], None, False

    monkeypatch.setattr(vtex, "baixar_json", falso)
    fonte = vtex.FonteVTEX("www.loja.com.br", paginas=2)
    tarefas = fonte.tarefas_varredura()
    assert len(tarefas) == 2
    res = tarefas[1][1]()
    assert len(res.achados) == 3
    url, params = pedidos[-1]
    assert params["O"] == "OrderByBestDiscountDESC" and params["_from"] == 50 and params["ft"] == "perfume"
    assert vtex.FonteVTEX("www.x.com.br", varrer=False).tarefas_varredura() == []


def test_ml_login_e_renovacao(monkeypatch, tmp_path):
    import time as _t

    arq = tmp_path / "ml_token.json"
    monkeypatch.setenv("ML_CLIENT_ID", "123")
    monkeypatch.setenv("ML_CLIENT_SECRET", "seg")
    monkeypatch.setenv("ML_REDIRECT_URI", "https://www.google.com/")
    pedidos = []

    def token_falso(campos):
        pedidos.append(campos)
        n = len(pedidos)
        return {"access_token": f"A{n}", "refresh_token": f"R{n}", "expires_in": 21600}, 200

    monkeypatch.setattr(ml, "_pedir_token", token_falso)
    assert "client_id=123" in ml.link_login() and "redirect_uri=https" in ml.link_login()
    ok, _ = ml.trocar_codigo("https://www.google.com/?code=TG-xyz&state=1", caminho=arq)
    assert ok and pedidos[0]["code"] == "TG-xyz" and pedidos[0]["grant_type"] == "authorization_code"
    assert ml.token_valido(arq) == "A1" and len(pedidos) == 1  # ainda válido: não renova

    import json
    dados = json.loads(arq.read_text())
    dados["expira_em"] = _t.time() - 1  # venceu
    arq.write_text(json.dumps(dados))
    assert ml.token_valido(arq) == "A2"
    assert pedidos[1] == {"grant_type": "refresh_token", "client_id": "123", "client_secret": "seg", "refresh_token": "R1"}
    assert json.loads(arq.read_text())["refresh_token"] == "R2"  # guarda o refresh novo


def test_ml_tarefas_de_varredura():
    fonte = ml.FonteMercadoLivre(termos_varredura=["perfume masculino", "perfume arabe"], paginas=2)
    assert len(fonte.tarefas_varredura()) == 4
