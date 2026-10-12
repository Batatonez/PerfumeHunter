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
    assert not res.erro and len(res.achados) == 1
    a = res.achados[0]
    assert (a.titulo, a.url, a.preco, a.preco_de, a.oficial, a.ml, a.marca) == (
        "Perfume Sauvage Dior Eau de Parfum 100ml", "https://www.mercadolivre.com.br/p/MLB1", 520.0, 799.0,
        True, 100.0, "Dior")
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
    assert [a.preco for a in res.achados] == [520.0]


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
    tarefas = fonte.tarefas_varredura()
    rotulos = [r for r, _ in tarefas]
    assert rotulos[:6] == [f"ML mais vendidos #{i}" for i in range(1, 7)] and rotulos[6:] == [
        'ML "perfume masculino" pág. 1']
    res = tarefas[0][1]()
    assert [(a.url, a.preco) for a in res.achados] == [("https://www.mercadolivre.com.br/p/MLB1", 520.0)]
    assert fonte._descobrir_categorias("TOKEN") == ["MLB6284", "MLB6285", "MLB6286"]
    # a 4ª categoria não existe: tarefa vazia, sem erro
    assert tarefas[3][1]().achados == [] and not tarefas[3][1]().erro
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
    assert not res.erro and not res.bloqueado and res.achados == []
    chamadas.clear()
    assert fonte._mais_vendidos(0).achados == [] and chamadas == []  # nem tenta de novo hoje


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
    assert fonte._mais_vendidos(0).achados == []
    assert [a.preco for a in fonte._mais_vendidos(1).achados] == [520.0]


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
