"""Preço de referência, níveis de promo e regras de aviso (anti-spam).

Tudo aqui é função pura: não mexe em banco, rede nem Discord.
"""

import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Nivel:
    nivel: int
    nome: str
    emoji: str
    min_pct: float
    cor: int
    ping: bool = False  # marca o dono
    rechecar: bool = False
    everyone: bool = False  # marca @everyone (promo muito boa)


# ============================================================
# REFERÊNCIA (o "100%" do desconto)
# ============================================================

FONTE_HISTORICO = "historico"  # mediana do próprio histórico
FONTE_LOJA = "loja"  # "preço de" riscado da página (sem histórico ainda)
FONTE_MENOR = "menor"  # tinha mais de uma, ficou com a menor
FONTE_CATALOGO = "catalogo"  # preço normal de mercado (catalogo.toml), sem histórico ainda


def calcular_referencia(historico, preco_de, min_leituras=5, ref_catalogo=None):
    """historico: preços (sem esgotados) dos últimos N dias, ANTES da leitura atual.
    ref_catalogo: preço normal de mercado pro tamanho (só anúncios da busca automática).
    Sempre fica com a MENOR referência disponível (proteção contra "Black Fraude").
    Devolve (referencia, fonte) ou (None, None)."""
    mediana = statistics.median(historico) if len(historico) >= min_leituras else None
    preco_de = preco_de if preco_de and preco_de > 0 else None
    ref_catalogo = ref_catalogo if ref_catalogo and ref_catalogo > 0 else None

    if mediana:
        candidatas = [mediana] + [r for r in (preco_de, ref_catalogo) if r]
        if len(candidatas) == 1:
            return mediana, FONTE_HISTORICO
        return min(candidatas), FONTE_MENOR
    if preco_de and ref_catalogo:
        return (preco_de, FONTE_LOJA) if preco_de < ref_catalogo else (ref_catalogo, FONTE_CATALOGO)
    if preco_de:
        return preco_de, FONTE_LOJA
    if ref_catalogo:
        return ref_catalogo, FONTE_CATALOGO
    return None, None


def desconto_pct(preco, referencia):
    if not referencia or referencia <= 0:
        return 0.0
    return (referencia - preco) / referencia * 100


def nivel_do_desconto(pct, niveis):
    """Maior nível cujo mínimo foi atingido. None se não chegou no nível 1."""
    atingido = None
    for nivel in sorted(niveis, key=lambda n: n.min_pct):
        if pct >= nivel.min_pct:
            atingido = nivel
    return atingido


@dataclass
class Avaliacao:
    preco: float
    referencia: float | None
    fonte: str | None
    pct: float
    nivel: Nivel | None


def avaliar(preco, preco_de, historico, niveis, min_leituras=5, ref_catalogo=None):
    referencia, fonte = calcular_referencia(historico, preco_de, min_leituras, ref_catalogo)
    pct = desconto_pct(preco, referencia)
    return Avaliacao(preco, referencia, fonte, pct, nivel_do_desconto(pct, niveis))


# ============================================================
# ANTI-SPAM
# ============================================================

@dataclass
class Estado:
    nivel_avisado: int = 0  # 0 = nenhum aviso de nível ativo
    alvo_avisado: bool = False


@dataclass
class Decisao:
    nivel: Nivel | None = None  # nível a avisar (None = não avisa nível)
    alvo: bool = False  # avisa "bateu seu alvo"
    novo_estado: Estado = field(default_factory=Estado)

    @property
    def avisar(self):
        return self.nivel is not None or self.alvo


def decidir(preco, esgotado, nivel, alvo, estado, nivel_minimo=1):
    """Máquina de estado do aviso.
    - sobe de nível em relação ao último aviso -> avisa
    - mesmo nível ou menor -> não repete
    - desconto abaixo do nível 1 -> zera, a próxima queda avisa de novo
    - alvo: avisa uma vez quando preço <= alvo, libera quando volta acima
    - esgotado: não avisa e não mexe no estado
    """
    if esgotado:
        return Decisao(novo_estado=Estado(estado.nivel_avisado, estado.alvo_avisado))

    novo = Estado(estado.nivel_avisado, estado.alvo_avisado)
    decisao = Decisao(novo_estado=novo)

    numero = nivel.nivel if nivel else 0
    if numero == 0:
        novo.nivel_avisado = 0
    elif numero > estado.nivel_avisado and numero >= nivel_minimo:
        decisao.nivel = nivel
        novo.nivel_avisado = numero

    if alvo:
        if preco <= alvo:
            if not estado.alvo_avisado:
                decisao.alvo = True
                novo.alvo_avisado = True
        else:
            novo.alvo_avisado = False
    else:
        novo.alvo_avisado = False

    return decisao


MENCAO_DONO = "dono"
MENCAO_EVERYONE = "everyone"


def mencao(nivel):
    """Quem o alerta marca: None, MENCAO_DONO ou MENCAO_EVERYONE (este vence)."""
    if not nivel:
        return None
    if nivel.everyone:
        return MENCAO_EVERYONE
    if nivel.ping:
        return MENCAO_DONO
    return None


# ============================================================
# SANIDADE
# ============================================================

def precisa_rechecar(decisao, pct, queda_pct=60):
    """Queda grande demais (ou nível marcado com rechecar) -> baixa de novo antes de avisar."""
    if not decisao.avisar:
        return False
    if pct >= queda_pct:
        return True
    return bool(decisao.nivel and decisao.nivel.rechecar)


def confirmou(preco1, preco2, tolerancia_pct=2):
    """A segunda leitura confirma a primeira se estiver perto (ou ainda menor)."""
    if not preco2:
        return False
    return preco2 <= preco1 * (1 + tolerancia_pct / 100)


# ============================================================
# RANKING (/locate)
# ============================================================

def chave_ranking(pct, preco):
    """Maior desconto primeiro; empate -> menor preço."""
    return (-round(pct, 2), preco)


# ============================================================
# OFERTA (tudo que um alerta / o /locate precisa mostrar)
# ============================================================

@dataclass
class Oferta:
    anuncio_id: int
    perfume: str
    loja: str
    url: str
    ml: float | None
    preco: float
    avaliacao: Avaliacao
    preco_pix: float | None = None
    esgotado: bool = False
    menor_ja_visto: float | None = None
    recorde: bool = False
    alvo: float | None = None
    bateu_alvo: bool = False
    quando: object = None  # datetime UTC da leitura
    origem: str = "manual"  # 'manual' (/add) ou 'descoberta' (busca automática)
    categoria: str | None = None  # rótulo do catálogo, ex.: '🕌 Árabe'
    oficial: bool = False  # loja oficial / vendido pela própria loja

    @property
    def por_ml(self):
        return self.preco / self.ml if self.ml else None

    @property
    def titulo(self):
        if self.ml:
            return f"{self.perfume} {self.ml:g}ml"
        return self.perfume
