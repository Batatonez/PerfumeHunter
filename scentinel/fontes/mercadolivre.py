"""Mercado Livre.

1. API oficial: GET https://api.mercadolibre.com/sites/MLB/search?q=termo&limit=50
   com "Authorization: Bearer <token>". O token vem do login feito UMA vez com
   `python -m scentinel ml-login` (app grátis do developers.mercadolivre.com.br; .env com
   ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI). Ele dura poucas horas e é renovado
   sozinho com o refresh_token, guardado em dados/ml_token.json.
2. Plano B: a página de busca https://lista.mercadolivre.com.br/termo (costuma pedir captcha).

Perfume falsificado é comum no ML: por padrão só entra anúncio de LOJA OFICIAL.
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from scentinel import coleta
from scentinel.coleta import texto_para_preco
from scentinel.fontes import Achado, ResultadoBusca, baixar_json, registrar

API = "https://api.mercadolibre.com"
AUTH = "https://auth.mercadolivre.com.br/authorization"
ARQ_TOKEN = Path(__file__).resolve().parent.parent.parent / "dados" / "ml_token.json"


# ============================================================
# LOGIN (OAuth authorization_code + refresh_token)
# ============================================================

def credenciais():
    return (os.environ.get("ML_CLIENT_ID", "").strip(), os.environ.get("ML_CLIENT_SECRET", "").strip(),
            os.environ.get("ML_REDIRECT_URI", "https://www.google.com/").strip())


def link_login():
    cid, _, redirect = credenciais()
    return f"{AUTH}?" + urlencode({"response_type": "code", "client_id": cid, "redirect_uri": redirect})


def extrair_codigo(texto):
    """Aceita o código puro (TG-...) ou a URL inteira pra onde o ML redirecionou."""
    texto = texto.strip()
    if texto.startswith("http"):
        return (parse_qs(urlparse(texto).query).get("code") or [""])[0]
    return texto


def _salvar_token(dados, caminho=None):
    caminho = Path(caminho or ARQ_TOKEN)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    registro = {
        "access_token": dados["access_token"],
        "refresh_token": dados.get("refresh_token"),
        "expira_em": time.time() + int(dados.get("expires_in", 10800)) - 300,
    }
    caminho.write_text(json.dumps(registro))
    os.chmod(caminho, 0o600)
    return registro


def _pedir_token(campos):
    try:
        r = coleta._sessao.post(f"{API}/oauth/token", data=campos,
                                headers={"Accept": "application/json"}, timeout=coleta.TIMEOUT_S)
        return r.json() if r.content else {}, r.status_code
    except Exception as e:
        return {"message": e.__class__.__name__}, 0


def trocar_codigo(codigo, caminho=None):
    """Troca o código do login por access_token + refresh_token e salva. Devolve (ok, mensagem)."""
    cid, segredo, redirect = credenciais()
    if not cid or not segredo:
        return False, "falta ML_CLIENT_ID / ML_CLIENT_SECRET no .env"
    dados, status = _pedir_token({
        "grant_type": "authorization_code", "client_id": cid, "client_secret": segredo,
        "code": extrair_codigo(codigo), "redirect_uri": redirect,
    })
    if status == 200 and dados.get("access_token"):
        _salvar_token(dados, caminho)
        return True, "login ok"
    return False, f"HTTP {status}: {dados.get('message') or dados.get('error') or dados}"


def token_valido(caminho=None):
    """Token salvo, renovado se estiver vencendo. None se não houver login."""
    caminho = Path(caminho or ARQ_TOKEN)
    if not caminho.exists():
        return None
    try:
        registro = json.loads(caminho.read_text())
    except ValueError:
        return None
    if time.time() < registro.get("expira_em", 0):
        return registro["access_token"]
    cid, segredo, _ = credenciais()
    if not registro.get("refresh_token") or not cid or not segredo:
        return None
    dados, status = _pedir_token({
        "grant_type": "refresh_token", "client_id": cid, "client_secret": segredo,
        "refresh_token": registro["refresh_token"],
    })
    if status == 200 and dados.get("access_token"):
        return _salvar_token(dados, caminho)["access_token"]
    return None


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
        foto = (r.get("thumbnail") or "").replace("http://", "https://")
        foto = re.sub(r"-[A-Z]\.(jpg|webp)$", r"-O.\1", foto) or None  # versão maior da miniatura
        saida.append(Achado(
            titulo=r.get("title") or "",
            url=r["permalink"].split("#")[0],
            preco=preco,
            loja="Mercado Livre",
            marca=_marca_dos_atributos(r),
            preco_de=preco_de if preco_de and preco_de > preco else None,
            disponivel=(r.get("available_quantity") or 1) > 0,
            oficial=oficial,
            imagem=foto,
            chave=f"ml:{r['catalog_product_id']}" if r.get("catalog_product_id") else None,
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
        img = card.select_one("img")
        foto = (img.get("data-src") or img.get("src")) if img else None
        saida.append(Achado(
            titulo=titulo,
            url=a["href"].split("#")[0].split("?")[0],
            preco=preco,
            loja="Mercado Livre",
            preco_de=preco_de if preco_de and preco_de > preco else None,
            preco_pix=coleta._achar_pix(texto, preco),
            oficial=oficial,
            imagem=foto if foto and foto.startswith("http") else None,
        ))
    sopa.decompose()
    return saida


@registrar("mercadolivre")
class FonteMercadoLivre:
    id = "mercadolivre"

    TERMOS_VARREDURA = [
        "perfume masculino", "perfume feminino", "perfume importado", "perfume arabe",
        "eau de parfum", "eau de toilette", "perfume nicho", "perfume unissex",
    ]

    def __init__(self, nome="Mercado Livre", so_loja_oficial=True, modo="auto", termos_varredura=None, paginas=2):
        self.nome = nome
        self.so_oficial = so_loja_oficial
        self.modo = modo  # "auto" | "api" | "html"
        self.termos_varredura = list(termos_varredura or self.TERMOS_VARREDURA)
        self.paginas = max(1, int(paginas))
        self._token = None
        self._token_ate = 0
        self._api_fora = False

    def _pegar_token(self):
        token = token_valido()
        if token:
            return token
        cid, segredo, _ = credenciais()
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

    def _api(self, termo, offset=0):
        params = {"q": termo, "limit": 50, "offset": offset}
        if self.so_oficial:
            params["official_store"] = "all"
        token = self._pegar_token()
        cab = {"Authorization": f"Bearer {token}"} if token else None
        dados, erro, bloq = baixar_json(f"{API}/sites/MLB/search", params=params, headers=cab)
        if erro and ("401" in erro or "403" in erro or bloq) and not token:
            erro += " (faça o login: python -m scentinel ml-login)"
        if erro:
            return None, erro
        return achados_da_api(dados, self.so_oficial), None

    def _html(self, termo):
        slug = re.sub(r"[^a-z0-9]+", "-", termo.lower()).strip("-")
        html, erro, bloqueado = coleta.baixar_pagina(f"https://lista.mercadolivre.com.br/{slug}")
        if erro:
            return None, erro, bloqueado
        return achados_do_html(html, self.so_oficial), None, False

    def _pagina(self, termo, pagina):
        if pagina == 0:
            return self.buscar(termo)
        if not token_valido():
            return ResultadoBusca()  # sem login só dá pra pegar a 1ª página (pela página do site)
        achados, erro = self._api(termo, offset=pagina * 50)
        return ResultadoBusca(achados=achados or [], erro=f"api: {erro}" if erro else None)

    def tarefas_varredura(self):
        return [(f"ML \"{t}\" pág. {p + 1}", (lambda t=t, p=p: self._pagina(t, p)))
                for t in self.termos_varredura for p in range(self.paginas)]

    def buscar(self, termo):
        if self.modo in ("auto", "api") and not self._api_fora:
            achados, erro = self._api(termo)
            if not erro:
                return ResultadoBusca(achados=achados)
            if self.modo == "api" or token_valido():
                return ResultadoBusca(erro=f"api: {erro}")
            self._api_fora = True  # sem login: não insiste na API nesta execução; tenta a página
        achados, erro, bloqueado = self._html(termo)
        if erro:
            return ResultadoBusca(erro=f"página: {erro}", bloqueado=bloqueado)
        return ResultadoBusca(achados=achados)
