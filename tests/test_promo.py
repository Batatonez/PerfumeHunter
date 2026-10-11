import pytest

from cacador.promo import (
    FONTE_HISTORICO, FONTE_LOJA, FONTE_MENOR, Decisao, Estado, Nivel,
    avaliar, calcular_referencia, confirmou, decidir, desconto_pct,
    nivel_do_desconto, precisa_rechecar,
)

NIVEIS = [
    Nivel(1, "Cheirinho", "🟢", 10, 0x1),
    Nivel(2, "Elogio", "🟡", 20, 0x2),
    Nivel(3, "Rastro", "🟠", 30, 0x3),
    Nivel(4, "Fera", "🔴", 40, 0x4, ping=True),
    Nivel(5, "Bug de preço", "💎", 50, 0x5, ping=True, rechecar=True),
]
N = {n.nivel: n for n in NIVEIS}


# ============================================================
# referência
# ============================================================

def test_referencia_mediana_do_historico():
    assert calcular_referencia([100, 110, 90, 105, 95], None) == (100, FONTE_HISTORICO)


def test_referencia_poucas_leituras_usa_preco_de():
    assert calcular_referencia([100, 100], 150) == (150, FONTE_LOJA)


def test_referencia_usa_o_menor_entre_mediana_e_preco_de():
    # "Black Fraude": a loja infla o "de" pra 300, mas o histórico diz 100
    assert calcular_referencia([100] * 5, 300) == (100, FONTE_MENOR)
    assert calcular_referencia([100] * 5, 90) == (90, FONTE_MENOR)


def test_referencia_sem_nada():
    assert calcular_referencia([100, 100], None) == (None, None)


def test_min_leituras_configuravel():
    assert calcular_referencia([100, 100], None, min_leituras=2) == (100, FONTE_HISTORICO)


# ============================================================
# níveis
# ============================================================

@pytest.mark.parametrize(
    "pct, esperado",
    [(0, None), (9.99, None), (10, 1), (19.9, 1), (20, 2), (34, 3), (40, 4), (49.9, 4), (50, 5), (90, 5)],
)
def test_nivel_do_desconto(pct, esperado):
    nivel = nivel_do_desconto(pct, NIVEIS)
    assert (nivel.nivel if nivel else None) == esperado


def test_desconto_pct():
    assert desconto_pct(659.9, 999.0) == pytest.approx(33.94, abs=0.01)
    assert desconto_pct(100, None) == 0


def test_avaliar_exemplo_do_handoff():
    av = avaliar(659.90, None, [999.0] * 6, NIVEIS)
    assert av.referencia == 999.0
    assert av.nivel.nome == "Rastro"


def test_avaliar_sem_referencia_nao_tem_nivel():
    av = avaliar(100, None, [], NIVEIS)
    assert av.nivel is None and av.referencia is None


# ============================================================
# anti-spam
# ============================================================

def test_primeiro_aviso():
    d = decidir(800, False, N[1], None, Estado())
    assert d.nivel == N[1]
    assert d.novo_estado.nivel_avisado == 1


def test_mesmo_nivel_nao_repete():
    d = decidir(800, False, N[1], None, Estado(nivel_avisado=1))
    assert not d.avisar
    assert d.novo_estado.nivel_avisado == 1


def test_sobe_de_nivel_avisa():
    d = decidir(600, False, N[3], None, Estado(nivel_avisado=1))
    assert d.nivel == N[3]
    assert d.novo_estado.nivel_avisado == 3


def test_desce_de_nivel_nao_avisa_e_mantem_estado():
    d = decidir(800, False, N[1], None, Estado(nivel_avisado=3))
    assert not d.avisar
    assert d.novo_estado.nivel_avisado == 3


def test_reset_quando_sai_da_promo():
    d = decidir(1000, False, None, None, Estado(nivel_avisado=3))
    assert not d.avisar
    assert d.novo_estado.nivel_avisado == 0
    # e a próxima queda avisa de novo
    d2 = decidir(800, False, N[1], None, d.novo_estado)
    assert d2.nivel == N[1]


def test_nivel_minimo():
    d = decidir(800, False, N[1], None, Estado(), nivel_minimo=2)
    assert not d.avisar
    assert d.novo_estado.nivel_avisado == 0
    d2 = decidir(700, False, N[2], None, d.novo_estado, nivel_minimo=2)
    assert d2.nivel == N[2]


def test_esgotado_nao_avisa_e_nao_mexe_no_estado():
    d = decidir(100, True, N[5], 200, Estado(nivel_avisado=1))
    assert not d.avisar
    assert d.novo_estado == Estado(nivel_avisado=1, alvo_avisado=False)


def test_alvo_avisa_uma_vez_e_libera_quando_volta():
    d = decidir(490, False, None, 500, Estado())
    assert d.alvo and d.avisar
    d = decidir(480, False, None, 500, d.novo_estado)
    assert not d.alvo
    d = decidir(520, False, None, 500, d.novo_estado)
    assert not d.alvo and not d.novo_estado.alvo_avisado
    d = decidir(499, False, None, 500, d.novo_estado)
    assert d.alvo


def test_alvo_e_nivel_juntos():
    d = decidir(490, False, N[2], 500, Estado())
    assert d.alvo and d.nivel == N[2]


# ============================================================
# sanidade
# ============================================================

def test_precisa_rechecar():
    assert precisa_rechecar(Decisao(nivel=N[4]), 65, queda_pct=60)
    assert not precisa_rechecar(Decisao(nivel=N[4]), 45, queda_pct=60)
    assert precisa_rechecar(Decisao(nivel=N[5]), 52, queda_pct=60)  # nível com rechecar=true
    assert not precisa_rechecar(Decisao(), 80)  # não ia avisar mesmo


def test_confirmou():
    assert confirmou(100, 100)
    assert confirmou(100, 101.5, tolerancia_pct=2)
    assert confirmou(100, 90)
    assert not confirmou(100, 300)
    assert not confirmou(100, None)
