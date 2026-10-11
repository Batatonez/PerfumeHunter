import pytest

from scentinel.busca import buscar, detectar_concentracao, normalizar


def perfume(nome, marca=None, apelidos="", id_=None):
    return {
        "id": id_ or abs(hash(nome)),
        "nome": nome,
        "marca": marca,
        "apelidos": apelidos,
        "concentracao": detectar_concentracao(nome),
    }


CATALOGO = [
    perfume("Bleu de Chanel Eau de Parfum", "Chanel"),
    perfume("Sauvage EDP", "Dior"),
    perfume("Sauvage EDT", "Dior"),
    perfume("Sauvage Elixir", "Dior"),
    perfume("Malbec", "O Boticário"),
    perfume("Kaiak Clássico", "Natura", apelidos="kaiak"),
    perfume("Club de Nuit Intense Man", "Armaf", apelidos="cdnim"),
]


def nomes(resultados):
    return [p["nome"] for p, _ in resultados]


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("Bleu de Chanel Eau de Parfum 100ml", "bleu chanel edp"),
        ("Perfume Sauvage Masculino EDT", "sauvage edt"),
        ("Kaiak Clássico", "kaiak classico"),
        ("Extrait de Parfum", "extrait"),
    ],
)
def test_normalizar(entrada, esperado):
    assert normalizar(entrada) == esperado


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("Sauvage Eau de Toilette", "EDT"),
        ("sauvage edp", "EDP"),
        ("Sauvage Elixir", "Elixir"),
        ("Malbec Deo Colônia", "Colônia"),
        ("Bleu de Chanel Parfum", "Parfum"),
        ("Malbec", None),
    ],
)
def test_detectar_concentracao(entrada, esperado):
    assert detectar_concentracao(entrada) == esperado


def test_bleu_edp_acha_bleu_de_chanel():
    assert nomes(buscar("bleu edp", CATALOGO)) == ["Bleu de Chanel Eau de Parfum"]


def test_sauvage_acha_as_tres_variacoes():
    assert set(nomes(buscar("sauvage", CATALOGO))) == {"Sauvage EDP", "Sauvage EDT", "Sauvage Elixir"}


def test_concentracao_na_busca_filtra():
    assert nomes(buscar("sauvage edp", CATALOGO)) == ["Sauvage EDP"]
    assert nomes(buscar("Sauvage Eau de Toilette", CATALOGO)) == ["Sauvage EDT"]


def test_erro_de_digitacao():
    assert "Malbec" in nomes(buscar("malbek", CATALOGO))
    assert set(nomes(buscar("savage", CATALOGO))) >= {"Sauvage EDP"}


def test_apelido_e_marca():
    assert nomes(buscar("cdnim", CATALOGO)) == ["Club de Nuit Intense Man"]
    assert set(nomes(buscar("dior", CATALOGO))) == {"Sauvage EDP", "Sauvage EDT", "Sauvage Elixir"}


def test_nada_bate():
    assert buscar("one million", CATALOGO) == []
    assert buscar("", CATALOGO) == []


# ============================================================
# marca
# ============================================================

@pytest.mark.parametrize(
    "consulta, marca, bate",
    [
        ("dior", "Dior", True),
        ("Dior", "Christian Dior", True),
        ("boticario", "O Boticário", True),
        ("boticário", "O Boticário", True),
        ("chanel", "Chanel", True),
        ("chanell", "Chanel", True),   # erro de digitação
        ("dior", "Chanel", False),
        ("natura", "O Boticário", False),
        ("dior", None, True),          # perfume sem marca cadastrada não é descartado
    ],
)
def test_marca_bate(consulta, marca, bate):
    from scentinel.busca import marca_bate

    assert marca_bate(consulta, marca) is bate


def test_buscar_com_marca_filtra():
    # "sauvage" sem marca acha as 3 variações da Dior
    assert len(buscar("sauvage", CATALOGO)) == 3
    assert set(nomes(buscar("sauvage", CATALOGO, marca="dior"))) == {"Sauvage EDP", "Sauvage EDT", "Sauvage Elixir"}
    assert buscar("sauvage", CATALOGO, marca="chanel") == []
    assert nomes(buscar("bleu", CATALOGO, marca="chanel")) == ["Bleu de Chanel Eau de Parfum"]
    assert nomes(buscar("malbec", CATALOGO, marca="boticario")) == ["Malbec"]
