"""Catálogo automático: todo dia às 20:00 o bot olha o que achou na semana e põe no catálogo os perfumes
masculinos/unissex novos que valem a pena.

Regras (pra não encher o catálogo de lixo nem inventar promo):
- só perfume visto em pelo menos 2 leituras na última semana, em 2 anúncios ou em 2 dias diferentes
  (não entra anúncio que apareceu uma vez);
- só masculino ou unissex, e de marca que o bot conhece (o estilo vem dela: designer/nicho/árabe/nacional);
- não pode já estar no catálogo, nem ter sido tirado pelo dono (/catalogo remover não volta mais);
- preço normal = mediana do que a semana mostrou (se o perfume esteve em promo a semana toda, a referência
  fica baixa: o bot erra pro lado de NÃO postar);
- palavras de outras versões da mesma marca viram "evitar" (Khamrah não engole o Khamrah Qahwa);
- não entra versão "genérica" de perfume que o catálogo já tem (Sauvage sem concentração não vira item:
  ela engoliria o EDT, o EDP e o Elixir com um preço misturado);
- no máximo 15 por dia e 60 no total, os mais vistos primeiro; quem some do ML por 30 dias sai.

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
from scentinel.formato import fuso

_FUSO = fuso()

log = logging.getLogger("scentinel")

ARQ_AUTO = PASTA_PROJETO / "dados" / "catalogo_auto.json"
MAX_POR_DIA = 15
MAX_TOTAL = 60  # cada item é mais uma busca no ML a cada 6 h: o catálogo automático não cresce sem fim
MIN_LEITURAS = 2
DIAS = 7
EXPIRA_DIAS = 30  # item que ninguém mais vende há 30 dias sai sozinho (pode voltar se reaparecer)
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


def itens_salvos(ids_existentes=(), arq=None, manuais=()):
    """Itens do catálogo automático. Fica de fora o que bate com um id do catalogo.toml, o que um item feito
    à mão já cobre (ele entrou no catalogo.toml depois) e nome genérico demais (de versão antiga)."""
    saida = []
    for d in carregar(arq)["itens"]:
        try:
            item = item_de_dict(d)
        except TypeError:
            continue
        if item.id in ids_existentes or not _palavras_que_diferenciam(item.nome):
            continue
        if manuais and _ja_coberto(item.marca, _palavras_do_nome(item.nome, item.marca), manuais):
            continue
        saida.append(item)
    return saida


# ============================================================
# de título de loja pra item
# ============================================================

def nome_do_titulo(titulo, marca):
    """'Perfume Lattafa Hayaati Eau de Parfum 100ml Masculino' + 'Lattafa' -> 'Hayaati'."""
    nome = _GENERO.sub(" ", _CONC.sub(" ", catalogo.nome_limpo(titulo, limite=120)))
    palavras_marca = set(busca.normalizar(marca).split())
    marca_crua = set(catalogo._texto_marca(marca).split())  # com o "de"/"le" da marca ("Parfums de Marly")
    tokens = []
    for t in nome.split():
        norm = busca.normalizar(t)
        if norm in palavras_marca or norm in _DESCRITIVAS or (not norm and not t.isalpha()):
            continue  # palavra da marca, "Árabe", "Desodorante" ou pontuação solta ("&", "-")
        if not norm and catalogo._texto_marca(t) in marca_crua:
            continue  # o "de" de "Parfums de Marly", o "Le" de "Le Labo"
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
            extras.update(p for p in palavras - permitidas if not p.isdigit() and len(p) >= 3)
    return sorted(p for p, _ in extras.most_common(12))  # as versões que mais aparecem


def _palavras_do_nome(item_ou_nome, marca):
    """Palavras que identificam o perfume dentro da marca (sem concentração e sem a marca)."""
    marca_norm = set(busca.normalizar(marca).split())
    return frozenset(p for p in catalogo._palavras(busca.normalizar(busca.separar_numeros(item_ou_nome)))
                     if p not in marca_norm)


def _ja_coberto(marca, palavras, itens):
    """O catálogo já tem esse perfume (com qualquer concentração) ou uma versão mais específica dele?
    Ex.: 'Sauvage' sem concentração é coberto por Sauvage EDT/EDP/Elixir; 'Eros' pelo Eros EDT/EDP."""
    for item in itens:
        if busca.marca_bate(marca, item.marca) and palavras <= _palavras_do_nome(item.nome, item.marca):
            return True
    return False


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
        "SELECT a.id, a.catalogo_id, a.titulo, a.ml, p.preco, pf.marca, p.quando FROM anuncios a"
        " JOIN precos p ON p.anuncio_id = a.id JOIN perfumes pf ON pf.id = a.perfume_id"
        " WHERE a.origem = 'descoberta' AND p.esgotado = 0 AND p.quando >= ? AND a.titulo IS NOT NULL",
        (desde,),
    ).fetchall()
    grupos, por_marca = {}, {}
    for anuncio_id, chave, titulo, ml, preco, marca, quando in linhas:
        if marca:
            por_marca.setdefault(busca.normalizar(marca), set()).add(titulo)
        if chave in ids or not ml:
            continue
        g = grupos.setdefault((chave or titulo).rsplit("|", 1)[0], {"titulos": Counter(), "precos": {}, "marca": marca,
                                                                     "anuncios": set(), "dias": set()})
        g["titulos"][titulo] += 1
        g["precos"].setdefault(ml, []).append(preco)
        g["anuncios"].add(anuncio_id)
        g["dias"].add(db.de_texto(quando).astimezone(_FUSO).date())  # dia em São Paulo, não em UTC
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
    ids = {i.id for i in itens} | set(removidos)
    escolhidos = set()  # (marca, palavras do nome) que já entraram nesta rodada
    achados = []
    for g in grupos.values():
        leituras = sum(len(p) for p in g["precos"].values())
        titulo = g["titulos"].most_common(1)[0][0]
        marca = g["marca"]
        if leituras < MIN_LEITURAS or not marca:
            continue
        if len(g["anuncios"]) < 2 and len(g["dias"]) < 2:
            continue  # um anúncio só, num dia só: pode ser coisa de momento
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
        palavras = _palavras_do_nome(nome, marca)
        chave = (busca.normalizar(marca), palavras)
        if not _palavras_que_diferenciam(nome):
            continue  # "Pour Homme", "Man": genérico demais, engoliria o Eros, o Chrome, o Invictus...
        if not palavras or chave in escolhidos or _ja_coberto(marca, palavras, itens):
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
        if item.id in ids:
            continue
        ok, _ = catalogo.casar(item, titulo, marca)
        if not ok:
            continue  # o nome montado não casa nem com o próprio título: melhor não arriscar
        item.evitar = _evitar(item, titulos_por_marca.get(busca.normalizar(marca), ()), g["titulos"])
        ids.add(item.id)
        escolhidos.add(chave)
        achados.append((item, leituras, len(g["anuncios"])))
    achados.sort(key=lambda t: (-t[1], -t[2], t[0].id))
    return [(item, leituras) for item, leituras, _ in achados[:limite]]


def vagas(arq=None):
    """Quantos ainda podem entrar hoje (máximo por dia e no total)."""
    return max(0, min(MAX_POR_DIA, MAX_TOTAL - len(carregar(arq)["itens"])))


def gravar(novos, arq=None, agora=None):
    """Acrescenta [(ItemCatalogo, leituras)] no catalogo_auto.json (o que já estiver lá não duplica).
    Devolve os que entraram de fato."""
    dados = carregar(arq)
    ja = {d.get("id") for d in dados["itens"]} | set(dados["removidos"])
    entram = [(item, n) for item, n in novos if item.id not in ja]
    if not entram:
        return []
    quando = db.para_texto(agora or db.agora_utc())
    dados["itens"] += [{**asdict(item), "criado_em": quando} for item, _ in entram]
    salvar(dados, arq)
    log.info("📚 Catálogo automático: +%d (%s)", len(entram), ", ".join(i.nome_exibicao for i, _ in entram))
    return entram


def expirar(con, agora=None, arq=None):
    """Tira (sem bloquear a volta) o que entrou há mais de 30 dias e não teve leitura nos últimos 30.
    Lê o banco: roda na thread do bot. Devolve os ids que saíram."""
    agora = agora or db.agora_utc()
    limite = agora - timedelta(days=EXPIRA_DIAS)
    dados = carregar(arq)
    ficam, saem = [], []
    for d in dados["itens"]:
        criado = db.de_texto(d.get("criado_em")) if d.get("criado_em") else None
        visto = con.execute(
            "SELECT 1 FROM precos p JOIN anuncios a ON a.id = p.anuncio_id"
            " WHERE a.catalogo_id = ? AND p.quando >= ? LIMIT 1", (d.get("id"), db.para_texto(limite)),
        ).fetchone()
        if criado and criado < limite and not visto:
            saem.append(d.get("id"))
        else:
            ficam.append(d)
    if saem:
        dados["itens"] = ficam
        salvar(dados, arq)
        log.info("📚 Catálogo automático: saíram %d sem anúncio há %d dias (%s)", len(saem), EXPIRA_DIAS,
                 ", ".join(saem))
    return saem


def rodar(con, itens, agora=None, arq=None):
    """Análise do dia: acha os novos, grava no catalogo_auto.json e devolve [(ItemCatalogo, leituras)]."""
    novos = candidatos(con, itens, agora, carregar(arq)["removidos"], vagas(arq))
    return gravar(novos, arq, agora)


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
