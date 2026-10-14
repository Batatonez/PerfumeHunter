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


def test_vtex_um_achado_por_sku_so_da_propria_loja():
    achados = vtex.achados_do_produto(PRODUTO_VTEX, "www.loja.com.br", "Loja")
    # o SKU de 100ml só tem estoque num parceiro do marketplace: fica de fora (confiança)
    assert [a.variacao for a in achados] == ["60ml", "200ml"]
    a60, a200 = achados
    assert a60.preco == 599.9 and a60.preco_de == 749.9 and a60.preco_pix == 569.9
    assert a60.marca == "Dior" and a60.oficial and a60.disponivel
    assert a60.url.endswith("/p?skuId=1001")
    assert "60ml" in a60.titulo
    assert not a200.disponivel  # sem estoque


def test_vtex_parceiro_nao_e_oficial_nem_usa_o_de_dele():
    # caso real da Época: parceiro marcado como sellerDefault, listado ANTES da loja, com "de" inflado
    produto = {"productName": "Afnan 9PM EDP 100ml", "brand": "Afnan", "link": "/9pm/p", "items": [{
        "itemId": "9", "name": "100ml", "sellers": [
            {"sellerId": "lojaxyz", "sellerDefault": True,
             "commertialOffer": {"Price": 229.0, "ListPrice": 557.0, "AvailableQuantity": 3}},
            {"sellerId": "1", "sellerDefault": False,
             "commertialOffer": {"Price": 319.0, "ListPrice": 319.0, "AvailableQuantity": 3}},
        ]}]}
    (a,) = vtex.achados_do_produto(produto, "www.epoca.com.br", "Época")
    assert a.preco == 319.0 and a.oficial and a.preco_de is None  # a loja, a preço normal
    # mesmo liberando parceiros, sem estoque na loja ele entra SEM selo e SEM o "de" inflado
    produto["items"][0]["sellers"][1]["commertialOffer"]["AvailableQuantity"] = 0
    (b,) = vtex.achados_do_produto(produto, "www.epoca.com.br", "Época", so_loja_propria=False)
    assert b.preco == 229.0 and not b.oficial and b.preco_de is None


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
    assert len(res.achados) == 2 and not res.erro
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


def test_ml_cai_pra_pagina_quando_api_pede_login(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "ARQ_TOKEN", tmp_path / "sem_login.json")
    monkeypatch.setattr(ml, "baixar_json", lambda *a, **k: (None, "bloqueado (HTTP 403)", True))
    urls = []
    monkeypatch.setattr(ml.coleta, "baixar_pagina", lambda url: (urls.append(url), (HTML_ML.encode(), None, False))[1])
    fonte = ml.FonteMercadoLivre()
    res = fonte.buscar("sauvage dior")
    # a página já vem filtrada pelo próprio ML (só loja oficial): todo card conta
    assert urls == ["https://lista.mercadolivre.com.br/sauvage-dior_Loja_all"]
    assert len(res.achados) == 2 and all(a.oficial for a in res.achados) and not res.erro
    # na próxima busca nem tenta a API (sem login), vai direto na página
    chamadas_api = []
    monkeypatch.setattr(ml, "baixar_json", lambda *a, **k: (chamadas_api.append(1), (None, "403", True))[1])
    fonte.buscar("sauvage dior")
    assert chamadas_api == []


def test_ml_patrocinado():
    html = HTML_ML.replace("https://x/MLB-2", "https://click1.mercadolivre.com.br/mclics/clicks/external/MLB/count?a=1")
    assert all("click1" not in a.url for a in ml.achados_do_html(html, so_oficial=False))


def test_ml_login_sem_refresh_token_avisa(monkeypatch, tmp_path):
    monkeypatch.setenv("ML_CLIENT_ID", "1")
    monkeypatch.setenv("ML_CLIENT_SECRET", "2")
    monkeypatch.setattr(ml, "_pedir_token", lambda campos: ({"access_token": "A", "expires_in": 21600}, 200))
    ok, msg = ml.trocar_codigo("TG-1", caminho=tmp_path / "t.json")
    assert ok and "refresh_token" in msg


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
    assert len(res.achados) == 2
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


def test_ml_tarefas_de_varredura():  # sem login: só os termos, pela página
    fonte = ml.FonteMercadoLivre(termos_varredura=["perfume masculino", "perfume arabe"], paginas=2)
    assert len(fonte.tarefas_varredura()) == 4


# ============================================================
# ML pela API de catálogo (a busca /sites/MLB/search fechou em 2025)
# ============================================================

def _produto_bruto(pid, nome, marca="Dior", volume="100 mL", dominio="MLB-PERFUMES", genero=None):
    attrs = [{"id": "BRAND", "value_name": marca}, {"id": "UNIT_VOLUME", "value_name": volume}]
    if genero:
        attrs.append({"id": "GENDER", "value_name": genero})
    return {"id": pid, "name": nome, "domain_id": dominio, "attributes": attrs,
            "pictures": [{"id": "x", "url": f"http://http2.mlstatic.com/{pid}-F.jpg"}]}


ITENS_P1 = {"paging": {"total": 4, "offset": 0}, "results": [
    {"item_id": "MLB10", "price": 450.0, "original_price": None, "currency_id": "BRL", "official_store_id": None,
     "condition": "new"},  # mais barato, mas não é loja oficial
    {"item_id": "MLB11", "price": 520.0, "original_price": 799.0, "currency_id": "BRL", "official_store_id": 77,
     "condition": "new"},
    {"item_id": "MLB12", "price": 600.0, "original_price": 500.0, "currency_id": "BRL", "official_store_id": 78,
     "condition": "new"},
    {"item_id": "MLB13", "price": 300.0, "currency_id": "BRL", "official_store_id": 79, "condition": "used"},
]}


def _api_falsa(rotas, chamadas=None):
    """baixar_json falso: a primeira rota (prefixo do caminho) que bater responde."""
    def falso(url, params=None, headers=None, timeout=None):
        caminho = url.replace(ml.API, "")
        if chamadas is not None:
            chamadas.append((caminho, dict(params or {}), headers))
        for prefixo, resposta in rotas.items():
            if caminho == prefixo:
                return resposta(params) if callable(resposta) else resposta
        return None, "HTTP 404", False
    return falso


def _com_login(monkeypatch, tmp_path):
    import json

    arq = tmp_path / "ml_token.json"
    arq.write_text(json.dumps({"access_token": "TOKEN", "refresh_token": "R", "expira_em": 9e12}))
    monkeypatch.setattr(ml, "ARQ_TOKEN", arq)


def test_ml_produto_do_catalogo():
    p = ml.produto_do_catalogo(_produto_bruto("MLB1", "Sauvage Eau de Parfum 100 ml"))
    assert (p.id, p.marca, p.ml) == ("MLB1", "Dior", 100.0)
    assert p.url == "https://www.mercadolivre.com.br/p/MLB1"
    assert p.imagem == "https://http2.mlstatic.com/MLB1-F.jpg"
    # nome sem "perfume" (mas do domínio de perfumes) e sem público: completa com o que o ML sabe
    p = ml.produto_do_catalogo(_produto_bruto("MLB2", "Club de Nuit Intense 105ml", "Armaf", genero="Feminino"))
    assert p.nome == "Perfume Club de Nuit Intense 105ml Feminino"
    assert ml.produto_do_catalogo({"id": "MLB3", "name": "  "}) is None
    assert ml.produto_do_catalogo("lixo") is None


def test_ml_ofertas_do_produto():
    ofertas = ml.ofertas_do_produto(ITENS_P1)
    assert [o.item_id for o in ofertas] == ["MLB11", "MLB12"]  # só loja oficial, novo, do mais barato
    assert ofertas[0].preco_de == 799.0 and ofertas[1].preco_de is None  # "de" menor que o preço não vale
    todas = ml.ofertas_do_produto(ITENS_P1, so_oficial=False)
    assert [o.item_id for o in todas] == ["MLB10", "MLB11", "MLB12"]
    sem_estoque = {"results": [dict(ITENS_P1["results"][1], available_quantity=0)]}
    assert ml.ofertas_do_produto(sem_estoque) == []
    dolar = {"results": [dict(ITENS_P1["results"][1], currency_id="USD")]}
    assert ml.ofertas_do_produto(dolar) == []


def test_ml_busca_pelo_catalogo(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    busca = {"results": [
        _produto_bruto("MLB1", "Perfume Sauvage Dior Eau de Parfum 100ml"),
        _produto_bruto("MLB2", "Kit Sauvage Dior Eau de Parfum 100ml + Pós Barba"),  # kit: nem abre
        _produto_bruto("MLB3", "Perfume Sauvage Dior Elixir 60ml", volume="60 mL"),
    ]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": (busca, None, False),
        "/products/MLB1/items": (ITENS_P1, None, False),
        # MLB3: ninguém vendendo ("No winners found") -> só não entra
    }, chamadas))
    res = ml.FonteMercadoLivre().buscar("sauvage dior")
    assert not res.erro and len(res.achados) == 2
    a, comum = res.achados
    assert (a.titulo, a.url, a.preco, a.preco_de, a.oficial, a.ml, a.marca) == (
        "Perfume Sauvage Dior Eau de Parfum 100ml", "https://www.mercadolivre.com.br/p/MLB1", 520.0, 799.0,
        True, 100.0, "Dior")
    # vendedor comum mais barato: vem junto (pro /locate), com o link do anúncio dele e sem selo de oficial
    assert (comum.url, comum.preco, comum.oficial) == ("https://produto.mercadolivre.com.br/MLB-10-_JM", 450.0, False)
    caminhos = [c[0] for c in chamadas]
    assert caminhos == ["/products/search", "/products/MLB1/items", "/products/MLB3/items"]
    assert chamadas[0][1] == {"status": "active", "site_id": "MLB", "q": "sauvage dior"}
    assert chamadas[0][2] == {"Authorization": "Bearer TOKEN"}
    assert "/sites/MLB/search" not in caminhos  # a busca antiga dá 403 pra todo mundo: nem tenta


def test_ml_busca_procura_loja_oficial_na_pagina_seguinte(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    pag1 = {"paging": {"total": 2}, "results": [ITENS_P1["results"][0]]}
    pag2 = {"paging": {"total": 2}, "results": [ITENS_P1["results"][1]]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": ({"results": [_produto_bruto("MLB1", "Perfume Sauvage Dior EDP 100ml")]}, None, False),
        "/products/MLB1/items": lambda params: ((pag2 if (params or {}).get("offset") else pag1), None, False),
    }))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    assert [(a.preco, a.oficial) for a in res.achados] == [(520.0, True), (450.0, False)]


def test_ml_api_recusada(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    paginas = []
    monkeypatch.setattr(ml.coleta, "baixar_pagina", lambda url: (paginas.append(url), (b"", "x", True))[1])
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({"/products/search": (None, "bloqueado (HTTP 403)", True)}))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    # logado e o ML recusou: pausa a loja e diz o que fazer, sem cair na página (que só dá captcha)
    assert res.bloqueado and "403" in res.erro and "ml-teste" in res.erro and paginas == []

    # login venceu: também pausa e avisa (senão o ML some calado)
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({"/products/search": (None, "HTTP 401", False)}))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    assert res.bloqueado and "ml-login" in res.erro and paginas == []


def test_ml_so_token_do_app_recusado_tenta_a_pagina(monkeypatch):
    monkeypatch.setenv("ML_CLIENT_ID", "1")
    monkeypatch.setenv("ML_CLIENT_SECRET", "2")

    class Resp:
        status_code = 200

        def json(self):
            return {"access_token": "APP", "expires_in": 21600}

    monkeypatch.setattr(ml.coleta._sessao, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({"/products/search": (None, "bloqueado (HTTP 403)", True)}))
    monkeypatch.setattr(ml.coleta, "baixar_pagina", lambda url: (HTML_ML.encode(), None, False))
    res = ml.FonteMercadoLivre().buscar("sauvage dior")
    assert not res.erro and len(res.achados) == 2


def test_ml_varredura_pelos_mais_vendidos(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/categories/MLB1246": ({"children_categories": [
            {"id": "MLB1000", "name": "Maquiagem"}, {"id": "MLB6284", "name": "Perfumes"}]}, None, False),
        "/categories/MLB6284": ({"children_categories": [
            {"id": "MLB6285", "name": "Masculinos"}, {"id": "MLB6286", "name": "Femininos"}]}, None, False),
        "/highlights/MLB/category/MLB6284": ({"content": [
            {"id": "MLB1", "position": 1, "type": "PRODUCT"},
            {"id": "MLB777", "position": 2, "type": "ITEM"},  # anúncio solto: a API de anúncios fecha (403)
        ]}, None, False),
        "/products/MLB1": (_produto_bruto("MLB1", "Perfume Sauvage Dior EDP 100ml"), None, False),
        "/products/MLB1/items": (ITENS_P1, None, False),
    }, chamadas))
    fonte = ml.FonteMercadoLivre(termos_varredura=["perfume masculino"])
    # 1ª varredura: ainda não sabe as categorias -> uma tarefa descobre e já faz a primeira
    tarefas = fonte.tarefas_varredura()
    assert [r for r, _ in tarefas] == ["ML mais vendidos (descobrindo categorias)", 'ML "perfume masculino" pág. 1']
    res = tarefas[0][1]()
    assert [(a.url, a.preco) for a in res.achados if a.oficial] == [("https://www.mercadolivre.com.br/p/MLB1", 520.0)]
    # da próxima vez, uma tarefa por categoria
    tarefas = fonte.tarefas_varredura()
    assert [r for r, _ in tarefas] == ["ML mais vendidos MLB6284", "ML mais vendidos MLB6285",
                                       "ML mais vendidos MLB6286", 'ML "perfume masculino" pág. 1']
    # categoria sem lista (404): pulada, sem contar como sucesso nem erro
    res = tarefas[1][1]()
    assert res.pulado and not res.erro
    # nome e foto do produto ficam guardados: na próxima varredura não pede /products/MLB1 de novo
    chamadas.clear()
    tarefas[0][1]()
    assert "/products/MLB1" not in [c[0] for c in chamadas]
    assert not any(c[0].startswith("/categories") for c in chamadas)


def test_ml_mais_vendidos_fechado_nao_pausa_a_loja(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/categories/MLB1246": ({"children_categories": [{"id": "MLB6284", "name": "Perfumes"}]}, None, False),
        "/highlights/MLB/category/MLB6284": (None, "bloqueado (HTTP 403)", True),
    }, chamadas))
    fonte = ml.FonteMercadoLivre()
    res = fonte._mais_vendidos(0)
    assert res.pulado and not res.erro and not res.bloqueado and res.achados == []
    chamadas.clear()
    assert fonte._mais_vendidos(0).pulado and chamadas == []  # nem tenta de novo hoje
    assert not any("mais vendidos" in r for r, _ in fonte.tarefas_varredura())  # nem entra na lista


def test_ml_sem_login_varredura_so_pela_pagina(monkeypatch):
    fonte = ml.FonteMercadoLivre(termos_varredura=["perfume masculino"], paginas=2)
    assert [r for r, _ in fonte.tarefas_varredura()] == ['ML "perfume masculino" pág. 1',
                                                         'ML "perfume masculino" pág. 2']
    assert fonte._pagina("perfume masculino", 1).achados == []  # sem login, só a 1ª página do site


def test_ml_diagnostico_nao_mostra_token(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)

    class Resp:
        def __init__(self, status, dados):
            self.status_code, self._dados = status, dados

        def json(self):
            return self._dados

    respostas = {
        "/users/me": Resp(200, {"id": 1, "nickname": "MAE"}),
        "/sites/MLB/search": Resp(403, {"message": "forbidden"}),
        "/products/search": Resp(200, {"results": [_produto_bruto("MLB1", "Perfume Sauvage Dior EDP 100ml")]}),
        "/products/MLB1/items": Resp(200, ITENS_P1),
        "/categories/MLB1246": Resp(200, {"children_categories": [{"id": "MLB6284", "name": "Perfumes"}]}),
        "/highlights/MLB/category/MLB6284": Resp(200, {"content": [{"id": "MLB1", "type": "PRODUCT"}]}),
    }
    texto = "\n".join(ml.diagnostico("sauvage", obter=lambda caminho, params=None: respostas[caminho]))
    assert "❌ 403 /sites/MLB/search" in texto and "forbidden" in texto
    assert "✅ 200 /products/search" in texto and "oficiais=3" in texto
    assert "MLB6284 Perfumes" in texto and "tipos: PRODUCT" in texto
    assert "TOKEN" not in texto and "MAE" not in texto
    monkeypatch.setattr(ml, "ARQ_TOKEN", tmp_path / "nada.json")
    assert "ml-login" in ml.diagnostico()[0]


def test_ml_categoria_sem_lista_nao_fecha_as_outras(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/categories/MLB1246": ({"children_categories": [{"id": "MLB6284", "name": "Perfumes"}]}, None, False),
        "/categories/MLB6284": ({"children_categories": [
            {"id": "MLB1", "name": "Kits de Perfumes"}, {"id": "MLB2", "name": "Perfumes Infantis"},
            {"id": "MLB6285", "name": "Masculinos"}]}, None, False),
        # MLB6284 não tem lista (404); MLB6285 tem
        "/highlights/MLB/category/MLB6285": ({"content": [{"id": "MLB9", "type": "PRODUCT"}]}, None, False),
        "/products/MLB9": (_produto_bruto("MLB9", "Perfume Sauvage Dior EDP 100ml"), None, False),
        "/products/MLB9/items": (ITENS_P1, None, False),
    }))
    fonte = ml.FonteMercadoLivre()
    assert fonte._descobrir_categorias("TOKEN") == ["MLB6284", "MLB6285"]  # sem kit e infantil
    assert fonte._mais_vendidos(0).pulado
    assert [a.preco for a in fonte._mais_vendidos(1).achados if a.oficial] == [520.0]


def test_ml_resposta_estranha_nao_quebra(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": ({"results": [_produto_bruto("MLB1", "Perfume Sauvage Dior EDP 100ml"), "x", None]},
                             None, False),
        "/products/MLB1/items": ({"paging": "?", "results": [None, {"price": "abc"}, ITENS_P1["results"][1]]},
                                 None, False),
    }))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    assert [a.preco for a in res.achados] == [520.0]
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({"/products/search": (["lista"], None, False)}))
    assert ml.FonteMercadoLivre().buscar("sauvage").achados == []
    assert ml.ofertas_do_produto(None) == [] and ml.ofertas_do_produto([1, 2]) == []


# ============================================================
# ML: erros no meio da busca, renovação do login, limite de velocidade, público
# ============================================================

def test_ml_para_no_primeiro_erro_grave(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    busca = {"results": [_produto_bruto(f"MLB{i}", f"Perfume Sauvage Dior EDP {i}00ml") for i in range(1, 6)]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": (busca, None, False),
        "/products/MLB1/items": (ITENS_P1, None, False),
        "/products/MLB2/items": (None, "bloqueado (HTTP 429)", True),
    }, chamadas))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    # 429 no 2º produto: para ali (não martela os outros) e pede pausa curta, sem aviso no canal
    assert [c[0] for c in chamadas] == ["/products/search", "/products/MLB1/items", "/products/MLB2/items"]
    assert res.bloqueado and res.pausa_horas == 0.5 and "429" in res.erro


def test_ml_erros_de_rede_seguidos_param_a_tarefa(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    busca = {"results": [_produto_bruto(f"MLB{i}", f"Perfume Sauvage Dior EDP {i}00ml") for i in range(1, 6)]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": (busca, None, False),
        "/products/MLB1/items": (ITENS_P1, None, False),
        "/products/MLB2/items": (None, "erro de conexão: ReadTimeout", False),
        "/products/MLB3/items": (None, "erro de conexão: ReadTimeout", False),
    }, chamadas))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    assert len(chamadas) == 4  # parou depois de 2 erros de rede seguidos
    assert [a.preco for a in res.achados if a.oficial] == [520.0] and not res.erro  # entrega o que deu


def test_ml_renovacao_do_login_falhou_ou_foi_recusada(monkeypatch, tmp_path):
    import json

    arq = tmp_path / "ml_token.json"
    arq.write_text(json.dumps({"access_token": "VELHO", "refresh_token": "R", "expira_em": 0}))
    monkeypatch.setattr(ml, "ARQ_TOKEN", arq)
    monkeypatch.setenv("ML_CLIENT_ID", "1")
    monkeypatch.setenv("ML_CLIENT_SECRET", "2")
    app = []
    monkeypatch.setattr(ml.coleta._sessao, "post", lambda *a, **k: app.append(1))  # token do app: não pode
    # rede caiu na renovação: erro comum (tenta de novo), sem pausar nem mandar refazer o login
    monkeypatch.setattr(ml, "_pedir_token", lambda campos: ({"message": "ConnectionError"}, 0))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    assert res.erro and not res.bloqueado and "renovar" in res.erro and app == []
    # o ML recusou a renovação: aí sim pausa e avisa pra refazer o login
    monkeypatch.setattr(ml, "_pedir_token", lambda campos: ({"error": "invalid_grant"}, 400))
    res = ml.FonteMercadoLivre().buscar("sauvage")
    assert res.bloqueado and "ml-login" in res.erro and app == []
    assert json.loads(arq.read_text())["refresh_token"] == "R"  # não estraga o arquivo


def test_ml_rele_so_um_produto(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({"/products/MLB1/items": (ITENS_P1, None, False)}, chamadas))
    fonte = ml.FonteMercadoLivre()
    velho = ml.achado_do_produto(ml.ProdutoML("MLB1", "Perfume Sauvage Dior EDP 100ml"), ml.OfertaML(400, None, True))
    assert fonte.sabe_reler(velho) and not fonte.sabe_reler(ml.Achado("x", "https://x/MLB-2", 1, "ML"))
    res = fonte.reler(velho)
    assert [c[0] for c in chamadas] == ["/products/MLB1/items"]
    assert [(a.url, a.preco) for a in res.achados] == [("https://www.mercadolivre.com.br/p/MLB1", 520.0)]


def test_ml_mais_vendidos_429_vira_pausa_curta(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/categories/MLB1246": ({"children_categories": [{"id": "MLB6284", "name": "Perfumes"}]}, None, False),
        "/highlights/MLB/category/MLB6284": (None, "bloqueado (HTTP 429)", True),
    }))
    res = ml.FonteMercadoLivre()._mais_vendidos(0)
    assert res.bloqueado and res.pausa_horas == 0.5


def test_ml_categorias_com_erro_tenta_de_novo_logo(monkeypatch, tmp_path):
    import time as _time

    _com_login(monkeypatch, tmp_path)
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/categories/MLB1246": ({"children_categories": [{"id": "MLB6284", "name": "Perfumes"}]}, None, False),
        "/categories/MLB6284": (None, "erro de conexão: ReadTimeout", False),
    }))
    fonte = ml.FonteMercadoLivre()
    assert fonte._descobrir_categorias("T") == ["MLB6284"]
    assert fonte._categorias_ate - _time.time() < 3700  # só 1 h (não 1 semana) porque deu erro no caminho


def test_ml_canal_masculino(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    p = ml.produto_do_catalogo(_produto_bruto("MLB1", "Good Girl Carolina Herrera EDP 80ml", genero="Feminino"))
    assert p.genero == "f" and ml.achado_do_produto(p, ml.OfertaML(1, None, True)).genero == "f"
    chamadas = []
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/categories/MLB1246": ({"children_categories": [{"id": "MLB6284", "name": "Perfumes"}]}, None, False),
        "/categories/MLB6284": ({"children_categories": [
            {"id": "MLB6285", "name": "Masculinos"}, {"id": "MLB6286", "name": "Femininos"}]}, None, False),
        "/products/search": ({"results": [
            _produto_bruto("MLB1", "Good Girl EDP 80ml", "Carolina Herrera", genero="Feminino"),
            _produto_bruto("MLB2", "Perfume Sauvage Dior EDP 100ml", genero="Masculino")]}, None, False),
        "/products/MLB1/items": (ITENS_P1, None, False),
        "/products/MLB2/items": (ITENS_P1, None, False),
    }, chamadas))
    fonte = ml.FonteMercadoLivre(termos_varredura=["perfume masculino", "perfume feminino"])
    fonte.publico = "masculino"
    assert fonte._descobrir_categorias("T") == ["MLB6284", "MLB6285"]  # pula a feminina
    assert [r for r, _ in fonte.tarefas_varredura() if "pág" in r] == ['ML "perfume masculino" pág. 1']
    # na varredura, nem abre os vendedores do feminino; no /locate (buscar) mostra tudo
    chamadas.clear()
    fonte._pagina("perfume", 0)
    assert "/products/MLB1/items" not in [c[0] for c in chamadas]
    assert {a.url for a in fonte.buscar("perfume").achados if a.oficial} == {
        "https://www.mercadolivre.com.br/p/MLB1", "https://www.mercadolivre.com.br/p/MLB2"}


# ============================================================
# VTEX: público pelo que a loja cadastrou
# ============================================================

def test_vtex_genero():
    assert vtex.genero_vtex({"categories": ["/Perfumes/Perfumes Femininos/", "/Perfumes/"]}) == "f"
    assert vtex.genero_vtex({"Gênero": ["Masculino"], "categories": ["/Perfumes/Femininos/"]}) == "m"
    assert vtex.genero_vtex({"properties": [{"name": "Gênero", "values": ["Unissex"]}]}) == "u"
    assert vtex.genero_vtex({"categories": ["/Perfumes/Masculino/", "/Perfumes/Feminino/"]}) == "u"
    assert vtex.genero_vtex({}) is None
    produto = dict(PRODUTO_VTEX, categories=["/Perfumes/Perfume Masculino/"])
    assert {a.genero for a in vtex.achados_do_produto(produto, "www.loja.com.br", "Loja")} == {"m"}


def test_ml_so_vendedor_comum():
    fonte = ml.FonteMercadoLivre()
    produto = ml.ProdutoML("MLB1", "Perfume Queening Mind Games EDP 100ml")
    comuns = [ml.OfertaML(300, None, False, "MLB55"), ml.OfertaML(320, None, False, "MLB56")]
    # ninguém oficial: só o comum mais barato (não vai pro canal, mas aparece no /locate)
    assert [(a.url, a.oficial) for a in fonte._escolher(produto, comuns)] == [
        ("https://produto.mercadolivre.com.br/MLB-55-_JM", False)]
    # oficial mais barato que todo mundo: só ele
    oficial = ml.OfertaML(280, None, True, "MLB57")
    assert [a.url for a in fonte._escolher(produto, [oficial] + comuns)] == ["https://www.mercadolivre.com.br/p/MLB1"]
    # sem o filtro de loja oficial na fonte: o mais barato, com o link do produto
    assert [a.preco for a in ml.FonteMercadoLivre(so_loja_oficial=False)._escolher(produto, comuns)] == [300]


def test_ml_sem_login_nao_tenta_reler(monkeypatch, tmp_path):
    a = ml.Achado("Perfume X", "https://www.mercadolivre.com.br/x/p/MLB1", 100, "Mercado Livre")
    assert not ml.FonteMercadoLivre().sabe_reler(a)  # sem login: refaz a busca da página
    _com_login(monkeypatch, tmp_path)
    assert ml.FonteMercadoLivre().sabe_reler(a)


def test_ml_canal_masculino_feminino_vai_pro_fim(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    busca = {"results": [_produto_bruto(f"MLB{i}", f"212 VIP Rosé EDP {i}0ml", "Carolina Herrera", genero="Feminino")
                         for i in range(1, 6)] + [_produto_bruto("MLB9", "212 VIP Men EDT 100ml", genero="Masculino")]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({"/products/search": (busca, None, False)}, chamadas))
    fonte = ml.FonteMercadoLivre(max_produtos=2)
    fonte.publico = "masculino"
    fonte.buscar("212 vip")
    assert [c[0] for c in chamadas][1] == "/products/MLB9/items"  # o masculino entra primeiro


def test_ml_vendedor_bem_avaliado(monkeypatch, tmp_path):
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    itens = {"results": [
        {"item_id": "MLB20", "price": 280.0, "seller_id": 1, "condition": "new"},  # mais barato, reputação ruim
        {"item_id": "MLB21", "price": 300.0, "original_price": 450.0, "seller_id": 2, "condition": "new"},  # top
        {"item_id": "MLB22", "price": 400.0, "seller_id": 3, "official_store_id": 9, "condition": "new"},
    ]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": ({"results": [_produto_bruto("MLB1", "Perfume Queening Mind Games EDP 100ml")]}, None, False),
        "/products/MLB1/items": (itens, None, False),
        "/users/1": ({"seller_reputation": {"level_id": "3_yellow", "power_seller_status": None}}, None, False),
        "/users/2": ({"seller_reputation": {"level_id": "5_green", "power_seller_status": "platinum"}}, None, False),
    }, chamadas))
    fonte = ml.FonteMercadoLivre()
    res = fonte.buscar("queening")
    vistos = {(a.preco, a.oficial, a.bem_avaliado, a.url) for a in res.achados}
    assert vistos == {
        (400.0, True, False, "https://www.mercadolivre.com.br/p/MLB1"),  # oficial
        (300.0, False, True, "https://produto.mercadolivre.com.br/MLB-21-_JM"),  # bem avaliado: pode ir pro canal
        (280.0, False, False, "https://produto.mercadolivre.com.br/MLB-20-_JM"),  # o mais barato: só no /locate
    }
    # reputação fica guardada: a próxima busca não pergunta de novo
    chamadas.clear()
    fonte.buscar("queening")
    assert not any(c[0].startswith("/users/") for c in chamadas)
    # dá pra desligar
    assert not any(a.bem_avaliado for a in ml.FonteMercadoLivre(vendedor_bem_avaliado=False).buscar("queening").achados)


def test_ml_reputacao_boa():
    assert ml.reputacao_boa({"seller_reputation": {"level_id": "5_green", "power_seller_status": "gold"}})
    assert not ml.reputacao_boa({"seller_reputation": {"level_id": "5_green", "power_seller_status": "silver"}})
    assert not ml.reputacao_boa({"seller_reputation": {"level_id": "4_light_green", "power_seller_status": "platinum"}})
    assert not ml.reputacao_boa({}) and not ml.reputacao_boa(None)


def test_ml_outro_processo_renovou_o_login(monkeypatch, tmp_path):
    import json
    import time as _t

    arq = tmp_path / "ml_token.json"
    arq.write_text(json.dumps({"access_token": "VELHO", "refresh_token": "R1", "expira_em": 0}))
    monkeypatch.setattr(ml, "ARQ_TOKEN", arq)
    monkeypatch.setenv("ML_CLIENT_ID", "1")
    monkeypatch.setenv("ML_CLIENT_SECRET", "2")

    def renovou_antes(campos):  # o ml-teste renovou primeiro: o R1 já não vale
        arq.write_text(json.dumps({"access_token": "NOVO", "refresh_token": "R2", "expira_em": _t.time() + 3600}))
        return {"error": "invalid_grant"}, 400

    monkeypatch.setattr(ml, "_pedir_token", renovou_antes)
    assert ml.token_e_motivo() == ("NOVO", None)


def test_ml_produto_fechado_nao_bloqueia_a_loja(monkeypatch, tmp_path):
    """Bug do "queening": um produto com vendedores fechados (403) pausava o ML inteiro por 6 h."""
    _com_login(monkeypatch, tmp_path)
    busca = {"results": [_produto_bruto("MLB1", "Perfume Queening Mind Games EDP 100ml"),
                         _produto_bruto("MLB2", "Perfume Queening Mind Games EDP 50ml", volume="50 mL")]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": (busca, None, False),
        "/products/MLB1/items": (None, "bloqueado (HTTP 403)", True),
        "/products/MLB2/items": (ITENS_P1, None, False),
    }))
    res = ml.FonteMercadoLivre().buscar("queening")
    assert not res.bloqueado and not res.erro
    assert [a.preco for a in res.achados if a.oficial] == [520.0]
    # todos fechados: nada achado, mas sem pausar a loja
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": (busca, None, False),
        "/products/MLB1/items": (None, "bloqueado (HTTP 403)", True),
        "/products/MLB2/items": (None, "bloqueado (HTTP 403)", True),
    }))
    res = ml.FonteMercadoLivre().buscar("queening")
    assert not res.bloqueado and not res.erro and res.achados == []


def test_ml_detalhe_do_catalogo(monkeypatch, tmp_path):
    from collections import Counter

    assert ml.detalhe_catalogo(0, Counter()) == "nenhum produto com esse nome no catálogo do ML"
    assert ml.detalhe_catalogo(4, Counter(sem_vendedor=1, escondido=2, lixo=1)) == (
        "catálogo do ML: 4 produto(s) (1 sem ninguém vendendo agora, 2 com os vendedores escondidos pelo ML, "
        "1 kit/decant/amostra)")
    _com_login(monkeypatch, tmp_path)
    busca = {"results": [_produto_bruto("MLB1", "Perfume Queening Mind Games Extrait 100ml"),
                         _produto_bruto("MLB2", "Decant Queening Mind Games 10ml"),
                         _produto_bruto("MLB3", "Perfume Queening Mind Games Extrait 50ml", volume="50 mL")]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": (busca, None, False),
        "/products/MLB3/items": (None, "bloqueado (HTTP 403)", True),
        # MLB1: 404 (ninguém vendendo)
    }))
    res = ml.FonteMercadoLivre().buscar("queening")
    assert res.achados == [] and not res.erro
    assert res.detalhe == ("catálogo do ML: 3 produto(s) (1 sem ninguém vendendo agora, "
                           "1 com os vendedores escondidos pelo ML, 1 kit/decant/amostra)")


def test_ml_so_perfume_no_catalogo(monkeypatch, tmp_path):
    """"/locate god of fire" achava um Funko do God of War: no catálogo do ML só entra perfume."""
    _com_login(monkeypatch, tmp_path)
    chamadas = []
    busca = {"results": [
        {"id": "MLB7", "name": "God Of War - Fire Troll # 271 - Pop Funko", "domain_id": "MLB-COLLECTIBLE_FIGURES"},
        _produto_bruto("MLB1", "Stephane Humbert Lucas God Of Fire 50ml", "SHL 777", volume="50 mL"),
        _produto_bruto("MLB2", "Club de Nuit Intense Man 105ml", "Armaf", dominio="MLB-FRAGRANCES"),
    ]}
    monkeypatch.setattr(ml, "baixar_json", _api_falsa({
        "/products/search": (busca, None, False),
        "/products/MLB1/items": (ITENS_P1, None, False),
        "/products/MLB2/items": (ITENS_P1, None, False),
    }, chamadas))
    res = ml.FonteMercadoLivre().buscar("god of fire")
    assert "/products/MLB7/items" not in [c[0] for c in chamadas]  # nem abre os vendedores do Funko
    assert {a.url for a in res.achados if a.oficial} == {"https://www.mercadolivre.com.br/p/MLB1",
                                                         "https://www.mercadolivre.com.br/p/MLB2"}
    assert "1 que não é perfume" in res.detalhe
