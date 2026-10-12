"""Mercado Livre.

1. API de catálogo (a busca /sites/MLB/search fechou em 2025, veja mais abaixo), com
   "Authorization: Bearer <token>". O token vem do login feito UMA vez com
   `python -m scentinel ml-login` (app grátis do developers.mercadolivre.com.br; .env com
   ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI). Ele dura poucas horas e é renovado
   sozinho com o refresh_token, guardado em dados/ml_token.json.
2. Plano B: a página de busca https://lista.mercadolivre.com.br/termo (costuma pedir captcha).

Perfume falsificado é comum no ML: por padrão só entra anúncio de LOJA OFICIAL.
"""

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from scentinel import catalogo, coleta
from scentinel.coleta import texto_para_preco
from scentinel.fontes import Achado, ResultadoBusca, baixar_json, registrar

API = "https://api.mercadolibre.com"
AUTH = "https://auth.mercadolivre.com.br/authorization"
ARQ_TOKEN = Path(__file__).resolve().parent.parent.parent / "dados" / "ml_token.json"

log = logging.getLogger(__name__)
_trava_token = threading.Lock()  # o /locate e a varredura podem pedir token ao mesmo tempo


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
        if not dados.get("refresh_token"):
            return True, ("login ok, MAS o ML não mandou refresh_token: em ~6 h o acesso vence. "
                          "No app do devcenter, marque 'Refresh Token' / 'offline_access' e faça o login de novo.")
        return True, "login ok"
    return False, f"HTTP {status}: {dados.get('message') or dados.get('error') or dados}"


def token_valido(caminho=None):
    """Token salvo, renovado se estiver vencendo. None se não houver login."""
    with _trava_token:  # o refresh_token só vale uma vez: dois refresh juntos derrubariam o login
        return _token_valido(Path(caminho or ARQ_TOKEN))


def _token_valido(caminho):
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


def _valor(elemento):
    return coleta._valor_andes(elemento) if elemento else None


def achados_do_html(html, so_oficial=True, ja_filtrado=False):
    """ja_filtrado: a URL já pediu só loja oficial (_Loja_all), então todo card é oficial."""
    sopa = BeautifulSoup(html, coleta.PARSER)
    cards = sopa.select("div.poly-card") or sopa.select("li.ui-search-layout__item")
    saida = []
    for card in cards:
        a = (card.select_one("a.poly-component__title") or card.select_one("h3 a")
             or card.select_one("a.ui-search-link") or card.select_one("a[href]"))
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if "click1." in href or "/mclics/" in href:  # anúncio patrocinado: link de rastreio, sem id estável
            continue
        titulo = a.get_text(" ", strip=True) or (card.select_one("h2") or a).get_text(" ", strip=True)
        atual = (card.select_one(".poly-price__current .andes-money-amount")
                 or card.select_one(".andes-money-amount:not(.andes-money-amount--previous)"))
        preco = _valor(atual)
        if not preco:
            continue
        texto = card.get_text(" ", strip=True)
        oficial = ja_filtrado or bool(re.search(r"loja oficial", texto, re.IGNORECASE))
        if so_oficial and not oficial:
            continue
        if re.search(r"\busado\b|recondicionado", texto, re.IGNORECASE):
            continue
        preco_de = _valor(card.select_one("s.andes-money-amount--previous"))
        img = card.select_one("img")
        foto = (img.get("data-src") or img.get("src")) if img else None
        saida.append(Achado(
            titulo=titulo,
            url=href.split("#")[0].split("?")[0],
            preco=preco,
            loja="Mercado Livre",
            preco_de=preco_de if preco_de and preco_de > preco else None,
            preco_pix=coleta._achar_pix(texto, preco),
            oficial=oficial,
            imagem=foto if foto and foto.startswith("http") else None,
        ))
    sopa.decompose()
    return saida


# ============================================================
# API DE CATÁLOGO (com login)
# ============================================================
# Desde 2025 o ML fechou a busca /sites/MLB/search pra quase todo app (403 mesmo logado).
# O que segue aberto com o login: o catálogo de produtos.
#   /products/search?site_id=MLB&status=active&q=  -> produtos (nome, marca, foto, tamanho)
#   /products/{id}/items                          -> os anúncios de cada vendedor daquele produto
#                                                    (preço, preço "de", loja oficial, condição)
#   /highlights/MLB/category/{id}                 -> os 20 mais vendidos da categoria (varredura)

SITE = "MLB"
CATEGORIA_BELEZA = "MLB1246"  # Beleza e Cuidado Pessoal: a de Perfumes é filha dela
PAUSA_API_S = (0.4, 1.2)  # respiro entre chamadas seguidas da mesma busca
MAX_PAGINAS_OFERTAS = 3  # páginas de vendedores por produto (só se a 1ª não tiver loja oficial)
MAX_CACHE_PRODUTOS = 3000
_GENERO = {"feminino": "Feminino", "masculino": "Masculino"}
# subcategorias de perfume que não são o frasco pra pele adulta
_CATEGORIA_FORA = re.compile(r"\b(kits?|infanti[ls]|amostras?|decants?|miniaturas?|body|splash|"
                             r"ambientes?|capilar(es)?)\b")


def _respiro():
    time.sleep(random.uniform(*PAUSA_API_S))


@dataclass
class ProdutoML:
    """Uma página de produto do catálogo (o mesmo perfume, vários vendedores)."""
    id: str
    nome: str
    marca: str | None = None
    imagem: str | None = None
    ml: float | None = None

    @property
    def url(self):
        return url_produto(self.id)


@dataclass
class OfertaML:
    preco: float
    preco_de: float | None
    oficial: bool
    item_id: str | None = None


def url_produto(produto_id):
    return f"https://www.mercadolivre.com.br/p/{produto_id}"


def _atributo(dados, *ids):
    for attr in _lista(_obj(dados).get("attributes")):
        if attr.get("id") in ids:
            return attr
    return None


def _primeira_foto(dados):
    for foto in _lista(dados.get("pictures")):
        if foto.get("secure_url") or foto.get("url"):
            return (foto.get("secure_url") or foto["url"]).replace("http://", "https://")
    return None


def produto_do_catalogo(dados):
    """Um produto de /products/search ou /products/{id} -> ProdutoML (None se faltar id/nome)."""
    if not isinstance(dados, dict) or not dados.get("id") or not isinstance(dados.get("name"), str) \
            or not dados["name"].strip():
        return None
    nome = re.sub(r"\s+", " ", dados["name"]).strip()
    texto = catalogo.texto_titulo(nome)
    # nome do catálogo às vezes não diz que é perfume nem o público: completa com o que o ML sabe
    if "PERFUME" in str(dados.get("domain_id") or "").upper() and not catalogo.e_perfume(nome):
        nome = f"Perfume {nome}"
    genero = catalogo.texto_titulo((_atributo(dados, "GENDER") or {}).get("value_name") or "")
    if genero in _GENERO and not (catalogo._FEM.search(texto) or catalogo._MASC.search(texto)):
        nome = f"{nome} {_GENERO[genero]}"
    volume = _atributo(dados, "UNIT_VOLUME", "VOLUME", "NET_VOLUME")
    return ProdutoML(
        id=str(dados["id"]),
        nome=nome,
        marca=(_atributo(dados, "BRAND") or {}).get("value_name") or None,
        imagem=_primeira_foto(dados),
        ml=catalogo.extrair_ml(volume.get("value_name") or "") if volume else None,
    )


def _obj(dados):
    """Resposta que deveria ser um objeto JSON (a API às vezes devolve lista ou nada)."""
    return dados if isinstance(dados, dict) else {}


def _lista(valor):
    return [x for x in valor if isinstance(x, dict)] if isinstance(valor, list) else []


def ofertas_do_produto(dados, so_oficial=True):
    """Resposta de /products/{id}/items -> anúncios novos e à venda, do mais barato pro mais caro."""
    saida = []
    for r in _lista(_obj(dados).get("results")):
        preco = texto_para_preco(r.get("price"))
        if not preco or r.get("currency_id") not in (None, "BRL"):
            continue
        if r.get("condition") not in (None, "new") or r.get("available_quantity") == 0:
            continue
        oficial = bool(r.get("official_store_id"))
        if so_oficial and not oficial:
            continue
        de = texto_para_preco(r.get("original_price"))
        saida.append(OfertaML(preco, de if de and de > preco else None, oficial, r.get("item_id")))
    return sorted(saida, key=lambda o: o.preco)


def achado_do_produto(produto, oferta):
    """O link é a página do produto (fica o mesmo quando o vendedor mais barato muda: o histórico não se perde)."""
    return Achado(
        titulo=produto.nome,
        url=produto.url,
        preco=oferta.preco,
        loja="Mercado Livre",
        marca=produto.marca,
        preco_de=oferta.preco_de,
        oficial=oferta.oficial,
        ml=produto.ml,
        imagem=produto.imagem,
    )


def _tem_token_salvo(caminho=None):
    """Sem rede: só olha se o login já foi feito (pra montar a varredura sem travar o bot)."""
    try:
        return bool(json.loads(Path(caminho or ARQ_TOKEN).read_text()).get("access_token"))
    except (OSError, ValueError, AttributeError):
        return False


@registrar("mercadolivre")
class FonteMercadoLivre:
    id = "mercadolivre"

    TERMOS_VARREDURA = [
        "perfume masculino", "perfume feminino", "perfume importado", "perfume arabe",
        "eau de parfum", "eau de toilette", "perfume nicho", "perfume unissex",
    ]

    def __init__(self, nome="Mercado Livre", so_loja_oficial=True, modo="auto", termos_varredura=None, paginas=1,
                 categorias_varredura=None, max_categorias=6, max_produtos=6):
        self.nome = nome
        self.so_oficial = so_loja_oficial
        self.modo = modo  # "auto" | "api" | "html"
        self.termos_varredura = list(termos_varredura or self.TERMOS_VARREDURA)
        self.paginas = max(1, int(paginas))
        self.categorias_fixas = [str(c) for c in categorias_varredura or []]  # vazio = descobre sozinho
        self.max_categorias = max(0, int(max_categorias))
        self.max_produtos = max(1, int(max_produtos))
        self._token = None
        self._token_ate = 0
        self._produtos = {}  # cache id -> ProdutoML (nome e foto não mudam)
        self._categorias = None
        self._categorias_ate = 0
        self._sem_mais_vendidos_ate = 0

    # ---------------- token ----------------

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

    def _get(self, caminho, params=None, token=None):
        token = token or self._pegar_token()
        return baixar_json(f"{API}{caminho}", params=params, headers={"Authorization": f"Bearer {token}"})

    @staticmethod
    def _resultado_de_erro(erro, sem_login=False):
        if "401" in erro:  # login venceu/foi revogado: pausa e avisa no canal (senão o ML some calado)
            return ResultadoBusca(erro="api: o login do ML venceu (HTTP 401). Rode no notebook: "
                                       "python -m scentinel ml-login", bloqueado=True)
        if "403" in erro:
            dica = "faça o login: python -m scentinel ml-login" if sem_login else \
                "rode no notebook: python -m scentinel ml-teste"
            return ResultadoBusca(erro=f"api: o ML recusou a busca de produtos (HTTP 403); {dica}", bloqueado=True)
        return ResultadoBusca(erro=f"api: {erro}", bloqueado="429" in erro)

    # ---------------- catálogo ----------------

    def _guardar(self, produto):
        if len(self._produtos) >= MAX_CACHE_PRODUTOS:
            self._produtos.clear()
        self._produtos[produto.id] = produto

    def _produto(self, produto_id, token):
        produto = self._produtos.get(produto_id)  # .get: o /locate pode limpar o cache ao mesmo tempo
        if produto:
            return produto, None
        dados, erro, _ = self._get(f"/products/{produto_id}", token=token)
        if erro:
            return None, erro
        produto = produto_do_catalogo(dados)
        if produto:
            self._guardar(produto)
        return produto, None

    def _ofertas(self, produto_id, token):
        """Vendedores do produto. Só olha as páginas seguintes se a 1ª não tiver loja oficial."""
        ofertas, offset = [], 0
        for pagina in range(MAX_PAGINAS_OFERTAS):
            if pagina:
                _respiro()
            dados, erro, _ = self._get(f"/products/{produto_id}/items", {"offset": offset} if offset else None, token)
            if erro:
                if "404" in erro:  # "No winners found": ninguém vendendo agora
                    break
                return ofertas, erro
            dados = _obj(dados)
            resultados = _lista(dados.get("results"))
            ofertas += ofertas_do_produto(dados, self.so_oficial)
            offset += len(resultados)
            total = _obj(dados.get("paging")).get("total")
            if ofertas or not resultados or not isinstance(total, int) or offset >= total:
                break
        return sorted(ofertas, key=lambda o: o.preco), None

    def _achados_dos_produtos(self, produtos, token):
        achados, erros = [], []
        for i, produto in enumerate(produtos):
            if i:
                _respiro()
            ofertas, erro = self._ofertas(produto.id, token)
            if erro:
                erros.append(erro)
            if ofertas:
                achados.append(achado_do_produto(produto, ofertas[0]))
        return achados, erros

    def _buscar_api(self, termo, offset=0, token=None):
        token = token or self._pegar_token()
        if not token:
            return self._resultado_de_erro("HTTP 401")  # login salvo, mas venceu e não renovou
        params = {"status": "active", "site_id": SITE, "q": termo}
        if offset:
            params["offset"] = offset
        dados, erro, _ = self._get("/products/search", params, token)
        if erro:
            return self._resultado_de_erro(erro, sem_login=not _tem_token_salvo())
        produtos = []
        for bruto in _lista(_obj(dados).get("results")):
            produto = produto_do_catalogo(bruto)
            if produto and not catalogo.e_lixo(produto.nome):
                self._guardar(produto)
                produtos.append(produto)
        produtos = produtos[: self.max_produtos]
        if not produtos:
            return ResultadoBusca()
        _respiro()
        achados, erros = self._achados_dos_produtos(produtos, token)
        if erros and len(erros) == len(produtos):
            return self._resultado_de_erro(erros[0])
        return ResultadoBusca(achados=achados)

    # ---------------- mais vendidos (varredura) ----------------

    def _descobrir_categorias(self, token):
        """Categorias de perfume do ML (a de Perfumes e as filhas). Guarda depois de achar."""
        if self.categorias_fixas:
            return self.categorias_fixas[: self.max_categorias]
        if self._categorias is not None and time.time() < self._categorias_ate:
            return self._categorias
        achadas = []
        dados, erro, _ = self._get(f"/categories/{CATEGORIA_BELEZA}", token=token)
        for filha in _lista(_obj(dados).get("children_categories")):
            if "perfum" in catalogo.texto_titulo(filha.get("name")) and filha.get("id"):
                achadas.append(str(filha["id"]))
        for cat in list(achadas):
            _respiro()
            dados, _, _ = self._get(f"/categories/{cat}", token=token)
            for filha in _lista(_obj(dados).get("children_categories")):
                nome = catalogo.texto_titulo(filha.get("name"))
                if filha.get("id") and str(filha["id"]) not in achadas and not _CATEGORIA_FORA.search(nome):
                    achadas.append(str(filha["id"]))
        if not achadas:
            log.warning("ML: não achei as categorias de perfume (%s)", erro or "nenhuma com 'perfume' no nome")
        # achou: guarda por 1 semana; não achou: tenta de novo daqui a 6 h (não gasta chamada a cada tarefa)
        self._categorias = achadas[: self.max_categorias]
        self._categorias_ate = time.time() + (7 * 24 if achadas else 6) * 3600
        return self._categorias

    def _mais_vendidos(self, posicao):
        """Tarefa da varredura: os 20 mais vendidos da categoria número `posicao`."""
        if time.time() < self._sem_mais_vendidos_ate:
            return ResultadoBusca()
        token = self._pegar_token()
        if not token:
            return ResultadoBusca()
        categorias = self._descobrir_categorias(token)
        if posicao >= len(categorias):
            return ResultadoBusca()
        _respiro()
        dados, erro, _ = self._get(f"/highlights/{SITE}/category/{categorias[posicao]}", token=token)
        if erro:
            if "403" in erro:
                # o ML não abre essa lista pra esse app: a varredura segue só pelos termos (sem pausar a loja)
                self._sem_mais_vendidos_ate = time.time() + 24 * 3600
                log.warning("ML: lista de mais vendidos fechada (%s); tento de novo amanhã", erro)
                return ResultadoBusca()
            if "404" in erro:  # essa categoria não tem lista: só pula ela
                return ResultadoBusca()
            return ResultadoBusca(erro=f"api: {erro}")
        ids = []
        for c in _lista(_obj(dados).get("content")):
            if c.get("type") == "PRODUCT" and c.get("id") and str(c["id"]) not in ids:
                ids.append(str(c["id"]))
        produtos = []
        for pid in ids:
            if pid not in self._produtos:
                _respiro()
            produto, _ = self._produto(pid, token)
            if produto and not catalogo.e_lixo(produto.nome):
                produtos.append(produto)
        if not produtos:
            return ResultadoBusca()
        _respiro()
        achados, erros = self._achados_dos_produtos(produtos, token)
        if erros and len(erros) == len(produtos):
            return self._resultado_de_erro(erros[0])
        return ResultadoBusca(achados=achados)

    # ---------------- página do site (sem login) ----------------

    def _html(self, termo):
        slug = re.sub(r"[^a-z0-9]+", "-", termo.lower()).strip("-")
        sufixo = "_Loja_all" if self.so_oficial else ""  # filtro de loja oficial do próprio ML
        html, erro, bloqueado = coleta.baixar_pagina(f"https://lista.mercadolivre.com.br/{slug}{sufixo}")
        if erro:
            return None, erro, bloqueado
        return achados_do_html(html, self.so_oficial, ja_filtrado=self.so_oficial), None, False

    def _buscar_html(self, termo):
        achados, erro, bloqueado = self._html(termo)
        if erro:
            return ResultadoBusca(erro=f"página: {erro}", bloqueado=bloqueado)
        return ResultadoBusca(achados=achados)

    # ---------------- o que o bot chama ----------------

    def _pagina(self, termo, pagina):
        if self.modo != "html" and _tem_token_salvo():
            return self._buscar_api(termo, offset=pagina * 10)
        if pagina:
            return ResultadoBusca()  # sem login só dá pra pegar a 1ª página (pela página do site)
        return self.buscar(termo)

    def tarefas_varredura(self):
        tarefas = [(f"ML \"{t}\" pág. {p + 1}", (lambda t=t, p=p: self._pagina(t, p)))
                   for t in self.termos_varredura for p in range(self.paginas)]
        if self.modo != "html" and _tem_token_salvo():
            tarefas = [(f"ML mais vendidos #{i + 1}", (lambda i=i: self._mais_vendidos(i)))
                       for i in range(self.max_categorias)] + tarefas
        return tarefas

    def buscar(self, termo):
        token = self._pegar_token() if self.modo != "html" else None
        if token:
            res = self._buscar_api(termo, token=token)
            # só o token do app (sem o login da conta) e o ML recusou: tenta a página do site
            if not (res.bloqueado and "403" in (res.erro or "") and not _tem_token_salvo() and self.modo == "auto"):
                return res
        elif self.modo == "api":
            return ResultadoBusca(erro="api: falta o login do ML (python -m scentinel ml-login)")
        return self._buscar_html(termo)


# ============================================================
# DIAGNÓSTICO (python -m scentinel ml-teste)
# ============================================================

def diagnostico(termo="sauvage dior", obter=None):
    """Testa as rotas da API com o login salvo. Devolve linhas pra mostrar: só status e nomes de
    campos (nada de token nem dado da conta), pra mandar print sem medo."""
    token = token_valido()
    if not token:
        return ["❌ Sem login do ML. Rode: python -m scentinel ml-login"]
    cab = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    obter = obter or (lambda caminho, params=None: coleta._sessao.get(
        f"{API}{caminho}", params=params, headers=cab, timeout=coleta.TIMEOUT_S))
    linhas = []

    def chamar(rotulo, caminho, params=None):
        try:
            r = obter(caminho, params)
        except Exception as e:
            linhas.append(f"❌ ... {rotulo}: erro de conexão ({e.__class__.__name__})")
            return None
        try:
            dados = r.json()
        except ValueError:
            dados = None
        extra = ""
        if r.status_code != 200 and isinstance(dados, dict):
            extra = f" · {dados.get('message') or dados.get('error') or ''}"[:80]
        linhas.append(f"{'✅' if r.status_code == 200 else '❌'} {r.status_code} {rotulo}{extra}")
        return dados if r.status_code == 200 and isinstance(dados, dict) else None

    def campos(d):
        return ", ".join(sorted(d.keys()))[:240] if isinstance(d, dict) else "-"

    chamar("/users/me (login)", "/users/me")
    chamar("/sites/MLB/search (busca antiga)", f"/sites/{SITE}/search", {"q": termo, "limit": 1})
    busca = chamar(f"/products/search \"{termo}\"", "/products/search",
                   {"status": "active", "site_id": SITE, "q": termo})
    resultados = (busca or {}).get("results") or []
    if busca is not None:
        linhas.append(f"   {len(resultados)} produto(s). Campos: {campos(resultados[0] if resultados else None)}")
    for bruto in resultados[:2]:
        produto = produto_do_catalogo(bruto)
        if not produto:
            continue
        linhas.append(f"   produto {produto.id}: {produto.nome[:60]} · marca={produto.marca} · ml={produto.ml}")
        itens = chamar(f"/products/{produto.id}/items", f"/products/{produto.id}/items")
        lista = [r for r in (itens or {}).get("results") or [] if isinstance(r, dict)]
        if lista:
            com_loja = sum(1 for r in lista if "official_store_id" in r)
            oficiais = sum(1 for r in lista if r.get("official_store_id"))
            com_de = sum(1 for r in lista if r.get("original_price"))
            precos = [p for p in (texto_para_preco(r.get("price")) for r in lista) if p]
            linhas.append(f"   {len(lista)} vendedor(es) · campo loja oficial em {com_loja} · oficiais={oficiais} · "
                          f"com preço 'de'={com_de} · menor={min(precos) if precos else '-'}")
            linhas.append(f"   Campos: {campos(lista[0])}")
            break
    cats = chamar(f"/categories/{CATEGORIA_BELEZA} (Beleza)", f"/categories/{CATEGORIA_BELEZA}")
    perfume = [f for f in (cats or {}).get("children_categories") or []
               if "perfum" in catalogo.texto_titulo(f.get("name"))]
    if cats is not None:
        nomes = ", ".join(f"{c.get('id')} {c.get('name')}" for c in perfume) or "nenhuma"
        linhas.append(f"   categorias de perfume: {nomes}")
    if perfume:
        mais = chamar(f"/highlights/MLB/category/{perfume[0]['id']} (mais vendidos)",
                      f"/highlights/{SITE}/category/{perfume[0]['id']}")
        conteudo = (mais or {}).get("content") or []
        if mais is not None:
            tipos = sorted({c.get("type") for c in conteudo if isinstance(c, dict)})
            linhas.append(f"   {len(conteudo)} item(ns), tipos: {', '.join(map(str, tipos)) or '-'}")
    return linhas
