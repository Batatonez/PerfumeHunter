"""Lojas VTEX: busca pública de catálogo, sem login.

Duas APIs, tentadas nesta ordem (modo "auto"):
  1. catalog_system: GET https://{dominio}/api/catalog_system/pub/products/search?ft=termo&_from=0&_to=19
     -> lista de produtos [{productName, brand, link, items:[{itemId, name, nameComplete,
        sellers:[{sellerId, sellerName, sellerDefault, commertialOffer:{Price, ListPrice,
        AvailableQuantity, Installments:[{PaymentSystemName, Value, NumberOfInstallments}]}}]}]}]
  2. intelligent search (VTEX IO): GET https://{dominio}/api/io/_v/api/intelligent-search/product_search/?query=termo&count=20
     -> {"products": [ ...mesmo formato de produto (link pode ser relativo)... ]}
Cada SKU (tamanho) vira um Achado separado.
"""

from urllib.parse import urljoin

from scentinel.coleta import texto_para_preco
from scentinel.fontes import Achado, ResultadoBusca, baixar_json, registrar
from scentinel.lojas import nome_da_loja


def _oferta_do_sku(item):
    """Escolhe o vendedor: o padrão da loja (sellerDefault / sellerId '1') com estoque, senão o mais barato com estoque."""
    sellers = item.get("sellers") or []
    com_estoque = []
    for s in sellers:
        oferta = s.get("commertialOffer") or {}
        preco = texto_para_preco(oferta.get("Price"))
        if not preco:
            continue
        qtd = oferta.get("AvailableQuantity")
        disponivel = (qtd or 0) > 0 if qtd is not None else bool(oferta.get("IsAvailable", True))
        com_estoque.append((s, oferta, preco, disponivel))
    if not com_estoque:
        return None
    disponiveis = [c for c in com_estoque if c[3]] or com_estoque
    padrao = [c for c in disponiveis if c[0].get("sellerDefault") or str(c[0].get("sellerId")) == "1"]
    return (padrao or sorted(disponiveis, key=lambda c: c[2]))[0]


def _pix(oferta, preco):
    """Preço à vista no Pix, se a loja informar nas parcelas (PaymentSystemName 'Pix')."""
    for parcela in oferta.get("Installments") or []:
        nome = str(parcela.get("PaymentSystemName") or parcela.get("Name") or "").lower()
        if "pix" in nome and int(parcela.get("NumberOfInstallments") or 1) == 1:
            valor = texto_para_preco(parcela.get("Value") or parcela.get("TotalValuePlusInterestRate"))
            if valor and preco * 0.7 <= valor < preco:
                return valor
    return None


def achados_do_produto(produto, dominio, loja, marca_padrao=None):
    titulo_base = produto.get("productName") or ""
    marca = produto.get("brand") or marca_padrao
    link = produto.get("link") or (f"/{produto['linkText']}/p" if produto.get("linkText") else "")
    link = urljoin(f"https://{dominio}/", link)
    saida = []
    for item in produto.get("items") or []:
        escolhido = _oferta_do_sku(item)
        if not escolhido:
            continue
        seller, oferta, preco, disponivel = escolhido
        preco_de = texto_para_preco(oferta.get("ListPrice"))
        if not preco_de or preco_de <= preco:
            preco_de = texto_para_preco(oferta.get("PriceWithoutDiscount"))
            if not preco_de or preco_de <= preco:
                preco_de = None
        titulo = item.get("nameComplete") or f"{titulo_base} {item.get('name') or ''}".strip()
        url = f"{link}?skuId={item['itemId']}" if item.get("itemId") else link
        oficial = bool(seller.get("sellerDefault") or str(seller.get("sellerId")) == "1")
        imagens = item.get("images") or []
        imagem = imagens[0].get("imageUrl") if imagens and isinstance(imagens[0], dict) else None
        saida.append(Achado(
            titulo=titulo, url=url, preco=preco, loja=loja, marca=marca, preco_de=preco_de,
            preco_pix=_pix(oferta, preco), disponivel=disponivel, oficial=oficial, variacao=item.get("name"),
            imagem=imagem,
        ))
    return saida


@registrar("vtex")
class FonteVTEX:
    def __init__(self, dominio, nome=None, modo="auto", marca_padrao=None, quantidade=20,
                 varrer=True, termo_varredura="perfume", paginas=3):
        self.dominio = dominio.replace("https://", "").replace("http://", "").strip("/")
        self.nome = nome or nome_da_loja(f"https://{self.dominio}")
        self.id = f"vtex:{self.dominio}"
        self.modo = modo  # "auto" | "catalogo" | "inteligente"
        self.marca_padrao = marca_padrao
        self.quantidade = max(1, min(int(quantidade), 50))
        self._modo_que_funciona = None
        self.varrer = varrer
        self.termo_varredura = termo_varredura
        self.paginas = max(1, int(paginas))

    def _catalogo(self, termo):
        url = f"https://{self.dominio}/api/catalog_system/pub/products/search"
        dados, erro, bloq = baixar_json(url, params={"ft": termo, "_from": 0, "_to": self.quantidade - 1})
        if erro:
            return None, erro, bloq
        if not isinstance(dados, list):
            return None, "resposta inesperada (catalog_system)", False
        return dados, None, False

    def _inteligente(self, termo):
        url = f"https://{self.dominio}/api/io/_v/api/intelligent-search/product_search/"
        dados, erro, bloq = baixar_json(url, params={"query": termo, "count": self.quantidade, "page": 1})
        if erro:
            return None, erro, bloq
        if not isinstance(dados, dict) or not isinstance(dados.get("products"), list):
            return None, "resposta inesperada (intelligent-search)", False
        return dados["products"], None, False

    def _pagina_descontos(self, pagina):
        """Uma página de perfumes, os de MAIOR DESCONTO primeiro."""
        if self._modo_que_funciona == "inteligente":
            url = f"https://{self.dominio}/api/io/_v/api/intelligent-search/product_search/"
            params = {"query": self.termo_varredura, "count": 50, "page": pagina + 1, "sort": "discount:desc"}
            dados, erro, bloq = baixar_json(url, params=params)
            produtos = dados.get("products") if isinstance(dados, dict) else None
        else:
            url = f"https://{self.dominio}/api/catalog_system/pub/products/search"
            params = {"ft": self.termo_varredura, "O": "OrderByBestDiscountDESC",
                      "_from": pagina * 50, "_to": pagina * 50 + 49}
            dados, erro, bloq = baixar_json(url, params=params)
            produtos = dados if isinstance(dados, list) else None
        if bloq:
            return ResultadoBusca(erro=erro, bloqueado=True)
        if erro or produtos is None:
            return ResultadoBusca(erro=erro or "resposta inesperada")
        achados = []
        for produto in produtos:
            achados += achados_do_produto(produto, self.dominio, self.nome, self.marca_padrao)
        return ResultadoBusca(achados=achados)

    def tarefas_varredura(self):
        """Lista de (rótulo, função) da varredura: as primeiras páginas por maior desconto."""
        if not self.varrer:
            return []
        return [(f"{self.nome} pág. {p + 1}", (lambda p=p: self._pagina_descontos(p))) for p in range(self.paginas)]

    def buscar(self, termo):
        termo = termo.replace("'", " ").replace('"', " ")  # a busca da VTEX recusa aspas
        modos = [self._modo_que_funciona] if self._modo_que_funciona else (
            ["catalogo", "inteligente"] if self.modo == "auto" else [self.modo]
        )
        erros = []
        for modo in modos:
            produtos, erro, bloqueado = (self._catalogo if modo == "catalogo" else self._inteligente)(termo)
            if bloqueado:
                return ResultadoBusca(erro=erro, bloqueado=True)
            if erro:
                erros.append(f"{modo}: {erro}")
                continue
            self._modo_que_funciona = modo
            achados = []
            for produto in produtos:
                achados += achados_do_produto(produto, self.dominio, self.nome, self.marca_padrao)
            return ResultadoBusca(achados=achados)
        return ResultadoBusca(erro="; ".join(erros) or "sem resposta")
