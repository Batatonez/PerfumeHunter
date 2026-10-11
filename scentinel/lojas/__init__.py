"""Lojas: nome bonito pra mostrar + adaptadores específicos.

Regra: o parser genérico (coleta.achar_preco) vem primeiro. Só cria um
adaptador aqui quando o genérico falhar numa página REAL da loja.

Um adaptador é uma função (html_bytes, seletor) -> coleta.Leitura | None,
registrada em ADAPTADORES com um pedaço do domínio como chave.
"""

from urllib.parse import urlparse

NOMES = {
    "mercadolivre": "Mercado Livre",
    "mercadolibre": "Mercado Livre",
    "boticario": "O Boticário",
    "natura": "Natura",
    "epocacosmeticos": "Época Cosméticos",
    "sephora": "Sephora",
    "belezanaweb": "Beleza na Web",
    "perfumariasumire": "Sumirê",
    "lojasrenner": "Renner",
    "magazineluiza": "Magalu",
    "magalu": "Magalu",
    "drogasil": "Drogasil",
    "drogaraia": "Droga Raia",
    "eudora": "Eudora",
    "avon": "Avon",
    "giraofertas": "Giraofertas",
    "maisperfumes": "Mais Perfumes",
    "lojadoperfume": "Loja do Perfume",
}

# chave = pedaço do domínio, valor = função adaptadora
ADAPTADORES = {}


def _dominio(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def nome_da_loja(url):
    dominio = _dominio(url)
    for chave, nome in NOMES.items():
        if chave in dominio:
            return nome
    base = dominio.split(".")[0] if dominio else "?"
    return base.capitalize()


def adaptador_para(url):
    dominio = _dominio(url)
    for chave, funcao in ADAPTADORES.items():
        if chave in dominio:
            return funcao
    return None
