import pytest


@pytest.fixture(autouse=True)
def ml_sem_login(monkeypatch, tmp_path):
    """Os testes nunca usam o login de verdade do ML (dados/ml_token.json e o .env do notebook)
    nem esperam o respiro entre chamadas da API."""
    from scentinel.fontes import mercadolivre as ml

    monkeypatch.setattr(ml, "ARQ_TOKEN", tmp_path / "ml_token_de_teste.json")
    monkeypatch.setattr(ml, "_respiro", lambda: None)
    for var in ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
