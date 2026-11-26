"""Servidor MCP "ponte-local": deixa o agente delegar tarefas a um LLM rodando na sua máquina.

Fala com qualquer servidor compatível com a API da OpenAI (`/v1/chat/completions`):
llama.cpp (`llama-server`), LM Studio, Ollama, vLLM, etc.

A economia de tokens vem do `local_resumir_arquivo`: o arquivo grande é lido aqui, resumido pelo
modelo local em partes, e só o resumo volta para o agente.

Configuração por variáveis de ambiente (todas opcionais):

    PONTE_LOCAL_URL          endereço do servidor (padrão http://127.0.0.1:8080; aceita com ou sem /v1)
    PONTE_LOCAL_MODELO       nome do modelo no servidor (padrão: o primeiro de /v1/models)
    PONTE_LOCAL_API_KEY      chave, se o servidor pedir uma
    PONTE_LOCAL_TEMPERATURA  padrão 0.7
    PONTE_LOCAL_SEM_PENSAR   "1" desliga o modo de raciocínio (Qwen3 e afins, via chat_template_kwargs)
    PONTE_LOCAL_TEMPO        tempo limite por chamada, em segundos (padrão 900)
    PONTE_LOCAL_PARTE        caracteres por parte ao resumir (padrão 60000, ~15k tokens)
    PONTE_LOCAL_LIMITE       tamanho máximo do arquivo a resumir, em caracteres (padrão 3000000)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

logging.getLogger("httpx").setLevel(logging.WARNING)


def _numero(nome: str, padrao: float) -> float:
    """Lê um número do ambiente; vazio ou inválido vira o padrão, para o servidor nunca falhar ao subir."""
    try:
        return float(os.environ.get(nome) or padrao)
    except ValueError:
        return padrao


BASE_URL = (os.environ.get("PONTE_LOCAL_URL") or "http://127.0.0.1:8080").rstrip("/").removesuffix("/v1")
MODELO = os.environ.get("PONTE_LOCAL_MODELO", "")
API_KEY = os.environ.get("PONTE_LOCAL_API_KEY", "")
TEMPERATURA = _numero("PONTE_LOCAL_TEMPERATURA", 0.7)
SEM_PENSAR = os.environ.get("PONTE_LOCAL_SEM_PENSAR", "") == "1"
TEMPO_LIMITE = _numero("PONTE_LOCAL_TEMPO", 900)
TAMANHO_PARTE = max(1000, int(_numero("PONTE_LOCAL_PARTE", 60_000)))
LIMITE_ARQUIVO = max(1000, int(_numero("PONTE_LOCAL_LIMITE", 3_000_000)))

DESLIGADO = (
    "O servidor local não respondeu em PONTE_LOCAL_URL. Inicie o llama-server, LM Studio ou Ollama e "
    "tente de novo, ou siga sem o modelo local."
)
SO_PENSOU = (
    "O modelo gastou todos os tokens raciocinando e não chegou à resposta. Defina "
    "PONTE_LOCAL_SEM_PENSAR=1 ou use um modelo sem modo de raciocínio."
)

# Usado só pelos testes, para trocar a rede por respostas prontas.
_TRANSPORTE: httpx.AsyncBaseTransport | None = None

servidor = MCPServer(
    name="ponte-local",
    instructions=(
        "Modelo de linguagem servido pelo endereço que o usuário configurou (em geral na própria "
        "máquina, grátis). Use para resumir arquivos e logs grandes sem colocá-los no seu contexto, "
        "traduções, rascunhos e perguntas simples. É um modelo menor que você: confira o que for crítico."
    ),
)


class ErroLocal(Exception):
    """Falha ao falar com o servidor local; a mensagem já está pronta para o usuário."""


def _cliente() -> httpx.AsyncClient:
    cabecalhos = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    return httpx.AsyncClient(base_url=BASE_URL, timeout=TEMPO_LIMITE, headers=cabecalhos, transport=_TRANSPORTE)


_PENSAMENTO = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def _limpar(texto: str) -> str:
    """Remove o bloco <think>...</think> que modelos de raciocínio põem ANTES da resposta.

    Só mexe no começo do texto, para não apagar um <think> que apareça citado na resposta. Se o
    bloco abriu e não fechou (acabaram os tokens no meio do raciocínio), não há resposta para dar.
    """
    limpo = _PENSAMENTO.sub("", texto, count=1)
    if limpo.lstrip().startswith("<think>"):
        raise ErroLocal(SO_PENSOU)
    return limpo.strip()


_modelo_descoberto: str | None = None


async def _modelo(cliente: httpx.AsyncClient) -> str:
    """Usa PONTE_LOCAL_MODELO ou, se vazio, o primeiro modelo de /v1/models (descoberto uma vez só)."""
    global _modelo_descoberto
    if MODELO:
        return MODELO
    if _modelo_descoberto is None:
        resposta = await cliente.get("/v1/models")
        resposta.raise_for_status()
        dados = resposta.json().get("data") or []
        if not dados:
            raise ErroLocal("O servidor local não lista nenhum modelo em /v1/models. Defina PONTE_LOCAL_MODELO.")
        _modelo_descoberto = dados[0]["id"]
    return _modelo_descoberto


async def _chat(mensagens: list[dict], max_tokens: int = 2048) -> str:
    try:
        async with _cliente() as cliente:
            corpo: dict = {
                "model": await _modelo(cliente),
                "messages": mensagens,
                "max_tokens": max_tokens,
                "temperature": TEMPERATURA,
            }
            if SEM_PENSAR:
                corpo["chat_template_kwargs"] = {"enable_thinking": False}
            resposta = await cliente.post("/v1/chat/completions", json=corpo)
            resposta.raise_for_status()
            texto = _limpar(resposta.json()["choices"][0]["message"]["content"] or "")
    except httpx.ConnectError as erro:
        raise ErroLocal(DESLIGADO) from erro
    except httpx.HTTPStatusError as erro:
        raise ErroLocal(f"Erro do servidor local ({erro.response.status_code}): {erro.response.text[:500]}") from erro
    except httpx.TimeoutException as erro:
        raise ErroLocal(f"O modelo local passou de {TEMPO_LIMITE:.0f}s sem responder. Tente um pedido menor.") from erro
    except httpx.HTTPError as erro:
        raise ErroLocal(f"A conexão com o servidor local caiu: {erro!r}") from erro
    except (KeyError, IndexError, TypeError, ValueError) as erro:
        raise ErroLocal(f"Resposta inesperada do servidor local: {erro!r}") from erro
    if not texto:
        raise ErroLocal("O modelo local devolveu uma resposta vazia.")
    return texto


def dividir(texto: str, tamanho: int | None = None) -> list[str]:
    """Quebra o texto em partes de até `tamanho` caracteres, preferindo cortar entre linhas.

    Uma linha maior que `tamanho` (JSON minificado, CSV largo) é fatiada, sem perder nada.
    """
    tamanho = tamanho or TAMANHO_PARTE
    partes: list[str] = []
    atual: list[str] = []
    ocupado = 0
    for linha in texto.splitlines(keepends=True):
        for inicio in range(0, len(linha), tamanho):
            pedaco = linha[inicio : inicio + tamanho]
            if ocupado + len(pedaco) > tamanho and atual:
                partes.append("".join(atual))
                atual, ocupado = [], 0
            atual.append(pedaco)
            ocupado += len(pedaco)
    if atual:
        partes.append("".join(atual))
    return partes


async def _juntar(resumos: list[str], pedido: str) -> str:
    """Junta resumos parciais em rodadas, sem nunca mandar ao modelo mais que uma parte de cada vez."""
    while len(resumos) > 1:
        juntos = "".join(f"[Parte {i}]\n{r}\n\n" for i, r in enumerate(resumos, 1))
        grupos = dividir(juntos)
        if len(grupos) >= len(resumos):  # cada resumo já ocupa uma parte inteira: junta tudo de uma vez
            grupos = [juntos]
        resumos = [
            await _chat(
                [
                    {"role": "system", "content": "Junte os resumos parciais num resumo único, sem perder fatos."},
                    {"role": "user", "content": f"Foque em: {pedido}\n\n{grupo}"},
                ],
                max_tokens=3000,
            )
            for grupo in grupos
        ]
    return resumos[0]


@servidor.tool()
async def local_perguntar(pergunta: str, contexto: str = "") -> str:
    """Faz uma pergunta ao modelo local (grátis). Bom para traduções, rascunhos de texto, código
    repetitivo e perguntas gerais simples. Passe em `contexto` qualquer texto de apoio."""
    mensagens = [{"role": "system", "content": "Responda de forma direta e objetiva."}]
    if contexto:
        mensagens.append({"role": "user", "content": f"Contexto:\n{contexto}"})
    mensagens.append({"role": "user", "content": pergunta})
    try:
        return await _chat(mensagens, max_tokens=4096)
    except ErroLocal as erro:
        return str(erro)


@servidor.tool()
async def local_resumir_arquivo(caminho: str, foco: str = "") -> str:
    """Lê um arquivo de texto (log, documentação, CSV, código grande) e devolve só um resumo feito
    pelo modelo local, sem o conteúdo entrar no seu contexto. Use `foco` para dizer o que importa
    (ex.: "erros e stack traces", "funções públicas e o que fazem")."""
    arquivo = Path(caminho).expanduser()
    try:
        if not arquivo.is_file():
            return f"Arquivo não encontrado: {arquivo}"
        if arquivo.stat().st_size > LIMITE_ARQUIVO * 4:  # nem tenta ler algo muito acima do limite
            return f"Arquivo grande demais ({arquivo.stat().st_size:,} bytes). Filtre antes ou peça um trecho."
        texto = arquivo.read_text(encoding="utf-8", errors="replace")
    except OSError as erro:
        return f"Não consegui ler {arquivo}: {erro}"
    if len(texto) > LIMITE_ARQUIVO:
        return (
            f"Arquivo grande demais ({len(texto):,} caracteres; limite {LIMITE_ARQUIVO:,}, ajustável em "
            "PONTE_LOCAL_LIMITE). Peça um trecho específico (ex.: as últimas N linhas) ou filtre antes."
        )
    if not texto.strip():
        return f"O arquivo {arquivo} está vazio."

    pedido = foco or "os pontos principais, erros, números e nomes importantes"
    partes = dividir(texto)
    try:
        resumos = [
            await _chat(
                [
                    {
                        "role": "system",
                        "content": "Você resume arquivos para outro assistente. Seja fiel ao texto, "
                        "cite nomes, números de linha e valores exatos quando importarem. Não invente.",
                    },
                    {
                        "role": "user",
                        "content": f"Arquivo: {arquivo.name} (parte {i} de {len(partes)})\n"
                        f"Foque em: {pedido}\n\n---\n{parte}",
                    },
                ],
                max_tokens=1500,
            )
            for i, parte in enumerate(partes, 1)
        ]
        final = await _juntar(resumos, pedido)
    except ErroLocal as erro:
        return str(erro)

    linhas = texto.count("\n") + 1
    return (
        f"Resumo do modelo local de {arquivo} ({linhas:,} linhas, {len(texto):,} caracteres, "
        f"{len(partes)} parte(s)). Confira no arquivo o que for crítico.\n\n{final}"
    )


@servidor.tool()
async def local_modelos() -> str:
    """Lista os modelos que o servidor local oferece e qual está em uso."""
    try:
        async with _cliente() as cliente:
            resposta = await cliente.get("/v1/models")
            resposta.raise_for_status()
            ids = [m.get("id", "?") for m in resposta.json().get("data") or []]
    except httpx.ConnectError:
        return DESLIGADO
    except (httpx.HTTPError, ValueError) as erro:
        return f"Não consegui listar os modelos: {erro!r}"
    em_uso = MODELO or _modelo_descoberto or (ids[0] if ids else "(nenhum)")
    return "\n".join([f"Em uso: {em_uso}", *(f"- {i}" for i in ids)])


def main() -> None:
    servidor.run()


if __name__ == "__main__":
    main()
