"""Servidor MCP "ponte-gemini": deixa o agente consultar o Gemini pelo Antigravity CLI (`agy`).

Chama `agy -p` em modo headless, com a conta Google em que você já fez login no `agy`.

Segurança: por padrão o Gemini roda em modo `plan` + `--sandbox`. Ele pode LER arquivos para
responder, mas não edita nada nem roda comandos que mudem estado. Quem aplica mudanças é sempre o
agente principal, depois de revisar. Perguntas e pesquisas rodam numa pasta temporária vazia, para
o Gemini não enxergar a pasta de onde o servidor foi iniciado.

Configuração por variáveis de ambiente (todas opcionais):

    AGY_PATH                    caminho do executável agy (padrão: procura no PATH)
    PONTE_GEMINI_MODELO         modelo padrão (padrão "hydrogen")
    PONTE_GEMINI_MODELO_FORTE   modelo de gemini_analisar (padrão "carbon")
    PONTE_GEMINI_MODO           modo do agy (padrão "plan"; os outros modos podem gravar arquivos)
    PONTE_GEMINI_TEMPO          tempo limite por chamada, em segundos (padrão 900)
    PONTE_GEMINI_TMP            onde criar as pastas temporárias (padrão: a do sistema)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

APELIDOS = ("carbon", "argon", "hydrogen", "flash")
ESFORCOS = ("low", "medium", "high", "xhigh")
# Nome de modelo e de modo vão direto para a linha de comando: só letras, números, ponto e hífen,
# começando por letra ou número (nada de "--alguma-flag").
_NOME_SEGURO = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")


def _numero(nome: str, padrao: int) -> int:
    try:
        return int(os.environ.get(nome) or padrao)
    except ValueError:
        return padrao


MODELO_PADRAO = os.environ.get("PONTE_GEMINI_MODELO") or "hydrogen"
MODELO_FORTE = os.environ.get("PONTE_GEMINI_MODELO_FORTE") or "carbon"
# Testado no agy 1.5: o modo padrão GRAVA arquivos na pasta; `plan` não grava nem roda comandos.
# Não use --disable-slash-commands: ele desliga o modo plan.
MODO = os.environ.get("PONTE_GEMINI_MODO") or "plan"
TEMPO_LIMITE = _numero("PONTE_GEMINI_TEMPO", 900)
LIMITE_PROMPT_DIRETO = 12_000  # acima disso o texto vai por arquivo (limite de linha de comando do Windows)
PASTA_TEMP: str | None = os.environ.get("PONTE_GEMINI_TMP") or None

# Sem isso, no modo plan o Gemini escreve um plano e espera aprovação em vez de responder.
ENQUADRAMENTO = (
    "Modo consulta: não há nada para implementar nem editar. Não crie planos nem arquivos. "
    "Leia o que precisar e responda diretamente no chat com a resposta completa.\n\n"
)

NAO_INSTALADO = (
    "O Antigravity CLI (agy) não foi encontrado. Instale pelo site oficial (antigravity.google), "
    "rode `agy` uma vez num terminal para fazer login, ou aponte AGY_PATH para o executável."
)
SEM_LOGIN = (
    "O agy está instalado mas sem login. Abra um terminal, rode `agy`, faça login no navegador e "
    "feche com /exit. Depois tente de novo."
)

servidor = MCPServer(
    name="ponte-gemini",
    instructions=(
        "Gemini do Google via Antigravity CLI. Modelos: carbon (o mais forte, para revisão "
        "independente e problemas difíceis), argon (planejamento), hydrogen (rápido, padrão). Use "
        "como segunda opinião independente, para pesquisa na web com Google, para ler muitos "
        "arquivos sem gastar seu contexto e para analisar imagens, PDFs e vídeos. O Gemini só lê: "
        "quem edita é você. Confira o que ele afirmar antes de aplicar."
    ),
)

_cache_modelos: list[str] | None = None


def _agy() -> str | None:
    """Acha o executável. Um AGY_PATH definido manda: se estiver errado, não cai para outro agy."""
    definido = os.environ.get("AGY_PATH", "")
    if definido:
        return definido if Path(definido).expanduser().is_file() else None
    candidatos = [
        shutil.which("agy") or "",
        str(Path(os.environ.get("LOCALAPPDATA") or "~") / "agy" / "bin" / "agy.exe"),  # instalador do Windows
    ]
    return next((c for c in candidatos if c and Path(c).expanduser().is_file()), None)


async def _executar(args: list[str], cwd: str | None = None, tempo: int = TEMPO_LIMITE) -> tuple[int, str, str]:
    try:
        processo = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as erro:
        return -1, "", f"Não consegui executar {args[0]}: {erro}"
    try:
        saida, erro = await asyncio.wait_for(processo.communicate(), timeout=tempo + 30)
    except TimeoutError:
        _matar(processo)
        await processo.wait()
        return -1, "", f"O Gemini passou de {tempo}s e foi interrompido."
    except BaseException:  # pedido cancelado pelo cliente MCP: não deixa o agy órfão gastando cota
        _matar(processo)
        raise
    return (
        processo.returncode or 0,
        saida.decode("utf-8", errors="replace"),
        erro.decode("utf-8", errors="replace"),
    )


def _matar(processo: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):  # pode ter terminado sozinho nesse meio-tempo
        processo.kill()


def extrair_modelos(saida: str) -> list[str]:
    """Pega os slugs de modelo da saída de `agy models`, sem repetir e na ordem em que aparecem."""
    slugs = re.findall(r"(?<![\w.\-])[a-z0-9][a-z0-9.\-]*[a-z0-9](?![\w.\-])", saida)
    return [s for s in dict.fromkeys(slugs) if "-" in s and ("gemini" in s or any(a in s for a in APELIDOS))]


async def _listar_modelos(agy: str) -> list[str]:
    """Lista e guarda os modelos. Uma falha (ex.: antes do login) não fica guardada: tenta de novo depois."""
    global _cache_modelos
    if not _cache_modelos:
        codigo, saida, _ = await _executar([agy, "models"], tempo=60)
        _cache_modelos = extrair_modelos(saida) if codigo == 0 else []
    return _cache_modelos


def escolher_slug(modelo: str, esforco: str, disponiveis: list[str]) -> str:
    """Aceita o slug exato de `agy models` ou apelido + esforço.

    O esforço faz parte do slug (carbon + high -> gemini-4-carbon-high). Se o esforço pedido não
    existir para aquele modelo, usa o mais alto disponível abaixo dele; se não houver nenhum abaixo,
    o mais baixo que existir. Esforço desconhecido conta como "medium".
    """
    modelo = (modelo or MODELO_PADRAO).strip().lower()
    esforco = (esforco or "medium").strip().lower()
    if esforco not in ESFORCOS:
        esforco = "medium"
    if modelo in disponiveis or not disponiveis:
        return modelo
    variantes: dict[str, str] = {}
    for slug in disponiveis:  # a primeira ocorrência vence: `agy models` lista os mais novos antes
        sufixo = slug.rsplit("-", 1)[-1]
        if f"-{modelo}-" in f"-{slug}" and sufixo in ESFORCOS:
            variantes.setdefault(sufixo, slug)
    if not variantes:
        sem_esforco = [s for s in disponiveis if s.endswith(f"-{modelo}")]
        return sem_esforco[0] if sem_esforco else modelo
    if esforco in variantes:
        return variantes[esforco]
    abaixo = [e for e in ESFORCOS[: ESFORCOS.index(esforco) + 1] if e in variantes]
    return variantes[abaixo[-1]] if abaixo else variantes[min(variantes, key=ESFORCOS.index)]


def _envelope(saida: str) -> dict:
    """O agy imprime logs e depois o JSON. Tenta a saída inteira e, se não der, a última linha."""
    texto = saida.strip()
    for candidato in (texto, texto.splitlines()[-1] if texto else ""):
        try:
            dados = json.loads(candidato)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(dados, dict):
            return dados
    return {}


def interpretar_resposta(codigo: int, saida: str, erro: str, slug: str) -> tuple[str | None, str]:
    """Lê o envelope JSON do `agy --output-format json`.

    Devolve (resposta, detalhe): `resposta` é o texto final com rodapé, ou None se falhou; nesse
    caso `detalhe` explica o motivo.
    """
    envelope = _envelope(saida)
    if envelope.get("status") == "SUCCESS":
        texto = str(envelope.get("response") or "").strip()
        if texto:
            uso = envelope.get("usage") or {}
            rodape = (
                f"\n\n---\n[Gemini · modelo {slug} · {envelope.get('duration_seconds', '?')}s · "
                f"{uso.get('total_tokens', '?')} tokens]"
            )
            return texto + rodape, ""
    # Só procura o aviso de login quando a chamada falhou: uma resposta boa pode citar essa frase.
    if "authentication required" in (erro + str(envelope.get("error") or "")).lower():
        return None, SEM_LOGIN
    detalhe = str(envelope.get("error") or erro.strip() or saida.strip() or "resposta vazia")[:1500]
    return None, f"O Gemini não respondeu (código {codigo}, status {envelope.get('status', '?')}): {detalhe}"


_MODELO_RECUSADO = re.compile(r"model\S*\s.*(not found|unknown|invalid|unsupported|not available)", re.I)


async def _gemini(
    prompt: str,
    modelo: str = "",
    esforco: str = "",
    pasta: str = "",
    pastas_extras: list[str] | None = None,
) -> str:
    agy = _agy()
    if not agy:
        return NAO_INSTALADO
    if not _NOME_SEGURO.match(MODO):
        return f"PONTE_GEMINI_MODO inválido: {MODO!r}"
    modelo = (modelo or MODELO_PADRAO).strip().lower()
    if not _NOME_SEGURO.match(modelo):
        return (
            f"Nome de modelo inválido: {modelo!r}. Use um apelido (carbon, argon, hydrogen) ou um slug "
            "de gemini_modelos."
        )

    pasta_trabalho = str(Path(pasta).expanduser()) if pasta else None
    if pasta_trabalho and not Path(pasta_trabalho).is_dir():
        return f"Pasta não encontrada: {pasta_trabalho}"

    extras = [str(Path(p).expanduser().resolve()) for p in pastas_extras or []]
    with tempfile.TemporaryDirectory(prefix="ponte-gemini-", dir=PASTA_TEMP, ignore_cleanup_errors=True) as temp:
        if len(prompt) > LIMITE_PROMPT_DIRETO:
            arquivo = Path(temp) / "pedido.md"
            arquivo.write_text(prompt, encoding="utf-8")
            extras.append(temp)
            prompt = (
                f"O pedido completo está no arquivo {arquivo}. Leia o arquivo inteiro e responda ao "
                "que ele pede. Não edite nenhum arquivo."
            )

        slug = escolher_slug(modelo, esforco, await _listar_modelos(agy))
        args = [
            agy,
            "-p",
            ENQUADRAMENTO + prompt,
            "--output-format",
            "json",
            "--mode",
            MODO,
            "--sandbox",
            "--print-timeout",
            f"{TEMPO_LIMITE}s",
            "--model",
            slug,
        ]
        for extra in extras:
            args += ["--add-dir", extra]
        # Sem pasta pedida, roda na pasta temporária vazia em vez da pasta atual do servidor.
        codigo, saida, erro = await _executar(args, cwd=pasta_trabalho or temp)

    resposta, detalhe = interpretar_resposta(codigo, saida, erro, slug)
    if resposta is not None:
        return resposta
    if codigo != 0 and _MODELO_RECUSADO.search(erro):
        disponiveis = await _listar_modelos(agy)
        return f"Modelo '{slug}' recusado. Disponíveis: {', '.join(disponiveis) or '(rode agy models)'}\n{erro[:800]}"
    return detalhe


@servidor.tool()
async def gemini_perguntar(pergunta: str, contexto: str = "", modelo: str = "", esforco: str = "") -> str:
    """Pergunta ao Gemini e devolve a resposta. Bom para segunda opinião independente sobre um
    plano, decisão ou trecho de código. `modelo`: hydrogen (padrão, rápido), argon (planejar) ou
    carbon (o mais forte), ou um slug exato de gemini_modelos. `esforco`: low, medium (padrão),
    high ou xhigh (nem todo modelo tem todos). Passe em `contexto` o texto de apoio (código, diff,
    spec), porque o Gemini não vê esta conversa."""
    prompt = pergunta if not contexto else f"Contexto:\n{contexto}\n\n---\nPedido:\n{pergunta}"
    return await _gemini(prompt, modelo or MODELO_PADRAO, esforco)


@servidor.tool()
async def gemini_analisar(
    pasta: str, pedido: str, arquivos: list[str] | None = None, modelo: str = "", esforco: str = "high"
) -> str:
    """O Gemini abre uma pasta em modo só-leitura e faz o que você pedir: revisão de código
    independente, achar bugs, mapear um repositório grande, resumir muitos arquivos, ou analisar
    imagens, PDFs e vídeos. O conteúdo NÃO entra no seu contexto, só a resposta. `arquivos`
    (opcional) lista caminhos para ele focar; se algum estiver fora de `pasta`, a pasta onde ele
    está também é liberada para leitura. Padrão: modelo carbon com esforço high."""
    base = Path(pasta).expanduser().resolve()
    caminhos = [Path(a).expanduser().resolve() for a in arquivos or []]
    extras = sorted({str(c.parent) for c in caminhos if not c.is_relative_to(base)})
    foco = "\n\nFoque nestes arquivos:\n" + "\n".join(f"- {c}" for c in caminhos) if caminhos else ""
    prompt = (
        f"{pedido}{foco}\n\nRegras: só leia, não edite nada nem rode comandos que mudem estado. "
        "Cite caminho:linha quando apontar algo no código. Diga quando não tiver certeza."
    )
    return await _gemini(prompt, modelo or MODELO_FORTE, esforco, pasta=pasta, pastas_extras=extras)


@servidor.tool()
async def gemini_pesquisar(pergunta: str, modelo: str = "", esforco: str = "") -> str:
    """Pesquisa na web atual usando o Gemini (Google Search) e devolve um resumo com links das
    fontes. Bom para notícias recentes, preços, documentação nova e comparar fontes."""
    prompt = (
        f"Pesquise na web e responda: {pergunta}\n\n"
        "Use buscas reais, prefira fontes oficiais, inclua a data das informações e termine com "
        "a lista de URLs usadas. Se as fontes discordarem, diga."
    )
    return await _gemini(prompt, modelo or MODELO_PADRAO, esforco)


@servidor.tool()
async def gemini_modelos() -> str:
    """Lista os modelos que a sua conta pode usar no Antigravity CLI (slugs aceitos em `modelo`)."""
    agy = _agy()
    if not agy:
        return NAO_INSTALADO
    codigo, saida, erro = await _executar([agy, "models"], tempo=60)
    if codigo != 0 and "authentication required" in (saida + erro).lower():
        return SEM_LOGIN
    return saida.strip() or erro.strip() or f"agy models terminou com código {codigo}"


def main() -> None:
    servidor.run()


if __name__ == "__main__":
    main()
