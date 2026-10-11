"""SQLite. Cria as tabelas sozinho no primeiro start.

Datas são guardadas em UTC, como texto ISO ('2026-10-11T15:00:00+00:00'),
o que deixa comparar por ordem alfabética.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS perfumes (
    id            INTEGER PRIMARY KEY,
    nome          TEXT NOT NULL,
    nome_norm     TEXT NOT NULL UNIQUE,
    marca         TEXT,
    concentracao  TEXT,
    apelidos      TEXT NOT NULL DEFAULT '',
    criado_em     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anuncios (
    id               INTEGER PRIMARY KEY,
    perfume_id       INTEGER NOT NULL REFERENCES perfumes(id) ON DELETE CASCADE,
    loja             TEXT NOT NULL,
    url              TEXT NOT NULL UNIQUE,
    ml               REAL,
    seletor          TEXT,
    alvo             REAL,
    ativo            INTEGER NOT NULL DEFAULT 1,
    bloqueado        INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate    TEXT,
    falhas_bloqueio  INTEGER NOT NULL DEFAULT 0,
    nivel_avisado    INTEGER NOT NULL DEFAULT 0,
    alvo_avisado     INTEGER NOT NULL DEFAULT 0,
    ultima_checagem  TEXT,
    ultimo_erro      TEXT,
    criado_em        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precos (
    id          INTEGER PRIMARY KEY,
    anuncio_id  INTEGER NOT NULL REFERENCES anuncios(id) ON DELETE CASCADE,
    quando      TEXT NOT NULL,
    preco       REAL NOT NULL,
    preco_de    REAL,
    preco_pix   REAL,
    esgotado    INTEGER NOT NULL DEFAULT 0,
    metodo      TEXT
);
CREATE INDEX IF NOT EXISTS idx_precos_anuncio ON precos(anuncio_id, quando);

CREATE TABLE IF NOT EXISTS alertas (
    id          INTEGER PRIMARY KEY,
    anuncio_id  INTEGER NOT NULL REFERENCES anuncios(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL,          -- 'nivel' | 'alvo' | 'nivel+alvo'
    nivel       INTEGER,
    preco       REAL NOT NULL,
    quando      TEXT NOT NULL
);
"""


def agora_utc():
    return datetime.now(UTC).replace(microsecond=0)


def para_texto(momento):
    return momento.astimezone(UTC).isoformat(timespec="seconds")


def de_texto(texto):
    return datetime.fromisoformat(texto) if texto else None


def conectar(caminho):
    caminho = Path(caminho)
    if str(caminho) != ":memory:":
        caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(ESQUEMA)
    con.commit()
    return con


def _dict(linha):
    return dict(linha) if linha is not None else None


# ============================================================
# PERFUMES
# ============================================================

def perfume_por_nome_norm(con, nome_norm):
    return _dict(con.execute("SELECT * FROM perfumes WHERE nome_norm = ?", (nome_norm,)).fetchone())


def perfume_por_id(con, perfume_id):
    return _dict(con.execute("SELECT * FROM perfumes WHERE id = ?", (perfume_id,)).fetchone())


def criar_perfume(con, nome, nome_norm, marca=None, concentracao=None, apelidos=""):
    cur = con.execute(
        "INSERT INTO perfumes (nome, nome_norm, marca, concentracao, apelidos, criado_em)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (nome, nome_norm, marca, concentracao, apelidos, para_texto(agora_utc())),
    )
    con.commit()
    return perfume_por_id(con, cur.lastrowid)


def listar_perfumes(con):
    return [_dict(l) for l in con.execute("SELECT * FROM perfumes ORDER BY nome COLLATE NOCASE")]


def remover_perfume_se_vazio(con, perfume_id):
    resto = con.execute("SELECT COUNT(*) FROM anuncios WHERE perfume_id = ?", (perfume_id,)).fetchone()[0]
    if resto == 0:
        con.execute("DELETE FROM perfumes WHERE id = ?", (perfume_id,))
        con.commit()
        return True
    return False


# ============================================================
# ANÚNCIOS
# ============================================================

_SELECT_ANUNCIO = """
SELECT a.*, p.nome AS perfume_nome, p.concentracao AS perfume_concentracao
FROM anuncios a JOIN perfumes p ON p.id = a.perfume_id
"""


def anuncio_por_id(con, anuncio_id):
    return _dict(con.execute(_SELECT_ANUNCIO + " WHERE a.id = ?", (anuncio_id,)).fetchone())


def anuncio_por_url(con, url):
    return _dict(con.execute(_SELECT_ANUNCIO + " WHERE a.url = ?", (url,)).fetchone())


def criar_anuncio(con, perfume_id, loja, url, ml=None, seletor=None, alvo=None):
    cur = con.execute(
        "INSERT INTO anuncios (perfume_id, loja, url, ml, seletor, alvo, criado_em)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (perfume_id, loja, url, ml, seletor, alvo, para_texto(agora_utc())),
    )
    con.commit()
    return anuncio_por_id(con, cur.lastrowid)


def remover_anuncio(con, anuncio_id):
    anuncio = anuncio_por_id(con, anuncio_id)
    if not anuncio:
        return None
    con.execute("DELETE FROM anuncios WHERE id = ?", (anuncio_id,))
    con.commit()
    remover_perfume_se_vazio(con, anuncio["perfume_id"])
    return anuncio


def listar_anuncios(con, perfume_id=None, so_ativos=False):
    sql = _SELECT_ANUNCIO + " WHERE 1=1"
    args = []
    if perfume_id is not None:
        sql += " AND a.perfume_id = ?"
        args.append(perfume_id)
    if so_ativos:
        sql += " AND a.ativo = 1"
    sql += " ORDER BY p.nome COLLATE NOCASE, a.loja"
    return [_dict(l) for l in con.execute(sql, args)]


def anuncios_para_checar(con, agora, intervalo):
    """Ativos, fora do backoff e sem checagem há pelo menos `intervalo`."""
    limite = para_texto(agora - intervalo)
    agora_txt = para_texto(agora)
    sql = (
        _SELECT_ANUNCIO
        + " WHERE a.ativo = 1"
        " AND (a.bloqueado_ate IS NULL OR a.bloqueado_ate <= ?)"
        " AND (a.ultima_checagem IS NULL OR a.ultima_checagem <= ?)"
        " ORDER BY a.ultima_checagem IS NOT NULL, a.ultima_checagem"
    )
    return [_dict(l) for l in con.execute(sql, (agora_txt, limite))]


def marcar_checado(con, anuncio_id, erro=None, quando=None):
    con.execute(
        "UPDATE anuncios SET ultima_checagem = ?, ultimo_erro = ? WHERE id = ?",
        (para_texto(quando or agora_utc()), erro, anuncio_id),
    )
    con.commit()


def marcar_bloqueado(con, anuncio_id, backoff_horas, backoff_max_horas, quando=None):
    """Marca como bloqueado e aplica backoff dobrando a cada falha.
    Devolve True se é o PRIMEIRO bloqueio (aí avisa no canal)."""
    quando = quando or agora_utc()
    anuncio = anuncio_por_id(con, anuncio_id)
    falhas = anuncio["falhas_bloqueio"] + 1
    espera = min(backoff_horas * 2 ** (falhas - 1), backoff_max_horas)
    con.execute(
        "UPDATE anuncios SET bloqueado = 1, falhas_bloqueio = ?, bloqueado_ate = ?,"
        " ultima_checagem = ?, ultimo_erro = 'bloqueado' WHERE id = ?",
        (falhas, para_texto(quando + timedelta(hours=espera)), para_texto(quando), anuncio_id),
    )
    con.commit()
    return not anuncio["bloqueado"]


def limpar_bloqueio(con, anuncio_id):
    con.execute(
        "UPDATE anuncios SET bloqueado = 0, falhas_bloqueio = 0, bloqueado_ate = NULL WHERE id = ?",
        (anuncio_id,),
    )
    con.commit()


def salvar_estado(con, anuncio_id, nivel_avisado, alvo_avisado):
    con.execute(
        "UPDATE anuncios SET nivel_avisado = ?, alvo_avisado = ? WHERE id = ?",
        (nivel_avisado, int(alvo_avisado), anuncio_id),
    )
    con.commit()


# ============================================================
# PREÇOS
# ============================================================

def registrar_preco(con, anuncio_id, leitura, quando=None):
    con.execute(
        "INSERT INTO precos (anuncio_id, quando, preco, preco_de, preco_pix, esgotado, metodo)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            anuncio_id,
            para_texto(quando or agora_utc()),
            leitura.preco,
            leitura.preco_de,
            leitura.preco_pix,
            int(leitura.esgotado),
            leitura.metodo,
        ),
    )
    con.commit()


def precos_validos(con, anuncio_id, desde, ate=None):
    """Preços sem esgotado no intervalo [desde, ate)."""
    sql = "SELECT preco FROM precos WHERE anuncio_id = ? AND esgotado = 0 AND quando >= ?"
    args = [anuncio_id, para_texto(desde)]
    if ate is not None:
        sql += " AND quando < ?"
        args.append(para_texto(ate))
    return [l[0] for l in con.execute(sql, args)]


def ultima_leitura(con, anuncio_id):
    return _dict(
        con.execute(
            "SELECT * FROM precos WHERE anuncio_id = ? ORDER BY quando DESC, id DESC LIMIT 1",
            (anuncio_id,),
        ).fetchone()
    )


def menor_preco(con, anuncio_id):
    return con.execute(
        "SELECT MIN(preco) FROM precos WHERE anuncio_id = ? AND esgotado = 0", (anuncio_id,)
    ).fetchone()[0]


def estatisticas(con, anuncio_id):
    linha = con.execute(
        "SELECT MIN(preco), MAX(preco), COUNT(*) FROM precos WHERE anuncio_id = ? AND esgotado = 0",
        (anuncio_id,),
    ).fetchone()
    return {"min": linha[0], "max": linha[1], "leituras": linha[2]}


# ============================================================
# ALERTAS
# ============================================================

def registrar_alerta(con, anuncio_id, tipo, nivel, preco, quando=None):
    con.execute(
        "INSERT INTO alertas (anuncio_id, tipo, nivel, preco, quando) VALUES (?, ?, ?, ?, ?)",
        (anuncio_id, tipo, nivel, preco, para_texto(quando or agora_utc())),
    )
    con.commit()
