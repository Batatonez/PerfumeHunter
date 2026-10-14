"""Catálogo automático: todo dia às 20:00 o bot olha o que achou na semana e põe no catálogo os perfumes
masculinos/unissex novos que valem a pena.

Regras (pra não encher o catálogo de lixo nem inventar promo):
- só perfume visto em pelo menos 2 leituras na última semana (não entra anúncio que apareceu uma vez);
- só masculino ou unissex, e de marca que o bot conhece (o estilo vem dela: designer/nicho/árabe/nacional);
- não pode já estar no catálogo, nem ter sido tirado pelo dono (/catalogo remover não volta mais);
- preço normal = mediana do que a semana mostrou (se o perfume esteve em promo a semana toda, a referência
  fica baixa: o bot erra pro lado de NÃO postar);
- palavras de outras versões da mesma marca viram "evitar" (Khamrah não engole o Khamrah Qahwa);
- no máximo 15 por dia, os mais vistos primeiro.

Fica em dados/catalogo_auto.json (o catalogo.toml, feito à mão, não é mexido).
"""

import json
import logging
import re
import statistics
from collections import Counter
from dataclasses import asdict
from datetime import timedelta

from scentinel import busca, catalogo, db
from scentinel.config import PASTA_PROJETO

log = logging.getLogger("scentinel")

ARQ_AUTO = PASTA_PROJETO / "dados" / "catalogo_auto.json"
MAX_POR_DIA = 15
MIN_LEITURAS = 2
DIAS = 7
FATOR_PRECO_BOM = 0.78  # igual ao catálogo feito à mão: ~22% abaixo do normal

_CONC = re.compile(
    r"\b(extrait de parfum|eau de parfum|eau de toilette|eau de cologne|deo colonia|deo parfum|extrait|edp|edt|edc|"
    r"parfum|colonia|cologne|elixir|intense parfum)\b",
    re.IGNORECASE,
)
_GENERO = re.compile(r"\b(masculin[oa]s?|unissex|unisex|for men|for him|homem|men)\s*$", re.IGNORECASE)
# descrevem o anúncio, não são o nome do perfume
_DESCRITIVAS = {"arabe", "arabic", "desodorante", "deo", "spray", "vapo", "vaporizador", "fragrancia"}
# palavras que não diferenciam um perfume de outro (pra montar o "evitar")
_COMUNS = set(busca._PALAVRAS_INUTEIS) | {
    "perfume", "eau", "toilette", "parfum", "spray", "masculino", "unissex", "for", "men", "homme", "pour", "him",
    "man", "ml", "original", "novo", "arabe", "arabic", "importado", "lacrado", "fragrancia", "colonia", "vapo",
}


# ============================================================
# arquivo
# ============================================================

def carregar(arq=None):
    arq = arq or ARQ_AUTO
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"itens": [], "removidos": []}
    except (OSError, ValueError):
        log.exception("catalogo_auto.json ilegível: ignorando")
        return {"itens": [], "removidos": []}
    return {"itens": list(dados.get("itens", [])), "removidos": list(dados.get("removidos", []))}


def salvar(dados, arq=None):
    arq = arq or ARQ_AUTO
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(arq)  # nunca deixa o arquivo pela metade


def item_de_dict(d):
    campos = {k: v for k, v in d.items() if k in catalogo.ItemCatalogo.__dataclass_fields__}
    return catalogo.ItemCatalogo(**campos)


def itens_salvos(ids_existentes=(), arq=None):
    """Itens do catálogo automático (os que não batem com um id do catalogo.toml)."""
    saida = []
    for d in carregar(arq)["itens"]:
        try:
            item = item_de_dict(d)
        except TypeError:
            continue
        if item.id not in ids_existentes:
            saida.append(item)
    return saida


# ============================================================
# de título de loja pra item
# ============================================================

def nome_do_titulo(titulo, marca):
    """'Perfume Lattafa Hayaati Eau de Parfum 100ml Masculino' + 'Lattafa' -> 'Hayaati'."""
    nome = _GENERO.sub(" ", _CONC.sub(" ", catalogo.nome_limpo(titulo, limite=120)))
    palavras_marca = set(busca.normalizar(marca).split())
    tokens = []
    for t in nome.split():
        norm = busca.normalizar(t)
        if norm in palavras_marca or norm in _DESCRITIVAS or (not norm and not t.isalpha()):
            continue  # palavra da marca, "Árabe", "Desodorante" ou pontuação solta ("&", "-")
        tokens.append(t)
    while tokens and not busca.normalizar(tokens[-1]):
        tokens.pop()  # "de" sobrando no fim
    nome = " ".join(tokens).strip(" -–.,|&")
    return nome if len(busca.normalizar(nome)) >= 2 else None


def _palavras_que_diferenciam(texto):
    return {p for p in busca.normalizar(busca.separar_numeros(texto)).split()
            if p not in _COMUNS and p not in busca._CODIGOS and len(p) >= 2}


def _evitar(item, titulos_da_marca, titulos_proprios=()):
    """Palavras que aparecem em outros perfumes da mesma marca que contêm o nome inteiro deste
    (Khamrah -> 'qahwa', por causa do Khamrah Qahwa). Palavra que aparece nos títulos do próprio
    perfume ("Árabe", "Spray"...) não entra."""
    nome = _palavras_que_diferenciam(item.nome)
    permitidas = nome | set(busca.normalizar(item.marca).split())
    for titulo in titulos_proprios:
        permitidas |= _palavras_que_diferenciam(catalogo.nome_limpo(titulo, limite=120))
    extras = Counter()
    for titulo in titulos_da_marca:
        palavras = _palavras_que_diferenciam(catalogo.nome_limpo(titulo, limite=120))
        if nome and nome <= palavras:
            extras.update(palavras - permitidas)
    return sorted(p for p in extras if not p.isdigit() and len(p) >= 3)[:12]


def _slug(texto):
    return re.sub(r"[^a-z0-9]+", "-", busca.sem_acento(texto).lower()).strip("-")


# ============================================================
# análise
# ============================================================

def _grupos(con, agora, itens):
    """Anúncios da busca automática fora do catálogo, lidos na semana, juntos por perfume (sem o tamanho).
    Devolve (grupos, títulos de cada marca, inclusive os do catálogo)."""
    desde = db.para_texto(agora - timedelta(days=DIAS))
    ids = {i.id for i in itens}
    linhas = con.execute(
        "SELECT a.id, a.catalogo_id, a.titulo, a.ml, p.preco, pf.marca FROM anuncios a"
        " JOIN precos p ON p.anuncio_id = a.id JOIN perfumes pf ON pf.id = a.perfume_id"
        " WHERE a.origem = 'descoberta' AND p.esgotado = 0 AND p.quando >= ? AND a.titulo IS NOT NULL",
        (desde,),
    ).fetchall()
    grupos, por_marca = {}, {}
    for anuncio_id, chave, titulo, ml, preco, marca in linhas:
        if marca:
            por_marca.setdefault(busca.normalizar(marca), set()).add(titulo)
        if chave in ids or not ml:
            continue
        g = grupos.setdefault((chave or titulo).rsplit("|", 1)[0], {"titulos": Counter(), "precos": {}, "marca": marca,
                                                                     "anuncios": set()})
        g["titulos"][titulo] += 1
        g["precos"].setdefault(ml, []).append(preco)
        g["anuncios"].add(anuncio_id)
        g["marca"] = g["marca"] or marca
    return grupos, por_marca


def preparar(con, itens, agora=None):
    """Lê o banco (na thread do bot). O resultado vai pro escolher(), que pode rodar em outra thread."""
    return _grupos(con, agora or db.agora_utc(), itens)


def candidatos(con, itens, agora=None, removidos=(), limite=MAX_POR_DIA):
    return escolher(preparar(con, itens, agora), itens, removidos, limite)


def escolher(preparado, itens, removidos=(), limite=MAX_POR_DIA):
    """[(ItemCatalogo, leituras)] novos pro catálogo, os mais vistos primeiro."""
    grupos, titulos_por_marca = preparado
    existentes = {(busca.normalizar(i.marca), busca.normalizar(i.nome), i.concentracao) for i in itens}
    ids = {i.id for i in itens} | set(removidos)
    achados = []
    for g in grupos.values():
        leituras = sum(len(p) for p in g["precos"].values())
        titulo = g["titulos"].most_common(1)[0][0]
        marca = g["marca"]
        if leituras < MIN_LEITURAS or not marca:
            continue
        if not catalogo.e_perfume(titulo) or catalogo.e_lixo(titulo):
            continue
        genero = catalogo.genero_do_texto(*g["titulos"])
        if genero not in ("m", "u"):
            continue  # feminino, ou não dá pra saber
        estilo = catalogo.estilo_da_marca(marca, titulo)
        if not estilo:
            continue  # marca que o bot não conhece: pode ser contratipo/marca duvidosa
        if catalogo.identificar(titulo, itens, marca):
            continue  # já é de um item do catálogo
        nome = nome_do_titulo(titulo, marca)
        if not nome:
            continue
        conc = busca.detectar_concentracao(titulo)
        ml_ref = max(g["precos"], key=lambda m: (len(g["precos"][m]), m))
        normal = round(statistics.median(g["precos"][ml_ref]))
        item = catalogo.ItemCatalogo(
            id=_slug(f"auto {marca} {nome} {conc or ''}"),
            nome=nome,
            marca=marca,
            concentracao=conc,
            ml=sorted(g["precos"]),
            publico="masc" if genero == "m" else "unissex",
            categoria=estilo,
            preco_normal=normal,
            ml_ref=ml_ref,
            preco_bom=round(normal * FATOR_PRECO_BOM),
            busca=[f"{nome} {marca}".lower()],
        )
        if item.id in ids or (busca.normalizar(marca), busca.normalizar(nome), conc) in existentes:
            continue
        ok, _ = catalogo.casar(item, titulo, marca)
        if not ok:
            continue  # o nome montado não casa nem com o próprio título: melhor não arriscar
        item.evitar = _evitar(item, titulos_por_marca.get(busca.normalizar(marca), ()), g["titulos"])
        ids.add(item.id)
        existentes.add((busca.normalizar(marca), busca.normalizar(nome), conc))
        achados.append((item, leituras, len(g["anuncios"])))
    achados.sort(key=lambda t: (-t[1], -t[2], t[0].id))
    return [(item, leituras) for item, leituras, _ in achados[:limite]]


def gravar(novos, arq=None):
    """Acrescenta [(ItemCatalogo, leituras)] no catalogo_auto.json."""
    if not novos:
        return
    dados = carregar(arq)
    dados["itens"] += [asdict(item) for item, _ in novos]
    salvar(dados, arq)
    log.info("📚 Catálogo automático: +%d (%s)", len(novos), ", ".join(i.nome_exibicao for i, _ in novos))


def rodar(con, itens, agora=None, arq=None):
    """Análise do dia: acha os novos, grava no catalogo_auto.json e devolve [(ItemCatalogo, leituras)]."""
    novos = candidatos(con, itens, agora, carregar(arq)["removidos"])
    gravar(novos, arq)
    return novos


def remover(item_id, arq=None):
    """Tira do catálogo automático e não deixa voltar. Devolve o dict removido (ou None)."""
    dados = carregar(arq)
    item = next((d for d in dados["itens"] if d.get("id") == item_id), None)
    if not item:
        return None
    dados["itens"] = [d for d in dados["itens"] if d.get("id") != item_id]
    if item_id not in dados["removidos"]:
        dados["removidos"].append(item_id)
    salvar(dados, arq)
    return item
