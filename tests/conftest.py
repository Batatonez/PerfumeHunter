from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def ml_sem_login(monkeypatch, tmp_path):
    """Os testes nunca usam o login de verdade do ML (dados/ml_token.json e o .env do notebook),
    nem esperam o respiro entre chamadas da API, nem abrem navegador, nem mexem no catálogo automático."""
    from scentinel import autocatalogo
    from scentinel.fontes import mercadolivre as ml

    from scentinel import atualizar

    monkeypatch.setattr(autocatalogo, "ARQ_AUTO", tmp_path / "catalogo_auto_de_teste.json")
    monkeypatch.setattr(atualizar, "ARQ_VERSAO", tmp_path / "versao_rodando_de_teste")
    monkeypatch.setattr(ml, "ARQ_TOKEN", tmp_path / "ml_token_de_teste.json")
    monkeypatch.setattr(ml, "_respiro", lambda: None)
    monkeypatch.setattr(ml, "INTERVALO_API_S", 0)
    # a fonte do ML nem abre o Chromium de verdade (o teste do navegador usa o módulo direto, de propósito)
    monkeypatch.setattr(ml, "navegador", SimpleNamespace(
        baixar_html=lambda *a, **k: (None, "navegador desligado no teste", False), disponivel=lambda: False))
    for var in ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
