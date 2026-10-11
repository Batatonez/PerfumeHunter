"""Normalização de nome + fuzzy match (usado no /locate e no autocomplete)."""

import re
import unicodedata

from rapidfuzz import fuzz

# Ordem importa: as frases longas vêm antes das curtas.
_CONCENTRACOES = (
    (r"\bextrait de parfum\b|\bextrait\b", "extrait", "Extrait"),
    (r"\beau de parfum\b|\bedp\b", "edp", "EDP"),
    (r"\beau de toilette\b|\bedt\b", "edt", "EDT"),
    (r"\beau de cologne\b|\bedc\b", "edc", "EDC"),
    (r"\bdeo colonia\b|\bcolonia\b", "colonia", "Colônia"),
    (r"\belixir\b", "elixir", "Elixir"),
    (r"\bparfum\b", "parfum", "Parfum"),
)

_PALAVRAS_INUTEIS = {
    "perfume", "perfumes", "masculino", "masculina", "feminino", "feminina",
    "unissex", "unisex", "de", "da", "do", "das", "dos", "e", "para", "com",
    "original", "importado", "lacrado", "frasco", "the", "le", "la",
}

LIMITE_MATCH = 80  # score mínimo (0-100)
JANELA_MATCH = 8  # só fica quem estiver até X pontos do melhor


def sem_acento(texto):
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalizar(texto):
    """'Bleu de Chanel Eau de Parfum 100ml' -> 'bleu chanel edp'."""
    texto = sem_acento(str(texto or "")).lower()
    texto = re.sub(r"\d+([.,]\d+)?\s*ml\b", " ", texto)
    texto = re.sub(r"[^a-z0-9 ]+", " ", texto)
    for regex, codigo, _ in _CONCENTRACOES:
        texto = re.sub(regex, f" {codigo} ", texto)
    palavras = [p for p in texto.split() if p not in _PALAVRAS_INUTEIS]
    return " ".join(palavras)


def detectar_concentracao(texto):
    """Devolve 'EDP', 'EDT', 'Elixir'... ou None."""
    texto = sem_acento(str(texto or "")).lower()
    texto = re.sub(r"[^a-z0-9 ]+", " ", texto)
    for regex, _, bonito in _CONCENTRACOES:
        if re.search(regex, texto):
            return bonito
    return None


def textos_do_perfume(perfume):
    """Tudo que pode bater numa busca: nome, marca + nome e apelidos."""
    nome = perfume["nome"]
    textos = [nome]
    if perfume.get("marca"):
        textos.append(f"{perfume['marca']} {nome}")
    for apelido in (perfume.get("apelidos") or "").split(";"):
        if apelido.strip():
            textos.append(apelido.strip())
    return textos


_CODIGOS = {codigo for _, codigo, _ in _CONCENTRACOES}


def _palavras(texto_norm):
    """Palavras do nome sem a concentração (ela é tratada à parte, como filtro)."""
    return [p for p in texto_norm.split() if p not in _CODIGOS]


def _nota_palavra(palavra, candidatas):
    melhor = 0.0
    for c in candidatas:
        if len(palavra) >= 3 and c.startswith(palavra):
            return 100.0  # prefixo: 'sauv' -> 'sauvage' (ajuda no autocomplete)
        melhor = max(melhor, fuzz.ratio(palavra, c))
    return melhor


def pontuar(palavras_consulta, perfume):
    """Média de quão bem cada palavra da busca aparece no perfume (0-100).
    Todas as palavras da busca precisam bater; palavras a mais no perfume não atrapalham."""
    melhor = 0.0
    for texto in textos_do_perfume(perfume):
        candidatas = _palavras(normalizar(texto))
        if not candidatas:
            continue
        notas = [_nota_palavra(p, candidatas) for p in palavras_consulta]
        melhor = max(melhor, sum(notas) / len(notas))
    return melhor


def buscar(consulta, perfumes, limite=LIMITE_MATCH, janela=JANELA_MATCH):
    """perfumes: lista de dicts com nome, marca, apelidos, concentracao.
    Devolve [(perfume, score)] do melhor pro pior."""
    consulta_norm = normalizar(consulta)
    if not consulta_norm:
        return []

    concentracao = detectar_concentracao(consulta)
    palavras = _palavras(consulta_norm)
    resultados = []
    for perfume in perfumes:
        if concentracao and perfume.get("concentracao") not in (None, "", concentracao):
            continue
        # busca só com a concentração ("edp") -> todos dessa concentração
        score = pontuar(palavras, perfume) if palavras else 100.0
        if score >= limite:
            resultados.append((perfume, score))

    if not resultados:
        return []
    resultados.sort(key=lambda r: (-r[1], r[0]["nome"]))
    melhor = resultados[0][1]
    return [r for r in resultados if r[1] >= melhor - janela]
