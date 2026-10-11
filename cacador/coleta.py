"""Baixar a página de um anúncio e descobrir o preço.

A lógica de leitura veio do cacador_v0.py e foi estendida para também
pegar o "preço de" (riscado) e o preço no Pix.

Ordem de tentativa para o preço:
  1. seletor CSS manual (configurado por anúncio, é o plano B)
  2. JSON-LD schema.org/Product
  3. meta tags (product:price:amount, og:price:amount, itemprop=price)
"""

import gc
import json
import re
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from cacador import lojas

try:  # lxml é mais rápido e gasta menos RAM; html.parser é o plano B
    import lxml  # noqa: F401

    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

# Finge ser um navegador normal. Sem isso, várias lojas recusam a conexão.
CABECALHOS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

TIMEOUT_S = 25
TAMANHO_MAX = 8 * 1024 * 1024  # páginas maiores que isso são cortadas

_sessao = requests.Session()
_sessao.headers.update(CABECALHOS)


@dataclass
class Leitura:
    preco: float | None
    metodo: str | None
    esgotado: bool = False
    preco_de: float | None = None  # o riscado, se tiver
    preco_pix: float | None = None


@dataclass
class Resultado:
    leitura: Leitura | None = None
    erro: str | None = None
    bloqueado: bool = False


# ============================================================
# TRANSFORMAR TEXTO EM PREÇO
# ============================================================

def texto_para_preco(valor):
    """Transforma coisas como 'R$ 1.299,90', '1299.90' ou 1299.9 em float.
    Texto que não é só um preço (ex.: '10x de R$ 29,90') devolve None."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor) if valor > 0 else None

    texto = str(valor).replace("R$", "").replace("\xa0", "").replace(" ", "").strip()
    if not texto:
        return None

    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            # Formato brasileiro: 1.299,90
            texto = texto.replace(".", "").replace(",", ".")
        else:
            # Formato americano: 1,299.90
            texto = texto.replace(",", "")
    elif "," in texto:
        # 699,90
        texto = texto.replace(",", ".")
    elif texto.count(".") == 1 and len(texto.split(".")[1]) == 3:
        # 1.299 -> provavelmente milhar, não decimal
        texto = texto.replace(".", "")

    try:
        preco = float(texto)
    except ValueError:
        return None
    return preco if preco > 0 else None


_RE_PARCELA = re.compile(r"\d+\s*x\s*(de\s*)?(R\$)?\s*\d", re.IGNORECASE)
_RE_NUMERO = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def extrair_preco_de_texto(texto):
    """Versão tolerante: acha o preço dentro de um texto tipo 'Por: R$ 699,90'.
    Texto de parcela ('10x de R$ 29,90') nunca vira preço."""
    if texto is None:
        return None
    texto = str(texto).replace("\xa0", " ")
    direto = texto_para_preco(texto)
    if direto:
        return direto
    if _RE_PARCELA.search(texto):
        return None
    achado = _RE_NUMERO.search(texto)
    return texto_para_preco(achado.group(0)) if achado else None


# ============================================================
# JSON-LD
# ============================================================

def _produtos_no_jsonld(dados):
    """Procura objetos do tipo Product dentro do JSON-LD (que pode vir
    como lista, como objeto, ou dentro de '@graph')."""
    encontrados = []
    if isinstance(dados, list):
        for item in dados:
            encontrados += _produtos_no_jsonld(item)
    elif isinstance(dados, dict):
        tipo = dados.get("@type")
        tipos = tipo if isinstance(tipo, list) else [tipo]
        if "Product" in tipos or "ProductGroup" in tipos:
            encontrados.append(dados)
        if "@graph" in dados:
            encontrados += _produtos_no_jsonld(dados["@graph"])
    return encontrados


_DISPONIBILIDADE_ESGOTADA = ("OutOfStock", "SoldOut", "Discontinued")
_TIPOS_PRECO_DE = ("ListPrice", "StrikethroughPrice", "MSRP")


def _ler_oferta(oferta):
    """Lê uma oferta. Devolve (preco, esgotado, preco_de)."""
    if not isinstance(oferta, dict):
        return None, False, None

    disponibilidade = str(oferta.get("availability", ""))
    esgotado = any(p in disponibilidade for p in _DISPONIBILIDADE_ESGOTADA)
    preco = None
    preco_de = None

    # AggregateOffer (várias ofertas juntas) usa lowPrice
    for chave in ("price", "lowPrice"):
        preco = texto_para_preco(oferta.get(chave))
        if preco:
            break

    # Algumas lojas colocam o preço dentro de priceSpecification
    especs = oferta.get("priceSpecification")
    if isinstance(especs, dict):
        especs = [especs]
    if isinstance(especs, list):
        for espec in especs:
            if not isinstance(espec, dict):
                continue
            valor = texto_para_preco(espec.get("price"))
            if not valor:
                continue
            if any(t in str(espec.get("priceType", "")) for t in _TIPOS_PRECO_DE):
                preco_de = preco_de or valor
            elif not preco:
                preco = valor

    # Ofertas aninhadas (AggregateOffer com lista de offers dentro)
    if not preco and "offers" in oferta:
        return _preco_das_ofertas(oferta["offers"])

    return preco, esgotado, preco_de


def _preco_das_ofertas(ofertas):
    """Lê o preço de 'offers'. Prefere a primeira oferta com estoque.
    Devolve (preco, esgotado, preco_de)."""
    if isinstance(ofertas, dict):
        return _ler_oferta(ofertas)
    if not isinstance(ofertas, list):
        return None, False, None

    primeira_esgotada = None
    for oferta in ofertas:
        preco, esgotado, preco_de = _preco_das_ofertas(oferta)
        if not preco:
            continue
        if not esgotado:
            return preco, False, preco_de
        primeira_esgotada = primeira_esgotada or (preco, True, preco_de)
    return primeira_esgotada or (None, False, None)


# ============================================================
# "PREÇO DE" E PIX
# ============================================================

# Elementos que costumam ter o preço riscado. Só usamos o valor se ele for
# maior que o preço atual (e não absurdamente maior).
_SELETORES_PRECO_DE = (
    "s.andes-money-amount--previous",  # Mercado Livre
    "[class*='listPrice']",
    "[class*='list-price']",
    "[class*='ListPrice']",
    "[class*='price-old']",
    "[class*='old-price']",
    "[class*='oldPrice']",
    "[class*='price-from']",
    "[class*='priceFrom']",
    "[class*='strikethrough']",
    "s",
    "del",
)

_RE_VTEX_LISTPRICE = re.compile(r'"ListPrice"\s*:\s*(\d+(?:\.\d+)?)')
_RE_PIX_DEPOIS = re.compile(
    r"R\$\s*(\d[\d.]*(?:,\d{2})?)\s*(?:à vista\s*)?(?:no|via|com|pelo|pagando no)\s+pix",
    re.IGNORECASE,
)
_RE_PIX_ANTES = re.compile(r"pix[^R\d]{0,30}R\$\s*(\d[\d.]*(?:,\d{2})?)", re.IGNORECASE)


def _valor_andes(elemento):
    """Preço no formato do Mercado Livre: fração e centavos em spans separados."""
    fracao = elemento.select_one(".andes-money-amount__fraction")
    if not fracao:
        return None
    centavos = elemento.select_one(".andes-money-amount__cents")
    texto = fracao.get_text(strip=True).replace(".", "")
    if centavos:
        texto += "," + centavos.get_text(strip=True)
    return texto_para_preco(texto)


def _preco_de_valido(valor, preco):
    return bool(valor and preco and preco < valor <= preco * 4)


def _achar_preco_de(sopa, html_texto, preco):
    for seletor in _SELETORES_PRECO_DE:
        for elemento in sopa.select(seletor)[:5]:
            valor = _valor_andes(elemento) or extrair_preco_de_texto(
                elemento.get("content") or elemento.get_text(" ", strip=True)
            )
            if _preco_de_valido(valor, preco):
                return valor

    for atributos in (
        {"property": "product:original_price:amount"},
        {"itemprop": "highPrice"},
    ):
        tag = sopa.find(attrs=atributos)
        if tag:
            valor = texto_para_preco(tag.get("content") or tag.get_text(strip=True))
            if _preco_de_valido(valor, preco):
                return valor

    # Lojas VTEX guardam o estado do produto num JSON dentro da página
    for achado in _RE_VTEX_LISTPRICE.finditer(html_texto[:3_000_000]):
        valor = texto_para_preco(float(achado.group(1)))
        if _preco_de_valido(valor, preco):
            return valor
    return None


def _achar_pix(texto_pagina, preco):
    """Procura 'R$ 626,90 no Pix' ou 'Pix: R$ 626,90' perto do preço.
    Só aceita se for um pouco menor que o preço normal."""
    for regex in (_RE_PIX_DEPOIS, _RE_PIX_ANTES):
        for achado in regex.finditer(texto_pagina):
            valor = texto_para_preco(achado.group(1))
            if valor and preco * 0.7 <= valor < preco:
                return valor
    return None


# ============================================================
# ACHAR O PREÇO NA PÁGINA
# ============================================================

def _preco_principal(sopa, seletor):
    """Devolve (preco, metodo, esgotado, preco_de_do_jsonld)."""
    # 1) Seletor CSS manual
    if seletor:
        elemento = sopa.select_one(seletor)
        if elemento:
            preco = _valor_andes(elemento) or extrair_preco_de_texto(
                elemento.get("content") or elemento.get_text(" ", strip=True)
            )
            if preco:
                return preco, "seletor", False, None

    # 2) JSON-LD: dados estruturados que a maioria das lojas coloca pro Google
    for script in sopa.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for produto in _produtos_no_jsonld(dados):
            preco, esgotado, preco_de = _preco_das_ofertas(produto.get("offers"))
            if preco:
                return preco, "json-ld", esgotado, preco_de

    # 3) Meta tags de preço
    for atributos in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    ):
        tag = sopa.find(attrs=atributos)
        if tag:
            preco = texto_para_preco(tag.get("content") or tag.get_text(strip=True))
            if preco:
                return preco, "meta", False, None

    return None, None, False, None


def achar_preco(html, seletor=None):
    """Lê a página inteira. Devolve uma Leitura (preco=None se não achou)."""
    sopa = BeautifulSoup(html, PARSER)
    try:
        preco, metodo, esgotado, preco_de = _preco_principal(sopa, seletor)
        if not preco:
            return Leitura(None, None)

        html_texto = html if isinstance(html, str) else html.decode("utf-8", "ignore")
        if not _preco_de_valido(preco_de, preco):
            preco_de = _achar_preco_de(sopa, html_texto, preco)

        for lixo in sopa(["script", "style", "noscript"]):
            lixo.decompose()
        preco_pix = _achar_pix(sopa.get_text(" ", strip=True), preco)

        return Leitura(preco, metodo, esgotado, preco_de, preco_pix)
    finally:
        sopa.decompose()
        del sopa
        gc.collect()


# ============================================================
# BAIXAR
# ============================================================

_RE_CAPTCHA = re.compile(
    rb"captcha|are you a robot|you are a robot|acesso negado|access denied",
    re.IGNORECASE,
)


def baixar_pagina(url):
    """Baixa o HTML. Devolve (html_bytes, erro, bloqueado)."""
    try:
        resposta = _sessao.get(url, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        return None, f"erro de conexão: {e.__class__.__name__}", False

    if resposta.status_code in (403, 429):
        return None, f"bloqueado pela loja (HTTP {resposta.status_code})", True
    if resposta.status_code != 200:
        return None, f"HTTP {resposta.status_code}", False
    if "captcha" in resposta.url.lower() or "account-verification" in resposta.url:
        return None, "a loja pediu captcha/verificação", True
    return resposta.content[:TAMANHO_MAX], None, False


def checar_url(url, seletor=None):
    """Baixa e lê. É a função que roda numa thread separada (to_thread)."""
    html, erro, bloqueado = baixar_pagina(url)
    if erro:
        return Resultado(erro=erro, bloqueado=bloqueado)

    adaptador = lojas.adaptador_para(url)
    leitura = adaptador(html, seletor) if adaptador else achar_preco(html, seletor)

    if not leitura or not leitura.preco:
        if _RE_CAPTCHA.search(html[:300_000]):
            return Resultado(erro="a loja pediu captcha", bloqueado=True)
        return Resultado(erro="preço não encontrado na página")
    return Resultado(leitura=leitura)
