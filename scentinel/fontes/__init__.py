"""Fontes de busca: lugares onde o bot procura ofertas sozinho (sem link cadastrado).

Cada fonte tem um `id` estável (ex.: 'vtex:www.epocacosmeticos.com.br'), um `nome` pra mostrar
e `buscar(termo) -> ResultadoBusca`. `buscar` é síncrona (requests) e roda numa thread.
"""

from dataclasses import dataclass, field

import requests

from scentinel import coleta


@dataclass
class Achado:
    """Um produto encontrado numa busca."""
    titulo: str
    url: str
    preco: float
    loja: str
    marca: str | None = None
    preco_de: float | None = None
    preco_pix: float | None = None
    disponivel: bool = True
    oficial: bool = False  # loja oficial / vendido pela própria loja
    ml: float | None = None
    variacao: str | None = None  # nome do SKU (ex.: "100ml")
    imagem: str | None = None  # foto do produto (vai no post)
    chave: str | None = None  # id do mesmo produto entre vendedores (ex.: catálogo do ML)
    genero: str | None = None  # 'm', 'f', 'u' (unissex) quando a loja diz; None = não sei


@dataclass
class ResultadoBusca:
    achados: list = field(default_factory=list)
    erro: str | None = None
    bloqueado: bool = False
    pulado: bool = False  # a tarefa não chegou a falar com a loja (nada a contar, nem sucesso nem erro)
    pausa_horas: float | None = None  # bloqueio curto (ex.: limite de velocidade da API): pausa menor e sem aviso


def baixar_json(url, params=None, headers=None, timeout=coleta.TIMEOUT_S):
    """GET de JSON com a mesma sessão/cabeçalhos educados da coleta. Devolve (dados, erro, bloqueado)."""
    cab = {"Accept": "application/json"}
    if headers:
        cab.update(headers)
    try:
        resposta = coleta._sessao.get(url, params=params, headers=cab, timeout=timeout)
    except requests.RequestException as e:
        return None, f"erro de conexão: {e.__class__.__name__}", False
    if resposta.status_code in (403, 429):
        return None, f"bloqueado (HTTP {resposta.status_code})", True
    if resposta.status_code not in (200, 206):
        return None, f"HTTP {resposta.status_code}", False
    try:
        return resposta.json(), None, False
    except ValueError:
        if b"captcha" in resposta.content[:200_000].lower():
            return None, "a loja pediu captcha", True
        return None, "resposta não é JSON", False


TIPOS = {}  # tipo -> classe; preenchido pelos módulos de fonte


def registrar(tipo):
    def deco(cls):
        TIPOS[tipo] = cls
        return cls
    return deco


def montar_fontes(config_fontes):
    """config_fontes: lista de dicts do config.toml ([[descoberta.fontes]])."""
    from scentinel.fontes import mercadolivre, vtex  # noqa: F401  (registra os tipos)

    fontes = []
    for c in config_fontes:
        if not c.get("ativo", True):
            continue
        cls = TIPOS.get(c.get("tipo"))
        if not cls:
            raise ValueError(f"fonte de tipo desconhecido no config.toml: {c.get('tipo')!r}")
        fonte = cls(**{k: v for k, v in c.items() if k not in ("tipo", "ativo", "categorias", "marcas")})
        fonte.categorias = set(c.get("categorias") or [])  # vazio = todas
        fonte.marcas = set(c.get("marcas") or [])  # vazio = todas
        fontes.append(fonte)
    return fontes


def fonte_busca_item(fonte, item):
    """A loja vende esse tipo de perfume? (evita procurar Malbec na Sephora)"""
    categorias = getattr(fonte, "categorias", None)
    marcas = getattr(fonte, "marcas", None)
    if categorias and item.categoria not in categorias:
        return False
    if marcas and item.marca not in marcas:
        return False
    return True
