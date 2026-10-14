"""Abre uma página num Chromium escondido (Playwright), igual a uma pessoa abrindo o site.

Só o /locate usa (a busca do site do ML acha os anúncios que ficam fora do catálogo da API).
Um navegador por vez (o notebook tem pouca RAM), imagem/fonte/vídeo não são baixados, e se o
site pedir verificação/captcha a gente desiste: não força.

Instalação (uma vez, no notebook, sem sudo):
    .venv/bin/python -m playwright install --only-shell chromium
"""

import logging
import os
import re
import threading

from scentinel import coleta

log = logging.getLogger(__name__)

TEMPO_S = 30
_uma_por_vez = threading.Semaphore(1)
_BLOQUEIO = re.compile(r"captcha|account-verification|/gz/|/lgz/|/login|registration", re.IGNORECASE)
# texto que aparece na tela quando o site quer saber se é gente (não o código da página)
_VERIFICACAO = re.compile(
    r"n[ãa]o (é|e|sou) (um )?rob[ôo]|confirme que voc[êe]|verifica[çc][ãa]o de seguran[çc]a|"
    r"are you a robot|i'?m not a robot|acesso negado|access denied|unusual traffic|tr[áa]fego incomum",
    re.IGNORECASE,
)
INSTALAR = ".venv/bin/python -m playwright install --only-shell chromium"
INSTALAR_LIBS = "sudo .venv/bin/python -m playwright install-deps chromium"


def disponivel():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def garantir_instalado(tempo_s=900):
    """Baixa o Chromium do Playwright se ainda não tiver (sem sudo: vai pra ~/.cache/ms-playwright).
    Já instalado, termina em ~1 s. O bot chama isso ao ligar, em segundo plano."""
    if not disponivel():
        return False
    import subprocess
    import sys

    try:
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"],
                           capture_output=True, text=True, timeout=tempo_s)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("não deu pra instalar o Chromium do /locate: %s", e)
        return False
    if r.returncode != 0:
        log.warning("não deu pra instalar o Chromium do /locate: %s", (r.stderr or r.stdout).strip()[-300:])
        return False
    return True


def _curto(url):
    """'https://www.mercadolivre.com.br/gz/account-verification?go=...' -> 'mercadolivre.com.br/gz/account-verif...'"""
    m = re.match(r"https?://(?:www\.)?([^?#]+)", url or "")
    texto = m.group(1) if m else str(url)
    return texto if len(texto) <= 60 else texto[:57] + "..."


def _texto(funcao):
    try:
        return funcao() or ""
    except Exception:
        return ""


def _pular_peso(rota):
    if rota.request.resource_type in ("image", "media", "font"):
        return rota.abort()
    return rota.continue_()


def baixar_html(url, esperar=None, tempo_s=TEMPO_S, executavel=None, espera_vez_s=None, espera_conteudo_s=10):
    """Abre `url` e devolve (html_bytes, erro, bloqueado). `esperar`: seletor CSS do conteúdo
    (espera ele aparecer; se não aparecer, devolve a página como estiver). `espera_vez_s`: quanto
    esperar se outra pesquisa estiver usando o navegador (padrão: tempo_s)."""
    try:
        from playwright.sync_api import Error as ErroPW
        from playwright.sync_api import TimeoutError as TempoPW
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "navegador não instalado (rode no notebook: scentinel-update)", False
    if not _uma_por_vez.acquire(timeout=tempo_s if espera_vez_s is None else espera_vez_s):
        return None, "navegador ocupado com outra pesquisa", False
    try:
        with sync_playwright() as p:
            args = ["--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--mute-audio"]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                args.append("--no-sandbox")  # root (servidor de teste) não roda com sandbox
            navegador = p.chromium.launch(headless=True, args=args, executable_path=executavel)
            try:
                contexto = navegador.new_context(
                    locale="pt-BR", user_agent=coleta.CABECALHOS["User-Agent"],
                    viewport={"width": 1280, "height": 900},
                )
                pagina = contexto.new_page()
                pagina.route("**/*", _pular_peso)
                resposta = pagina.goto(url, wait_until="domcontentloaded", timeout=tempo_s * 1000)
                if _BLOQUEIO.search(pagina.url):
                    return None, f"o site pediu verificação ({_curto(pagina.url)}; não forcei)", True
                status = resposta.status if resposta else 200
                if status in (403, 429):  # recusou (e não gasta 10 s esperando o conteúdo)
                    return None, f"o site recusou (HTTP {status}, não forcei)", True
                if status >= 400:
                    return None, f"o site respondeu HTTP {status}", False
                achou = True
                if esperar:
                    try:
                        pagina.wait_for_selector(esperar, timeout=min(espera_conteudo_s, tempo_s) * 1000)
                    except TempoPW:
                        achou = False  # sem resultado, ou página diferente: devolve o que tiver
                html = pagina.content().encode("utf-8")
                if _BLOQUEIO.search(pagina.url):
                    return None, f"o site pediu verificação ({_curto(pagina.url)}; não forcei)", True
                if not achou:
                    # desafio na mesma URL: olha o TEXTO que aparece na tela (o código da página do ML cita
                    # "captcha" nos scripts mesmo quando está tudo normal)
                    visivel = f"{_texto(pagina.title)} {_texto(lambda: pagina.inner_text('body'))}"[:5000]
                    if _VERIFICACAO.search(visivel):
                        return None, f"o site pediu verificação (\"{_texto(pagina.title)[:50]}\"; não forcei)", True
                return html, None, False
            finally:
                navegador.close()
    except TempoPW:
        return None, f"o site não abriu em {tempo_s} s", False
    except ErroPW as e:
        texto = str(e)
        if "executable doesn't exist" in texto.lower() or "playwright install" in texto:
            return None, f"Chromium não instalado (rode no notebook: {INSTALAR})", False
        if "shared librar" in texto or "missing dependencies" in texto:
            return None, f"faltam bibliotecas do Chromium (rode no notebook: {INSTALAR_LIBS})", False
        log.warning("navegador: %s", texto[:300])
        return None, f"navegador: {texto.splitlines()[0][:120]}", False
    except Exception as e:  # nunca derruba o /locate
        log.exception("navegador falhou")
        return None, f"navegador: {e.__class__.__name__}", False
    finally:
        _uma_por_vez.release()
