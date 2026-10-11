"""Catálogo de perfumes cobiçados (catalogo.toml) e o casamento título-de-loja -> item.

É o que transforma "Sauvage Dior Eau de Parfum - Perfume Masculino 100ml" em
"Sauvage EDP, 100 ml" e descarta kit, decant, contratipo, body splash etc.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from scentinel.busca import detectar_concentracao, normalizar, sem_acento

ARQ_CATALOGO = Path(__file__).resolve().parent.parent / "catalogo.toml"

CATEGORIAS = {
    "designer": "✨ Designer",
    "nicho": "💎 Nicho",
    "arabe": "🕌 Árabe",
    "nacional": "🇧🇷 Nacional",
}

# Anúncio que não é o frasco do perfume (ou é falsificação). Regex sobre o texto normalizado.
_LIXO = [
    r"\bkit\b", r"\bestojo\b", r"\bcoffret\b", r"\bgift ?set\b", r"\bconjunto\b",
    r"\bdecant\b", r"\bdecants\b", r"\bamostra\b", r"\bminiatura\b", r"\bmini\b", r"\btravel\b",
    r"\bvial\b", r"\btester\b", r"\bsem caixa\b",
    r"\bbody ?splash\b", r"\bbody ?spray\b", r"\bbody ?mist\b", r"\bhair ?mist\b",
    r"\bdesodorante\b(?! colonia)", r"\bantitranspirante\b", r"\broll ?on\b", r"\baerosol\b",
    r"\bhidratante\b", r"\blocao\b", r"\bgel\b", r"\bsabonete\b", r"\bshampoo\b", r"\boleo\b",
    r"\bcreme\b", r"\bpo\b", r"\bbalm\b", r"\bpos barba\b", r"\bafter ?shave\b",
    r"\bcontratipo\b", r"\binspirad[oa]\b", r"\binspiracao\b", r"\bsimilar\b", r"\breplica\b",
    r"\b1a linha\b", r"\bprimeira linha\b", r"\bvazio\b", r"\bcapa\b", r"\bcaixa vazia\b",
    # não é o frasco de perfume pra pele
    r"\brefil\b", r"\brefill\b", r"\bcapilar\b", r"\bcabelo\b", r"\bhair\b", r"\bmist\b",
    r"\bambiente\b", r"\bhome ?spray\b", r"\bdifusor\b", r"\baromatizador\b", r"\bpet\b", r"\bcaes\b",
    r"\bcachorro\b", r"\bgato\b",
    # jeitos de dizer "contratipo"
    r"\breferencia olfativa\b", r"\breferencia\b", r"\bversao\b", r"\bdupe\b", r"\bclone\b",
    r"\balternativa\b", r"\bequivalente\b", r"\bsimilaridade\b",
]
_RE_LIXO = re.compile("|".join(_LIXO))

_FEM = re.compile(r"\b(feminino|feminina|for her|woman|women|femme|pour femme|mujer|donna|lady)\b")
_MASC = re.compile(r"\b(masculino|masculina|for him|men|man|homme|pour homme|hombre|uomo)\b")
_RE_ML = re.compile(r"(\d{1,4}(?:[.,]\d)?)\s*ml\b")


def texto_titulo(texto):
    """Minúsculo, sem acento, pontuação vira espaço (mantém números e 'ml')."""
    texto = sem_acento(str(texto or "")).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.,]+", " ", texto)).strip()


def extrair_ml(*textos):
    """Maior volume citado em ml (kits já foram descartados antes)."""
    achados = []
    for texto in textos:
        for m in _RE_ML.finditer(texto_titulo(texto)):
            try:
                valor = float(m.group(1).replace(",", "."))
            except ValueError:
                continue
            if 1 <= valor <= 1000:
                achados.append(valor)
    return max(achados) if achados else None


def e_lixo(*textos):
    return any(_RE_LIXO.search(texto_titulo(t)) for t in textos if t)


@dataclass
class ItemCatalogo:
    id: str
    nome: str
    marca: str
    concentracao: str | None = None
    ml: list = field(default_factory=list)
    publico: str = "unissex"  # masc | fem | unissex
    categoria: str = "designer"
    preco_normal: float | None = None  # preço normal de mercado no ml_ref
    ml_ref: float | None = None
    preco_bom: float | None = None  # abaixo disso vale postar
    busca: list = field(default_factory=list)  # termos de busca nas lojas
    evitar: list = field(default_factory=list)  # termos que indicam outro produto

    @property
    def nome_exibicao(self):
        if self.concentracao and detectar_concentracao(self.nome) is None:
            return f"{self.nome} {self.concentracao}"
        return self.nome

    @property
    def termos_busca(self):
        return self.busca or [f"{self.nome} {self.marca}"]

    @property
    def rotulo_categoria(self):
        return CATEGORIAS.get(self.categoria, self.categoria)

    def _escala(self, valor, ml):
        """Preço de referência pra outro tamanho. Frasco maior sai mais barato por ml (expoente 0.8)."""
        if not valor or not self.ml_ref or not ml:
            return None
        return round(valor * (ml / self.ml_ref) ** 0.8, 2)

    def referencia_para(self, ml):
        return self._escala(self.preco_normal, ml)

    def preco_bom_para(self, ml):
        return self._escala(self.preco_bom, ml)


def _slug(texto):
    return re.sub(r"[^a-z0-9]+", "-", sem_acento(texto).lower()).strip("-")


def carregar_catalogo(caminho=ARQ_CATALOGO):
    if not Path(caminho).exists():
        return []
    with open(caminho, "rb") as f:
        bruto = tomllib.load(f)
    itens = []
    vistos = set()
    for p in bruto.get("perfumes", []):
        item = ItemCatalogo(
            id=p.get("id") or _slug(f"{p['marca']} {p['nome']} {p.get('concentracao') or ''}"),
            nome=p["nome"],
            marca=p["marca"],
            concentracao=p.get("concentracao") or None,
            ml=[float(x) for x in p.get("ml", [])],
            publico=p.get("publico", "unissex"),
            categoria=p.get("categoria", "designer"),
            preco_normal=p.get("preco_normal"),
            ml_ref=p.get("ml_ref") or (float(p["ml"][0]) if p.get("ml") else None),
            preco_bom=p.get("preco_bom"),
            busca=list(p.get("busca", [])),
            evitar=list(p.get("evitar", [])),
        )
        if item.id in vistos:
            raise ValueError(f"catalogo.toml: item repetido {item.id}")
        vistos.add(item.id)
        itens.append(item)
    return itens


# ============================================================
# CASAMENTO: o título da loja é esse item do catálogo?
# ============================================================

def _palavras_conc():
    from scentinel.busca import _CODIGOS

    return _CODIGOS


def _palavras(texto_norm):
    from scentinel.busca import _CODIGOS

    return [p for p in texto_norm.split() if p not in _CODIGOS]


def _tem_palavra(palavra, palavras_titulo):
    for p in palavras_titulo:
        if p == palavra:
            return True
        # tolera erro de digitação só em palavra grande (números e siglas têm que bater exato)
        if len(palavra) >= 5 and not palavra.isdigit() and fuzz.ratio(palavra, p) >= 88:
            return True
    return False


# Palavras de marca que não identificam nada sozinhas
_MARCA_GENERICA = {"parfums", "parfum", "perfumes", "paris", "maison", "fragrances", "cosmeticos", "beauty", "o", "al"}
# Abreviações comuns nos títulos
_MARCA_APELIDOS = {
    "yves saint laurent": ["ysl"],
    "jean paul gaultier": ["jpg"],
    "dolce gabbana": ["d g", "dg"],
    "maison francis kurkdjian": ["mfk"],
    "parfums marly": ["pdm"],
}


def _marca_presente(marca_norm, palavras, marca_loja=None):
    """A marca aparece se alguma palavra que a identifica estiver no título
    ('armani' basta pra Giorgio Armani, 'boticario' pra O Boticário), ou num apelido (YSL)."""
    if marca_loja:
        loja_norm = normalizar(marca_loja)
        if fuzz.ratio(loja_norm, marca_norm) >= 85 or (len(loja_norm) >= 4 and (loja_norm in marca_norm or marca_norm in loja_norm)):
            return True
    texto = " ".join(palavras)
    for apelido in _MARCA_APELIDOS.get(marca_norm, []):
        if _contem_frase(apelido, texto):
            return True
    fortes = [p for p in marca_norm.split() if len(p) >= 3 and p not in _MARCA_GENERICA]
    return any(_tem_palavra(p, palavras) for p in (fortes or marca_norm.split()))


def _contem_frase(frase_norm, texto_norm):
    return f" {frase_norm} " in f" {texto_norm} "


def casar(item, titulo, marca_loja=None, catalogo=None):
    """Devolve (ok, motivo). `catalogo` (lista completa) resolve ambiguidade de concentração."""
    texto = " ".join(t for t in (titulo, marca_loja) if t)
    norm = normalizar(texto)
    palavras = norm.split()

    if e_lixo(titulo):
        return False, "não é frasco (kit/decant/contratipo/corpo)"

    for termo in item.evitar:
        if _contem_frase(normalizar(termo), norm):
            return False, f"tem '{termo}'"

    # marca tem que aparecer (no título ou no campo marca da loja)
    marca_norm = normalizar(item.marca)
    if marca_norm and not _marca_presente(marca_norm, palavras, marca_loja):
        return False, "marca diferente"

    # todas as palavras do nome
    nome_norm = [p for p in _palavras(normalizar(item.nome)) if p not in marca_norm.split()]
    if not all(_tem_palavra(p, palavras) for p in nome_norm):
        return False, "nome não bate"

    # concentração
    conc_titulo = detectar_concentracao(titulo)
    if item.concentracao:
        if conc_titulo and conc_titulo != item.concentracao:
            return False, f"concentração {conc_titulo}"
        if not conc_titulo and catalogo:
            irmaos = {
                o.concentracao for o in catalogo
                if o.id != item.id and normalizar(o.marca) == marca_norm
                and _palavras(normalizar(o.nome)) == _palavras(normalizar(item.nome))
            }
            if irmaos - {item.concentracao}:
                return False, "concentração ambígua"

    # público
    t = texto_titulo(titulo)
    nome_txt = texto_titulo(item.nome)
    if item.publico == "masc" and _FEM.search(t) and not _FEM.search(nome_txt):
        return False, "feminino"
    if item.publico == "fem" and _MASC.search(t) and not _MASC.search(nome_txt):
        return False, "masculino"

    return True, "ok"


def identificar(titulo, catalogo, marca_loja=None, so_itens=None):
    """Acha o item do catálogo pro título. Se mais de um casar, fica o de nome mais específico."""
    candidatos = []
    for item in (so_itens or catalogo):
        ok, _ = casar(item, titulo, marca_loja, catalogo)
        if ok:
            candidatos.append(item)
    if not candidatos:
        return None
    return max(candidatos, key=lambda i: (len(_palavras(normalizar(i.nome))), bool(i.concentracao)))


# ============================================================
# QUALQUER PERFUME (varredura): sem depender do catálogo
# ============================================================

_RE_PERFUME = re.compile(
    r"\b(perfume|perfumes|fragrancia|eau de parfum|eau de toilette|eau de cologne|edp|edt|parfum|"
    r"extrait|elixir|colonia|deo parfum|deo colonia|cologne)\b"
)
_LIXO_NOME = re.compile(
    r"\b(perfume|perfumes|masculino|masculina|feminino|feminina|unissex|unisex|original|originais|importado|"
    r"lacrado|lacrada|novo|nova|spray|vaporizador|vapo|frasco|com caixa|selo adipec|selo|nota fiscal|"
    r"pronta entrega|envio imediato|promocao|oferta|frete gratis|100% original)\b",
    re.IGNORECASE,
)


def e_perfume(titulo):
    """O título é de um perfume (e não de creme, maquiagem, kit...)?"""
    return bool(_RE_PERFUME.search(texto_titulo(titulo))) and not e_lixo(titulo)


def nome_limpo(titulo, limite=70):
    """'Perfume Afnan 9PM Masculino EDP 100ml - Original' -> 'Afnan 9PM EDP'."""
    t = sem_acento(str(titulo or ""))
    t = re.sub(r"\d+([.,]\d+)?\s*ml\b", " ", t, flags=re.IGNORECASE)
    t = _LIXO_NOME.sub(" ", t)
    t = re.sub(r"[|/\\\-–—+()\[\]]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,")
    if not t:
        t = str(titulo or "").strip()
    return t[:limite].rstrip()


def chave_generica(titulo, ml, marca=None):
    """Chave pra reconhecer o mesmo produto em lojas diferentes sem catálogo:
    palavras do nome (normalizadas, sem as genéricas, em ordem) + público + tamanho.
    Título genérico demais (menos de 2 palavras que identificam) não ganha chave."""
    if not ml:
        return None
    from scentinel.busca import _CODIGOS

    texto = normalizar(nome_limpo(f"{titulo} {marca or ''}"))
    palavras = sorted(set(p for p in texto.split() if len(p) > 1 or p.isdigit()))
    distintivas = [p for p in palavras if p not in _CODIGOS]
    if len(distintivas) < 2:
        return None
    # Versão feminina vira outro produto ("212 VIP Feminino" != "212 VIP Masculino"). Título sem
    # gênero fica junto com o masculino: linha que existe nas duas versões sempre diz "Feminino".
    t = texto_titulo(titulo)
    publico = "f" if _FEM.search(t) and not _MASC.search(t) else "m"
    return f"t:{' '.join(palavras)}|{publico}|{ml:g}"
