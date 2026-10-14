"""/atualizar: o mesmo que o scentinel-update faz no notebook, mas pelo Discord (e sem sudo).

Baixa o código novo (git pull), instala dependência nova, roda os testes e, se tudo passar, o bot
sai com erro de propósito pra o systemd religar ele já na versão nova (Restart=on-failure).
Se os testes falharem, volta pro código de antes e o bot segue rodando como estava.
O que precisa de sudo (o arquivo do serviço do systemd mudou) fica pro scentinel-update.
"""

import os
import subprocess
import sys
from dataclasses import dataclass, field

from scentinel.config import PASTA_PROJETO

SAIDA_PRA_REINICIAR = 3  # qualquer código != 0 faz o systemd religar (Restart=on-failure)
_SEGREDOS = ("DISCORD_TOKEN", "ML_CLIENT_ID", "ML_CLIENT_SECRET")
# versão que o bot está rodando (gravada ao ligar): o scentinel-update e o /atualizar comparam com ela, assim
# código novo baixado mas não reiniciado (ex.: o Discord caiu no meio do /atualizar) não fica esquecido
ARQ_VERSAO = PASTA_PROJETO / "dados" / "versao_rodando"


@dataclass
class ResultadoAtualizacao:
    ok: bool
    reiniciar: bool = False
    linhas: list = field(default_factory=list)

    @property
    def texto(self):
        return "\n".join(self.linhas)


def _rodar(cmd, pasta, tempo_s=180):
    """(código de saída, saída). Os testes rodam sem os segredos do .env no ambiente."""
    ambiente = {k: v for k, v in os.environ.items() if k not in _SEGREDOS}
    try:
        r = subprocess.run(cmd, cwd=pasta, capture_output=True, text=True, timeout=tempo_s, env=ambiente)
    except subprocess.TimeoutExpired:
        return 124, f"demorou mais de {tempo_s} s"
    except OSError as e:
        return 127, str(e)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _ultima(saida):
    linhas = [x for x in (saida or "").splitlines() if x.strip()]
    return linhas[-1][:300] if linhas else "sem saída"


def registrar_versao(pasta=PASTA_PROJETO, rodar=_rodar, arq=None):
    """Ao ligar: guarda o commit que está rodando (e devolve ele; None se o git não responder)."""
    cod, saida = rodar(["git", "rev-parse", "HEAD"], pasta)
    if cod:
        return None
    versao = saida.strip()
    arq = arq or ARQ_VERSAO
    try:
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(versao + "\n", encoding="utf-8")
    except OSError:
        pass
    return versao


def atualizar(pasta=PASTA_PROJETO, rodar=_rodar, rodando=None):
    """rodando: commit que o bot está rodando agora (se for diferente do que está no disco, reinicia mesmo
    sem nada novo no git pull)."""
    cod, antes = rodar(["git", "rev-parse", "HEAD"], pasta)
    if cod:
        return ResultadoAtualizacao(False, linhas=[f"❌ O git não respondeu: {_ultima(antes)}"])
    antes = antes.strip()
    cod, saida = rodar(["git", "pull", "--ff-only", "-q"], pasta)
    if cod:
        return ResultadoAtualizacao(False, linhas=[f"❌ O git pull não deu: {_ultima(saida)}",
                                                   "Roda o `scentinel-update` no notebook e manda print pro Claude."])
    depois = rodar(["git", "rev-parse", "HEAD"], pasta)[1].strip()
    base = antes
    if depois == antes:
        if not rodando or rodando == depois:
            return ResultadoAtualizacao(True, linhas=["✅ Já está na versão mais nova."])
        base = rodando  # código novo já estava baixado, mas o bot não reiniciou: termina o serviço
        linhas = ["📦 O código novo já estava baixado, mas o bot ainda rodava a versão antiga."]
    else:
        linhas = ["📦 Novidades:"]
        log = rodar(["git", "log", "--oneline", "--no-decorate", f"{antes}..{depois}"], pasta)[1].splitlines()
        linhas += [f"• {x.split(' ', 1)[-1][:80]}" for x in log[:8]]
        if len(log) > 8:
            linhas.append(f"• ... e mais {len(log) - 8}")

    def mudou(*arquivos):
        # commit que não existe mais (histórico reescrito) dá erro no diff: conta como "mudou", por segurança
        return rodar(["git", "diff", "--quiet", base, depois, "--", *arquivos], pasta)[0] != 0

    def desfazer(motivo):
        if antes == depois:
            return ResultadoAtualizacao(False, linhas=linhas + [motivo, "Não reiniciei: o bot segue como estava."])
        cod, saida = rodar(["git", "reset", "-q", "--keep", antes], pasta)
        if cod:
            return ResultadoAtualizacao(False, linhas=linhas + [
                motivo, f"⚠️ E não consegui voltar o código ({_ultima(saida)}). O bot segue rodando a versão de "
                "antes, mas NÃO reinicia ele: roda o `scentinel-update` no notebook e manda print pro Claude."])
        return ResultadoAtualizacao(False, linhas=linhas + [motivo, "Voltei pro código de antes: o bot segue como estava."])

    if mudou("requirements.txt"):
        cod, saida = rodar([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], pasta, 600)
        if cod:
            return desfazer(f"❌ Não deu pra instalar as dependências novas: {_ultima(saida)}")
        linhas.append("📥 Dependências novas instaladas")

    cod, saida = rodar([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], pasta, 300)
    if cod:
        return desfazer(f"❌ Os testes falharam: {_ultima(saida)}")
    linhas.append(f"🧪 {_ultima(saida)}")
    if mudou("scentinel.service"):
        linhas.append("⚠️ O serviço do systemd mudou: depois roda `scentinel-update --reiniciar` no notebook "
                      "(esse precisa da senha).")
    return ResultadoAtualizacao(True, reiniciar=True, linhas=linhas)
