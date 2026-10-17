import os
import subprocess
from types import SimpleNamespace

import pytest

PASTA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def _arquivos_do_git(tmp_path_factory):
    """No /atualizar e no scentinel-update (SCENTINEL_TESTE_PELO_GIT=1): se o config.toml ou o catalogo.toml
    foram mexidos no notebook, os testes usam a versão do git. Assim uma configuração do dono nunca trava
    as atualizações (os testes conferem o código, não as escolhas dele)."""
    saida = {}
    if not os.environ.get("SCENTINEL_TESTE_PELO_GIT"):
        return saida
    pasta = tmp_path_factory.mktemp("do_git")
    for nome in ("config.toml", "catalogo.toml"):
        try:
            mexido = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", nome], cwd=PASTA, timeout=30).returncode
            if mexido != 1:
                continue
            r = subprocess.run(["git", "show", f"HEAD:{nome}"], cwd=PASTA, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout:
            (pasta / nome).write_bytes(r.stdout)
            saida[nome] = pasta / nome
    return saida


@pytest.fixture(autouse=True)
def ml_sem_login(monkeypatch, tmp_path, _arquivos_do_git):
    """Os testes nunca usam o login de verdade do ML (dados/ml_token.json e o .env do notebook),
    nem esperam o respiro entre chamadas da API, nem abrem navegador, nem mexem no catálogo automático."""
    from scentinel import autocatalogo
    from scentinel.fontes import mercadolivre as ml

    from scentinel import atualizar, catalogo, config

    monkeypatch.setattr(config, "ARQ_CONFIG_LOCAL", tmp_path / "config.local.toml.nao_existe")
    if "config.toml" in _arquivos_do_git:
        monkeypatch.setattr(config, "ARQ_CONFIG", _arquivos_do_git["config.toml"])
    if "catalogo.toml" in _arquivos_do_git:
        monkeypatch.setattr(catalogo, "ARQ_CATALOGO", _arquivos_do_git["catalogo.toml"])
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
