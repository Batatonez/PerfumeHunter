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
from collections import Counter
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
    """Token salvo, renovado se estiver vencendo. None se não houver login (ou não renovou)."""
    return token_e_motivo(caminho)[0]


def token_e_motivo(caminho=None):
    """(token, motivo). motivo: None (ok), 'sem_login', 'recusado' (o ML negou a renovação: refazer o
    ml-login) ou 'falhou' (rede/ML fora do ar na renovação: tenta de novo depois)."""
    with _trava_token:  # o refresh_token só vale uma vez: dois refresh juntos derrubariam o login
        return _token_valido(Path(caminho or ARQ_TOKEN))


def _token_valido(caminho):
    try:
        registro = json.loads(caminho.read_text())
    except (OSError, ValueError):
        return None, "sem_login"
    if not isinstance(registro, dict) or not registro.get("access_token"):
        return None, "sem_login"
    if time.time() < registro.get("expira_em", 0):
        return registro["access_token"], None
    cid, segredo, _ = credenciais()
    if not registro.get("refresh_token") or not cid or not segredo:
        return None, "recusado"
    dados, status = _pedir_token({
        "grant_type": "refresh_token", "client_id": cid, "client_secret": segredo,
        "refresh_token": registro["refresh_token"],
    })
    if status == 200 and dados.get("access_token"):
        return _salvar_token(dados, caminho)["access_token"], None
    # outro processo (ex.: o ml-teste no terminal) pode ter renovado agora mesmo com o mesmo refresh_token:
    # nesse caso o arquivo já tem o token novo
    try:
        novo = json.loads(caminho.read_text())
        if novo.get("refresh_token") != registro["refresh_token"] and time.time() < novo.get("expira_em", 0):
            return novo["access_token"], None
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    if status in (400, 401, 403):  # invalid_grant: o login foi revogado/venceu de vez
        return None, "recusado"
    return None, "falhou"


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
PAUSA_API_S = (0.6, 1.5)  # respiro entre chamadas seguidas da mesma busca (o ML dá 429 se correr)
MAX_PAGINAS_OFERTAS = 3  # páginas de vendedores por produto (só se a 1ª não tiver loja oficial)
MAX_CACHE_PRODUTOS = 3000
MAX_FALHAS_DE_REDE = 2  # erros de rede seguidos numa tarefa: para (não segura a fila)
PAUSA_LIMITE_HORAS = 0.5
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
    genero: str | None = None

    @property
    def url(self):
        return url_produto(self.id)


@dataclass
class OfertaML:
    preco: float
    preco_de: float | None
    oficial: bool
    item_id: str | None = None
    vendedor: str | None = None


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
        genero=catalogo.genero_do_texto(genero) if genero else catalogo.genero_do_texto(nome),
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
        vendedor = str(r["seller_id"]) if r.get("seller_id") else None
        saida.append(OfertaML(preco, de if de and de > preco else None, oficial, r.get("item_id"), vendedor))
    return sorted(saida, key=lambda o: o.preco)


def url_anuncio(item_id):
    """Link do anúncio de um vendedor (o ML abre a página do produto com esse vendedor)."""
    numero = re.sub(r"\D", "", str(item_id or ""))
    return f"https://produto.mercadolivre.com.br/MLB-{numero}-_JM" if numero else None


def achado_do_produto(produto, oferta, link_do_vendedor=False, bem_avaliado=False):
    """O link é a página do produto (fica o mesmo quando o vendedor mais barato muda: o histórico não se perde).
    link_do_vendedor: o anúncio daquele vendedor (vendedor comum)."""
    return Achado(
        titulo=produto.nome,
        url=(url_anuncio(oferta.item_id) if link_do_vendedor else None) or produto.url,
        preco=oferta.preco,
        loja="Mercado Livre",
        marca=produto.marca,
        preco_de=oferta.preco_de,
        oficial=oferta.oficial,
        ml=produto.ml,
        imagem=produto.imagem,
        genero=produto.genero,
        bem_avaliado=bem_avaliado,
    )


def reputacao_boa(dados):
    """/users/{id} -> vendedor top? Termômetro verde (5_green) e MercadoLíder Gold ou Platinum."""
    rep = _obj(_obj(dados).get("seller_reputation"))
    return str(rep.get("level_id") or "").startswith("5") and rep.get("power_seller_status") in ("gold", "platinum")


def detalhe_catalogo(total, situacao):
    """Explica em português o que o catálogo do ML respondeu (pro /locate que não achou nada)."""
    if not total:
        return "nenhum produto com esse nome no catálogo do ML"
    partes = []
    if situacao.get("com_vendedor"):
        partes.append(f"{situacao['com_vendedor']} com vendedor")
    if situacao.get("sem_vendedor"):
        partes.append(f"{situacao['sem_vendedor']} sem ninguém vendendo agora")
    if situacao.get("escondido"):
        partes.append(f"{situacao['escondido']} com os vendedores escondidos pelo ML")
    if situacao.get("lixo"):
        partes.append(f"{situacao['lixo']} kit/decant/amostra")
    return f"catálogo do ML: {total} produto(s)" + (f" ({', '.join(partes)})" if partes else "")


def _grave(erro):
    """Erro que vale pra loja toda (login, recusa, limite): para a tarefa na hora."""
    return any(c in (erro or "") for c in ("401", "403", "429"))


def produto_do_link(url):
    m = re.search(r"/p/(MLB\d+)", url or "")
    return m.group(1) if m else None


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
                 categorias_varredura=None, max_categorias=4, max_produtos=5, max_produtos_varredura=8,
                 vendedor_bem_avaliado=True):
        self.nome = nome
        self.so_oficial = so_loja_oficial
        self.modo = modo  # "auto" | "api" | "html"
        self.termos_varredura = list(termos_varredura or self.TERMOS_VARREDURA)
        self.paginas = max(1, int(paginas))
        self.categorias_fixas = [str(c) for c in categorias_varredura or []]  # vazio = descobre sozinho
        self.max_categorias = max(0, int(max_categorias))
        self.max_produtos = max(1, int(max_produtos))
        self.max_produtos_varredura = max(1, int(max_produtos_varredura))
        self.publico = "todos"  # a Descoberta troca pra "masculino" se o canal for só masculino
        self._token_app = None
        self._token_app_ate = 0
        self._produtos = {}  # cache id -> ProdutoML (nome e foto não mudam)
        self._categorias = None
        self._categorias_ate = 0
        self._sem_mais_vendidos_ate = 0
        # além da loja oficial, vale vendedor comum MercadoLíder Gold/Platinum com termômetro verde
        self.vendedor_bem_avaliado = vendedor_bem_avaliado
        self._reputacao = {}  # vendedor -> (bem avaliado?, vale até)

    @property
    def _so_masculino(self):
        return self.publico == "masculino"

    # ---------------- token ----------------

    def _pegar_token(self):
        """(token, motivo). Com o login da conta salvo, nunca troca pelo token do app: ele leva 403
        no catálogo e viraria um aviso falso de bloqueio."""
        token, motivo = token_e_motivo()
        if token or _tem_token_salvo():
            return token, motivo
        cid, segredo, _ = credenciais()
        if not cid or not segredo:
            return None, "sem_login"
        if self._token_app and time.time() < self._token_app_ate:
            return self._token_app, None
        try:
            r = coleta._sessao.post(f"{API}/oauth/token", data={
                "grant_type": "client_credentials", "client_id": cid, "client_secret": segredo,
            }, headers={"Accept": "application/json"}, timeout=coleta.TIMEOUT_S)
            dados = r.json() if r.status_code == 200 else {}
        except Exception:
            dados = {}
        if isinstance(dados, dict) and dados.get("access_token"):
            self._token_app = dados["access_token"]
            self._token_app_ate = time.time() + int(dados.get("expires_in", 21600)) - 300
            return self._token_app, None
        return None, "sem_login"

    def _get(self, caminho, token, params=None):
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
        if "429" in erro:  # limite de velocidade: para meia hora (dobra se repetir), sem aviso no canal
            return ResultadoBusca(erro="api: o ML pediu pra ir mais devagar (HTTP 429)", bloqueado=True,
                                  pausa_horas=PAUSA_LIMITE_HORAS)
        return ResultadoBusca(erro=f"api: {erro}")

    def _erro_sem_token(self, motivo):
        if motivo == "falhou":  # rede/ML fora na hora de renovar: não é bloqueio, tenta na próxima
            return ResultadoBusca(erro="api: não deu pra renovar o login do ML agora (tento de novo depois)")
        return self._resultado_de_erro("HTTP 401")

    # ---------------- catálogo ----------------

    def _guardar(self, produto):
        if len(self._produtos) >= MAX_CACHE_PRODUTOS:
            self._produtos.clear()
        self._produtos[produto.id] = produto

    def _produto(self, produto_id, token):
        produto = self._produtos.get(produto_id)  # .get: o /locate pode limpar o cache ao mesmo tempo
        if produto:
            return produto, None
        dados, erro, _ = self._get(f"/products/{produto_id}", token)
        if erro:
            return None, erro
        produto = produto_do_catalogo(dados)
        if produto:
            self._guardar(produto)
        return produto, None

    def _ofertas(self, produto_id, token, situacao=None):
        """Todos os vendedores do produto (oficiais ou não), do mais barato pro mais caro.
        Só olha as páginas seguintes se a 1ª não tiver loja oficial.
        situacao: Counter opcional que ganha 'sem_vendedor', 'escondido' ou 'com_vendedor' (pro /locate explicar)."""
        situacao = situacao if situacao is not None else Counter()
        ofertas, offset = [], 0
        for pagina in range(MAX_PAGINAS_OFERTAS):
            if pagina:
                _respiro()
            dados, erro, _ = self._get(f"/products/{produto_id}/items", token, {"offset": offset} if offset else None)
            if erro:
                if "404" in erro:  # "No winners found": ninguém vendendo agora
                    situacao["com_vendedor" if ofertas else "sem_vendedor"] += 1
                    return sorted(ofertas, key=lambda o: o.preco), None
                if "403" in erro:
                    # o ML esconde os vendedores de ALGUNS produtos: pula só esse (não é a loja toda bloqueada;
                    # bloqueio de verdade aparece no /products/search)
                    log.info("ML: vendedores de %s fechados (%s); pulando esse produto", produto_id, erro)
                    situacao["com_vendedor" if ofertas else "escondido"] += 1
                    return sorted(ofertas, key=lambda o: o.preco), None
                return ofertas, erro
            dados = _obj(dados)
            resultados = _lista(dados.get("results"))
            ofertas += ofertas_do_produto(dados, so_oficial=False)
            offset += len(resultados)
            total = _obj(dados.get("paging")).get("total")
            if any(o.oficial for o in ofertas) or not resultados or not isinstance(total, int) or offset >= total:
                break
        situacao["com_vendedor" if ofertas else "sem_vendedor"] += 1
        return sorted(ofertas, key=lambda o: o.preco), None

    def _achados_dos_produtos(self, produtos, token, situacao=None):
        """Melhor oferta de cada produto. Para no primeiro erro grave (login/recusa/limite) ou depois de
        MAX_FALHAS_DE_REDE erros de rede seguidos, pra não segurar a fila de requisições do bot."""
        achados, falhas = [], 0
        for i, produto in enumerate(produtos):
            if i:
                _respiro()
            ofertas, erro = self._ofertas(produto.id, token, situacao)
            if erro:
                falhas += 1
                if _grave(erro) or falhas >= MAX_FALHAS_DE_REDE:
                    return self._fechar(achados, erro)
                continue
            falhas = 0
            achados += self._escolher(produto, ofertas, token)
        return ResultadoBusca(achados=achados)

    def _escolher(self, produto, ofertas, token=None):
        """Loja oficial mais barata e, se tiver vendedor comum mais barato, ele também (com o link do anúncio
        dele). O canal posta o comum só se ele for bem avaliado; o /locate mostra todos."""
        if not ofertas:
            return []
        if not self.so_oficial:
            return [achado_do_produto(produto, ofertas[0])]
        oficiais = [o for o in ofertas if o.oficial]
        saida = [achado_do_produto(produto, oficiais[0])] if oficiais else []
        comuns = [o for o in ofertas if not o.oficial and (not oficiais or o.preco < oficiais[0].preco)]
        if comuns:
            # o mais barato bem avaliado (vai pro canal); se o mais barato de todos não for, ele também (/locate)
            bom = next((o for o in comuns[:2] if self._bem_avaliado(o.vendedor, token)), None)
            if bom:
                saida.append(achado_do_produto(produto, bom, link_do_vendedor=True, bem_avaliado=True))
            if bom is not comuns[0]:
                saida.append(achado_do_produto(produto, comuns[0], link_do_vendedor=True))
        return saida

    def _bem_avaliado(self, vendedor, token):
        """Reputação do vendedor (guardada 1 dia; se a API não responder, conta como não avaliado por 1 h)."""
        if not (self.vendedor_bem_avaliado and vendedor and token):
            return False
        guardado = self._reputacao.get(vendedor)
        if guardado and time.time() < guardado[1]:
            return guardado[0]
        _respiro()
        dados, erro, _ = self._get(f"/users/{vendedor}", token)
        bom = not erro and reputacao_boa(dados)
        if len(self._reputacao) >= MAX_CACHE_PRODUTOS:
            self._reputacao.clear()
        self._reputacao[vendedor] = (bom, time.time() + (3600 if erro else 24 * 3600))
        return bom

    def _fechar(self, achados, erro):
        """Tarefa interrompida: erro grave vale pra loja toda; erro de rede com algo achado entrega o que deu."""
        if _grave(erro) or not achados:
            return self._resultado_de_erro(erro)
        return ResultadoBusca(achados=achados)

    def _buscar_api(self, termo, token, offset=0, so_publico=False):
        params = {"status": "active", "site_id": SITE, "q": termo}
        if offset:
            params["offset"] = offset
        dados, erro, _ = self._get("/products/search", token, params)
        if erro:
            return self._resultado_de_erro(erro, sem_login=not _tem_token_salvo())
        brutos = _lista(_obj(dados).get("results"))
        situacao = Counter()
        produtos = []
        for bruto in brutos:
            produto = produto_do_catalogo(bruto)
            if not produto:
                continue
            if catalogo.e_lixo(produto.nome):
                situacao["lixo"] += 1
                continue
            self._guardar(produto)
            if so_publico and self._so_masculino and produto.genero == "f":
                continue
            produtos.append(produto)
        if self._so_masculino:  # feminino vai pro fim: não toma a vaga do masculino (o /locate ainda vê)
            produtos.sort(key=lambda p: p.genero == "f")
        produtos = produtos[: self.max_produtos]
        if produtos:
            _respiro()
            res = self._achados_dos_produtos(produtos, token, situacao)
        else:
            res = ResultadoBusca()
        res.detalhe = detalhe_catalogo(len(brutos), situacao)
        return res

    def sabe_reler(self, achado):
        return self.modo != "html" and _tem_token_salvo() and produto_do_link(achado.url) is not None

    def reler(self, achado):
        """Checagem de sanidade: relê só esse produto (1 chamada), em vez de refazer a busca inteira."""
        produto_id = produto_do_link(achado.url)
        token, motivo = self._pegar_token()
        if not token:
            return self._erro_sem_token(motivo)
        ofertas, erro = self._ofertas(produto_id, token)
        if erro:
            return self._resultado_de_erro(erro)
        if self.so_oficial:
            ofertas = [o for o in ofertas if o.oficial]
        if not ofertas:
            return ResultadoBusca()  # ninguém (oficial) vendendo agora: "sumiu da busca"
        produto = self._produtos.get(produto_id) or ProdutoML(
            produto_id, achado.titulo, achado.marca, achado.imagem, achado.ml, achado.genero)
        return ResultadoBusca(achados=[achado_do_produto(produto, ofertas[0])])

    # ---------------- mais vendidos (varredura) ----------------

    def _categorias_conhecidas(self):
        if self.categorias_fixas:
            return self.categorias_fixas[: self.max_categorias]
        if self._categorias is not None and time.time() < self._categorias_ate:
            return self._categorias
        return None

    def _descobrir_categorias(self, token):
        """Categorias de perfume do ML (a de Perfumes e as filhas). Achou tudo sem erro: guarda 1 semana;
        algum erro no caminho: tenta de novo em 1 h; nenhuma com 'perfume' no nome: 6 h."""
        conhecidas = self._categorias_conhecidas()
        if conhecidas is not None:
            return conhecidas
        achadas, erros = [], []
        dados, erro, _ = self._get(f"/categories/{CATEGORIA_BELEZA}", token)
        if erro:
            erros.append(erro)
        for filha in _lista(_obj(dados).get("children_categories")):
            if "perfum" in catalogo.texto_titulo(filha.get("name")) and filha.get("id"):
                achadas.append(str(filha["id"]))
        for cat in list(achadas):
            _respiro()
            dados, erro, _ = self._get(f"/categories/{cat}", token)
            if erro:
                erros.append(erro)
            for filha in _lista(_obj(dados).get("children_categories")):
                nome = catalogo.texto_titulo(filha.get("name"))
                if not filha.get("id") or str(filha["id"]) in achadas or _CATEGORIA_FORA.search(nome):
                    continue
                if self._so_masculino and catalogo.genero_do_texto(nome) == "f":
                    continue
                achadas.append(str(filha["id"]))
        if erros or not achadas:
            log.warning("ML: categorias de perfume: %s", erros[0] if erros else "nenhuma com 'perfume' no nome")
        horas = 1 if erros else 7 * 24 if achadas else 6
        self._categorias = achadas[: self.max_categorias]
        self._categorias_ate = time.time() + horas * 3600
        return self._categorias

    def _mais_vendidos(self, posicao):
        """Tarefa da varredura: os mais vendidos da categoria número `posicao`."""
        if time.time() < self._sem_mais_vendidos_ate:
            return ResultadoBusca(pulado=True)
        token, motivo = self._pegar_token()
        if not token:
            return self._erro_sem_token(motivo) if _tem_token_salvo() else ResultadoBusca(pulado=True)
        categorias = self._descobrir_categorias(token)
        if posicao >= len(categorias):
            return ResultadoBusca(pulado=True)
        _respiro()
        dados, erro, _ = self._get(f"/highlights/{SITE}/category/{categorias[posicao]}", token)
        if erro:
            if "403" in erro:
                # o ML não abre essa lista pra esse app: a varredura segue só pelos termos (sem pausar a loja)
                self._sem_mais_vendidos_ate = time.time() + 24 * 3600
                log.warning("ML: lista de mais vendidos fechada (%s); tento de novo amanhã", erro)
                return ResultadoBusca(pulado=True)
            if "404" in erro:  # essa categoria não tem lista: só pula ela
                return ResultadoBusca(pulado=True)
            return self._resultado_de_erro(erro)
        ids = []
        for c in _lista(_obj(dados).get("content")):
            if c.get("type") == "PRODUCT" and c.get("id") and str(c["id"]) not in ids:
                ids.append(str(c["id"]))
        produtos, falhas = [], 0
        for pid in ids:
            if len(produtos) >= self.max_produtos_varredura:
                break
            if pid not in self._produtos:
                _respiro()
            produto, erro = self._produto(pid, token)
            if erro:
                if "404" in erro or "403" in erro:  # produto sumiu ou fechado: só esse
                    continue
                falhas += 1
                if _grave(erro) or falhas >= MAX_FALHAS_DE_REDE:
                    return self._fechar([], erro)
                continue
            falhas = 0
            if not produto or catalogo.e_lixo(produto.nome):
                continue
            if self._so_masculino and produto.genero == "f":
                continue
            produtos.append(produto)
        if not produtos:
            return ResultadoBusca()
        _respiro()
        return self._achados_dos_produtos(produtos, token)

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
            token, motivo = self._pegar_token()
            if not token:
                return self._erro_sem_token(motivo)
            return self._buscar_api(termo, token, offset=pagina * 10, so_publico=True)
        if pagina:
            return ResultadoBusca(pulado=True)  # sem login só dá pra pegar a 1ª página (pela página do site)
        return self.buscar(termo)

    def tarefas_varredura(self):
        """Sem rede (roda no loop do bot): só monta a lista com o que já se sabe."""
        termos = [t for t in self.termos_varredura
                  if not (self._so_masculino and catalogo.genero_do_texto(t) == "f")]
        tarefas = [(f"ML \"{t}\" pág. {p + 1}", (lambda t=t, p=p: self._pagina(t, p)))
                   for t in termos for p in range(self.paginas)]
        if self.modo != "html" and _tem_token_salvo() and time.time() >= self._sem_mais_vendidos_ate:
            conhecidas = self._categorias_conhecidas()
            # ainda não sabe as categorias: uma tarefa descobre e já faz a primeira
            rotulos = conhecidas if conhecidas is not None else ["(descobrindo categorias)"]
            tarefas = [(f"ML mais vendidos {c}", (lambda i=i: self._mais_vendidos(i)))
                       for i, c in enumerate(rotulos[: self.max_categorias])] + tarefas
        return tarefas

    def buscar(self, termo):
        if self.modo == "html":
            return self._buscar_html(termo)
        token, motivo = self._pegar_token()
        if token:
            res = self._buscar_api(termo, token)
            # só o token do app (sem o login da conta) e o ML recusou: tenta a página do site
            if res.bloqueado and "403" in (res.erro or "") and not _tem_token_salvo() and self.modo == "auto":
                return self._buscar_html(termo)
            return res
        if _tem_token_salvo():
            return self._erro_sem_token(motivo)
        if self.modo == "api":
            return ResultadoBusca(erro="api: falta o login do ML (python -m scentinel ml-login)")
        return self._buscar_html(termo)


# ============================================================
# DIAGNÓSTICO (python -m scentinel ml-teste)
# ============================================================

def diagnostico(termo="sauvage dior", obter=None, completo=None):
    """Testa as rotas da API com o login salvo. Devolve linhas pra mostrar: só status e nomes de
    campos (nada de token nem dado da conta), pra mandar print sem medo."""
    token = token_valido()
    if not token:
        return ["❌ Sem login do ML. Rode: python -m scentinel ml-login"]
    cab = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    completo = obter is None if completo is None else completo
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
    mostrou_campos, vendedor_comum = False, None
    for bruto in resultados[:5]:  # onde cada produto se perde: sem vendedor, só vendedor comum...
        produto = produto_do_catalogo(bruto)
        if not produto:
            continue
        try:
            r = obter(f"/products/{produto.id}/items")
            itens = r.json() if r.status_code == 200 else None
            status = r.status_code
        except Exception as e:
            itens, status = None, e.__class__.__name__
        lista = [x for x in _obj(itens).get("results") or [] if isinstance(x, dict)]
        oficiais = sum(1 for x in lista if x.get("official_store_id"))
        precos = [p for p in (texto_para_preco(x.get("price")) for x in lista) if p]
        resumo = (f"{len(lista)} vendedor(es), oficiais={oficiais}, menor={min(precos) if precos else '-'}"
                  if lista else f"sem vendedor ({status})")
        linhas.append(f"   {produto.id} {produto.nome[:42]} -> {resumo}")
        if lista and not mostrou_campos:
            mostrou_campos = True
            com_loja = sum(1 for x in lista if "official_store_id" in x)
            linhas.append(f"   campo loja oficial em {com_loja}/{len(lista)} · campos: {campos(lista[0])[:160]}")
        comum = next((x for x in sorted(lista, key=lambda x: texto_para_preco(x.get("price")) or 9e9)
                      if not x.get("official_store_id") and x.get("seller_id")), None)
        vendedor_comum = vendedor_comum or (comum or {}).get("seller_id")
    if vendedor_comum:
        rep = chamar("/users/{vendedor} (reputação)", f"/users/{vendedor_comum}")
        if rep is not None:
            r = _obj(rep.get("seller_reputation"))
            linhas.append(f"   nível={r.get('level_id')} · líder={r.get('power_seller_status')} · "
                          f"bem avaliado={reputacao_boa(rep)}")
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
    if completo:  # o que o /locate mostraria (a busca de verdade, do jeito que o bot faz)
        res = FonteMercadoLivre().buscar(termo)
        linhas.append(f"{'❌' if res.erro else '✅'} /locate \"{termo}\": {len(res.achados)} oferta(s)"
                      + (f" · {res.erro}" if res.erro else ""))
        for a in res.achados[:4]:
            selo = "oficial" if a.oficial else "bem avaliado" if a.bem_avaliado else "comum"
            linhas.append(f"   R$ {a.preco:.2f} ({selo}) {a.titulo[:50]}")
    return linhas
