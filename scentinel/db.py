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

CREATE TABLE IF NOT EXISTS buscas (
    item_id     TEXT NOT NULL,          -- id do item no catalogo.toml
    fonte       TEXT NOT NULL,          -- id da fonte (ex.: 'vtex:www.epocacosmeticos.com.br')
    quando      TEXT NOT NULL,
    erro        TEXT,
    achados     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, fonte)
);

CREATE TABLE IF NOT EXISTS alertas (
    id          INTEGER PRIMARY KEY,
    anuncio_id  INTEGER NOT NULL REFERENCES anuncios(id) ON DELETE CASCADE,
    tipo        TEXT NOT NULL,          -- 'nivel' | 'alvo' | 'nivel+alvo'
    nivel       INTEGER,
    preco       REAL NOT NULL,
    quando      TEXT NOT NULL
);

-- /alerta: "me marca quando X ficar abaixo de R$ Y"
CREATE TABLE IF NOT EXISTS alertas_usuario (
    id             INTEGER PRIMARY KEY,
    usuario_id     INTEGER NOT NULL,
    consulta       TEXT NOT NULL,
    preco_max      REAL NOT NULL,
    ml             REAL,
    criado_em      TEXT NOT NULL,
    ultima_busca   TEXT,
    avisado_preco  REAL,                -- preço do último aviso; NULL = a próxima vez que chegar avisa
    acima_seguidas INTEGER NOT NULL DEFAULT 0  -- pesquisas seguidas com o preço acima do pedido
);
CREATE INDEX IF NOT EXISTS idx_alertas_usuario ON alertas_usuario(usuario_id);
"""


def agora_utc():
    return datetime.now(UTC).replace(microsecond=0)


def para_texto(momento):
    return momento.astimezone(UTC).isoformat(timespec="seconds")


def de_texto(texto):
    return datetime.fromisoformat(texto) if texto else None


# Colunas adicionadas depois da primeira versão: bancos antigos ganham elas no start.
MIGRACOES = {
    "anuncios": {
        "origem": "TEXT NOT NULL DEFAULT 'manual'",  # 'manual' (/add) ou 'descoberta' (busca automática)
        "catalogo_id": "TEXT",                       # item do catalogo.toml que esse anúncio representa
        "fonte": "TEXT",                             # de onde veio (ex.: 'vtex:epocacosmeticos.com.br')
        "titulo": "TEXT",                            # título do produto na loja
        "oficial": "INTEGER NOT NULL DEFAULT 0",     # loja oficial / vendido pela própria loja
        "imagem": "TEXT",                            # foto do produto
    },
    "alertas": {
        "pct": "REAL",                               # % OFF na hora do post (pro 🏆 Top da semana)
    },
    "alertas_usuario": {
        "acima_seguidas": "INTEGER NOT NULL DEFAULT 0",
    },
}


def migrar(con):
    for tabela, colunas in MIGRACOES.items():
        existentes = {linha[1] for linha in con.execute(f"PRAGMA table_info({tabela})")}
        for coluna, tipo in colunas.items():
            if coluna not in existentes:
                con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_anuncios_catalogo ON anuncios(catalogo_id)")
    con.commit()


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
    migrar(con)
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
SELECT a.*, p.nome AS perfume_nome, p.concentracao AS perfume_concentracao, p.marca AS perfume_marca
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
        + " WHERE a.ativo = 1 AND a.origem = 'manual'"
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

def registrar_alerta(con, anuncio_id, tipo, nivel, preco, quando=None, pct=None):
    con.execute(
        "INSERT INTO alertas (anuncio_id, tipo, nivel, preco, quando, pct) VALUES (?, ?, ?, ?, ?, ?)",
        (anuncio_id, tipo, nivel, preco, para_texto(quando or agora_utc()), pct),
    )
    con.commit()


def posts_com_desconto_desde(con, desde, pct_min=1):
    """Posts do canal (promo e vitrine) com % OFF desde `desde`, do maior desconto pro menor."""
    sql = (
        "SELECT al.anuncio_id, al.pct, al.preco, al.quando, a.catalogo_id, a.perfume_id FROM alertas al"
        " JOIN anuncios a ON a.id = al.anuncio_id WHERE al.quando >= ? AND al.pct >= ?"
        " ORDER BY al.pct DESC, al.preco"
    )
    return [_dict(l) for l in con.execute(sql, (para_texto(desde), pct_min))]


# ============================================================
# BUSCA AUTOMÁTICA
# ============================================================

def ultima_busca(con, item_id, fonte):
    linha = con.execute("SELECT quando FROM buscas WHERE item_id = ? AND fonte = ?", (item_id, fonte)).fetchone()
    return de_texto(linha[0]) if linha else None


def registrar_busca(con, item_id, fonte, quando, erro=None, achados=0):
    con.execute(
        "INSERT INTO buscas (item_id, fonte, quando, erro, achados) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(item_id, fonte) DO UPDATE SET quando = excluded.quando, erro = excluded.erro,"
        " achados = excluded.achados",
        (item_id, fonte, para_texto(quando), erro, achados),
    )
    con.commit()


def salvar_anuncio_descoberto(con, perfume_id, loja, url, ml, alvo, catalogo_id, fonte, titulo, oficial, imagem=None):
    """Cria ou atualiza (pela URL) um anúncio achado pela busca automática."""
    existente = anuncio_por_url(con, url)
    if existente and (existente.get("origem") or "manual") == "manual":
        return None  # cadastrado com /add: a busca automática não mexe
    if existente:
        con.execute(
            "UPDATE anuncios SET perfume_id = ?, loja = ?, ml = ?, alvo = ?, catalogo_id = ?, fonte = ?,"
            " titulo = ?, oficial = ?, imagem = COALESCE(?, imagem), ativo = 1 WHERE id = ?",
            (perfume_id, loja, ml, alvo, catalogo_id, fonte, titulo, int(oficial), imagem, existente["id"]),
        )
        con.commit()
        return anuncio_por_id(con, existente["id"])
    cur = con.execute(
        "INSERT INTO anuncios (perfume_id, loja, url, ml, alvo, criado_em, origem, catalogo_id, fonte, titulo, oficial, imagem)"
        " VALUES (?, ?, ?, ?, ?, ?, 'descoberta', ?, ?, ?, ?, ?)",
        (perfume_id, loja, url, ml, alvo, para_texto(agora_utc()), catalogo_id, fonte, titulo, int(oficial), imagem),
    )
    con.commit()
    return anuncio_por_id(con, cur.lastrowid)


def ofertas_recentes_do_item(con, catalogo_id, ml, desde, exceto_id=None, so_oficial=False):
    """Último preço (sem esgotado) de cada anúncio do mesmo item e tamanho, lido desde `desde`."""
    sql = """
    SELECT a.id, a.loja, a.oficial, p.preco, p.quando FROM anuncios a
    JOIN precos p ON p.id = (SELECT id FROM precos WHERE anuncio_id = a.id ORDER BY quando DESC, id DESC LIMIT 1)
    WHERE a.catalogo_id = ? AND a.ml = ? AND a.ativo = 1 AND p.esgotado = 0 AND p.quando >= ?
    """
    if so_oficial:
        sql += " AND a.oficial = 1"
    args = [catalogo_id, ml, para_texto(desde)]
    if exceto_id is not None:
        sql += " AND a.id != ?"
        args.append(exceto_id)
    return [_dict(l) for l in con.execute(sql, args)]


def anuncios_descobertos_recentes(con, desde):
    """Anúncios da busca automática com leitura desde `desde`."""
    sql = (
        _SELECT_ANUNCIO
        + " WHERE a.origem = 'descoberta' AND a.ativo = 1"
        " AND EXISTS (SELECT 1 FROM precos WHERE anuncio_id = a.id AND quando >= ?)"
    )
    return [_dict(l) for l in con.execute(sql, (para_texto(desde),))]


def ultimos_posts_desde(con, desde):
    """Anúncios que foram pro canal desde `desde`, com o último horário e o perfume (pra vitrine não repetir)."""
    sql = (
        "SELECT al.anuncio_id, MAX(al.quando) AS quando, a.perfume_id, a.catalogo_id "
        "FROM alertas al JOIN anuncios a ON a.id = al.anuncio_id WHERE al.quando >= ? GROUP BY al.anuncio_id"
    )
    return [_dict(l) for l in con.execute(sql, (para_texto(desde),))]


def ultimo_post(con):
    linha = con.execute("SELECT MAX(quando) FROM alertas").fetchone()
    return de_texto(linha[0]) if linha and linha[0] else None


def alertas_desde(con, desde):
    return con.execute("SELECT COUNT(*) FROM alertas WHERE quando >= ?", (para_texto(desde),)).fetchone()[0]


def menores_por_loja(con, catalogo_id, ml, desde, exceto_id=None, exceto_loja=None, so_oficial=True):
    """Menor preço atual de cada OUTRA loja pro mesmo produto e tamanho (sem esgotado, desde `desde`).
    Vários vendedores da mesma loja contam como uma loja só."""
    linhas = ofertas_recentes_do_item(con, catalogo_id, ml, desde, exceto_id=exceto_id, so_oficial=so_oficial)
    menores = {}
    for o in linhas:
        if exceto_loja and o["loja"] == exceto_loja:
            continue
        if o["loja"] not in menores or o["preco"] < menores[o["loja"]]:
            menores[o["loja"]] = o["preco"]
    return menores


def ultimo_alerta(con, anuncio_id, desde):
    return _dict(con.execute(
        "SELECT * FROM alertas WHERE anuncio_id = ? AND quando >= ? ORDER BY quando DESC, id DESC LIMIT 1",
        (anuncio_id, para_texto(desde)),
    ).fetchone())


def alertas_de_nivel_desde(con, nivel_minimo, desde):
    return con.execute(
        "SELECT COUNT(*) FROM alertas WHERE nivel >= ? AND quando >= ?", (nivel_minimo, para_texto(desde))
    ).fetchone()[0]


# ============================================================
# /alerta (alerta de preço de cada pessoa)
# ============================================================

def criar_alerta_usuario(con, usuario_id, consulta, preco_max, ml=None, avisado_preco=None, quando=None,
                         buscado=False):
    """buscado=True: acabou de pesquisar (na criação), a próxima pesquisa fica pra daqui ~3 h."""
    quando = para_texto(quando or agora_utc())
    cur = con.execute(
        "INSERT INTO alertas_usuario (usuario_id, consulta, preco_max, ml, criado_em, ultima_busca, avisado_preco)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (usuario_id, consulta, preco_max, ml, quando, quando if buscado else None, avisado_preco),
    )
    con.commit()
    return alerta_usuario(con, cur.lastrowid)


def alerta_usuario(con, alerta_id):
    return _dict(con.execute("SELECT * FROM alertas_usuario WHERE id = ?", (alerta_id,)).fetchone())


def alertas_do_usuario(con, usuario_id):
    return [_dict(l) for l in con.execute(
        "SELECT * FROM alertas_usuario WHERE usuario_id = ? ORDER BY id", (usuario_id,))]


def contar_alertas_usuario(con):
    return con.execute("SELECT COUNT(*) FROM alertas_usuario").fetchone()[0]


def remover_alerta_usuario(con, alerta_id, usuario_id):
    """Só remove o alerta da própria pessoa. Devolve o alerta removido (ou None)."""
    alerta = alerta_usuario(con, alerta_id)
    if not alerta or alerta["usuario_id"] != usuario_id:
        return None
    con.execute("DELETE FROM alertas_usuario WHERE id = ?", (alerta_id,))
    con.commit()
    return alerta


def alertas_usuario_vencidos(con, antes_de, limite):
    """Os que não foram pesquisados desde `antes_de`, os mais atrasados primeiro."""
    return [_dict(l) for l in con.execute(
        "SELECT * FROM alertas_usuario WHERE ultima_busca IS NULL OR ultima_busca <= ?"
        " ORDER BY ultima_busca IS NOT NULL, ultima_busca, id LIMIT ?",
        (para_texto(antes_de), limite),
    )]


def marcar_alerta_usuario(con, alerta_id, quando, avisado_preco, acima_seguidas=0):
    con.execute("UPDATE alertas_usuario SET ultima_busca = ?, avisado_preco = ?, acima_seguidas = ? WHERE id = ?",
                (para_texto(quando), avisado_preco, acima_seguidas, alerta_id))
    con.commit()
