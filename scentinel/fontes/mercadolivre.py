"""Mercado Livre.

1. API oficial: GET https://api.mercadolibre.com/sites/MLB/search?q=termo&limit=50
   Se ela pedir login (401/403) e o .env tiver ML_CLIENT_ID e ML_CLIENT_SECRET (app grátis
   do developers.mercadolivre.com.br), pega um token de aplicação (client_credentials).
2. Plano B: a página de busca https://lista.mercadolivre.com.br/termo, lida com o parser.

Perfume falsificado é comum no ML: por padrão só entra anúncio de LOJA OFICIAL.
"""

import os
import re
import time

from bs4 import BeautifulSoup

from scentinel import coleta
from scentinel.coleta import texto_para_preco
from scentinel.fontes import Achado, ResultadoBusca, baixar_json, registrar

API = "https://api.mercadolibre.com"


def _marca_dos_atributos(resultado):
    for attr in resultado.get("attributes") or []:
        if attr.get("id") == "BRAND":
            return attr.get("value_name")
    return None


def achados_da_api(dados, so_oficial=True):
    saida = []
    for r in (dados or {}).get("results") or []:
        preco = texto_para_preco(r.get("price"))
        if not preco or not r.get("permalink"):
            continue
        if r.get("condition") and r["condition"] != "new":
            continue
        oficial = bool(r.get("official_store_id"))
        if so_oficial and not oficial:
            continue
        preco_de = texto_para_preco(r.get("original_price"))
        saida.append(Achado(
            titulo=r.get("title") or "",
            url=r["permalink"].split("#")[0],
            preco=preco,
            loja="Mercado Livre",
            marca=_marca_dos_atributos(r),
            preco_de=preco_de if preco_de and preco_de > preco else None,
            disponivel=(r.get("available_quantity") or 1) > 0,
            oficial=oficial,
        ))
    return saida


def _valor(elemento):
    return coleta._valor_andes(elemento) if elemento else None


def achados_do_html(html, so_oficial=True):
    sopa = BeautifulSoup(html, coleta.PARSER)
    cards = sopa.select("div.poly-card") or sopa.select("li.ui-search-layout__item")
    saida = []
    for card in cards:
        a = (card.select_one("a.poly-component__title") or card.select_one("h3 a")
             or card.select_one("a.ui-search-link") or card.select_one("a[href]"))
        if not a or not a.get("href"):
            continue
        titulo = a.get_text(" ", strip=True) or (card.select_one("h2") or a).get_text(" ", strip=True)
        atual = (card.select_one(".poly-price__current .andes-money-amount")
                 or card.select_one(".andes-money-amount:not(.andes-money-amount--previous)"))
        preco = _valor(atual)
        if not preco:
            continue
        texto = card.get_text(" ", strip=True)
        oficial = bool(re.search(r"loja oficial", texto, re.IGNORECASE))
        if so_oficial and not oficial:
            continue
        if re.search(r"\busado\b|recondicionado", texto, re.IGNORECASE):
            continue
        preco_de = _valor(card.select_one("s.andes-money-amount--previous"))
        saida.append(Achado(
            titulo=titulo,
            url=a["href"].split("#")[0].split("?")[0],
            preco=preco,
            loja="Mercado Livre",
            preco_de=preco_de if preco_de and preco_de > preco else None,
            preco_pix=coleta._achar_pix(texto, preco),
            oficial=oficial,
        ))
    sopa.decompose()
    return saida


@registrar("mercadolivre")
class FonteMercadoLivre:
    id = "mercadolivre"

    def __init__(self, nome="Mercado Livre", so_loja_oficial=True, modo="auto"):
        self.nome = nome
        self.so_oficial = so_loja_oficial
        self.modo = modo  # "auto" | "api" | "html"
        self._token = None
        self._token_ate = 0
        self._api_fora = False

    def _pegar_token(self):
        cid, segredo = os.environ.get("ML_CLIENT_ID"), os.environ.get("ML_CLIENT_SECRET")
        if not cid or not segredo:
            return None
        if self._token and time.time() < self._token_ate:
            return self._token
        try:
            r = coleta._sessao.post(f"{API}/oauth/token", data={
                "grant_type": "client_credentials", "client_id": cid, "client_secret": segredo,
            }, headers={"Accept": "application/json"}, timeout=coleta.TIMEOUT_S)
            dados = r.json() if r.status_code == 200 else {}
        except Exception:
            dados = {}
        if dados.get("access_token"):
            self._token = dados["access_token"]
            self._token_ate = time.time() + int(dados.get("expires_in", 21600)) - 300
            return self._token
        return None

    def _api(self, termo):
        params = {"q": termo, "limit": 50}
        dados, erro, bloq = baixar_json(f"{API}/sites/MLB/search", params=params)
        if erro and ("401" in erro or "403" in erro or bloq):
            token = self._pegar_token()
            if token:
                dados, erro, bloq = baixar_json(f"{API}/sites/MLB/search", params=params,
                                                headers={"Authorization": f"Bearer {token}"})
        if erro:
            return None, erro
        return achados_da_api(dados, self.so_oficial), None

    def _html(self, termo):
        slug = re.sub(r"[^a-z0-9]+", "-", termo.lower()).strip("-")
        html, erro, bloqueado = coleta.baixar_pagina(f"https://lista.mercadolivre.com.br/{slug}")
        if erro:
            return None, erro, bloqueado
        return achados_do_html(html, self.so_oficial), None, False

    def buscar(self, termo):
        if self.modo in ("auto", "api") and not self._api_fora:
            achados, erro = self._api(termo)
            if not erro:
                return ResultadoBusca(achados=achados)
            if self.modo == "api":
                return ResultadoBusca(erro=f"api: {erro}")
            self._api_fora = True  # não insiste na API nesta execução; vai de página
        achados, erro, bloqueado = self._html(termo)
        if erro:
            return ResultadoBusca(erro=f"página: {erro}", bloqueado=bloqueado)
        return ResultadoBusca(achados=achados)
