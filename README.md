# ponte-llm

Dois servidores [MCP](https://modelcontextprotocol.io) que deixam o Claude Code (ou qualquer agente
que fale MCP) **delegar trabalho** para outros modelos:

- **`ponte-local`**: um LLM rodando na sua máquina (llama.cpp, LM Studio, Ollama, vLLM). Grátis,
  offline e sem gastar o contexto do agente principal.
- **`ponte-gemini`**: o Gemini do Google, pelo Antigravity CLI (`agy`), em modo só-leitura. Serve
  como segunda opinião independente, pesquisa na web e leitura de repositórios grandes.

> **English:** two MCP servers that let Claude Code (or any MCP client) hand work off to a local
> LLM (any OpenAI-compatible server) or to Google's Gemini through the Antigravity CLI, read-only.
> Install with `uvx --from git+https://github.com/joaobyss/ponte-llm ponte-local` (or `ponte-gemini`).
> Docs below are in Portuguese.

## Por que isso existe

O contexto do agente principal é caro e acaba rápido. Um log de 20 mil linhas não precisa entrar
inteiro nele: o modelo local lê o arquivo em partes e devolve só o resumo. Do mesmo jeito, uma
revisão de código feita por outro modelo, que não viu a conversa, pega coisas que o primeiro deixou
passar.

```
Claude Code ──MCP──▶ ponte-local  ──HTTP──▶ llama-server / LM Studio / Ollama
            └─MCP──▶ ponte-gemini ──CLI───▶ agy -p --mode plan --sandbox
```

## Ferramentas

| Servidor | Ferramenta | O que faz |
|---|---|---|
| `ponte-local` | `local_perguntar` | Pergunta direta ao modelo local (tradução, rascunho, código repetitivo). |
| | `local_resumir_arquivo` | Lê um arquivo grande em partes e devolve só o resumo, com o `foco` que você pedir. |
| | `local_modelos` | Lista os modelos que o servidor local oferece. |
| `ponte-gemini` | `gemini_perguntar` | Segunda opinião sobre um plano, decisão ou trecho de código. |
| | `gemini_analisar` | Abre uma pasta em modo só-leitura: revisão, caça a bugs, PDFs, imagens, vídeos. |
| | `gemini_pesquisar` | Pesquisa na web com Google Search e devolve resumo com links. |
| | `gemini_modelos` | Lista os modelos que a sua conta pode usar. |

## Instalação

Precisa de Python 3.11+ e do [uv](https://docs.astral.sh/uv/). Os comandos abaixo instalam direto
do GitHub; nada é instalado globalmente, o `uvx` cria um ambiente isolado.

### Modelo local

1. Suba um servidor compatível com a API da OpenAI. Exemplos:
   - llama.cpp: `llama-server -m modelo.gguf --port 8080`
   - LM Studio: aba *Developer* → *Start server* (porta 1234)
   - Ollama: já expõe `http://127.0.0.1:11434/v1`
2. Registre no Claude Code:

   ```bash
   claude mcp add ponte-local -e PONTE_LOCAL_URL=http://127.0.0.1:8080 -- uvx --from git+https://github.com/joaobyss/ponte-llm ponte-local
   ```

### Gemini

1. Instale o Antigravity CLI pelo site oficial e rode `agy` uma vez num terminal para fazer login.
2. Registre no Claude Code:

   ```bash
   claude mcp add ponte-gemini -- uvx --from git+https://github.com/joaobyss/ponte-llm ponte-gemini
   ```

### Claude Desktop (ou outro cliente com arquivo JSON)

```json
{
  "mcpServers": {
    "ponte-local": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/joaobyss/ponte-llm", "ponte-local"],
      "env": { "PONTE_LOCAL_URL": "http://127.0.0.1:8080" }
    },
    "ponte-gemini": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/joaobyss/ponte-llm", "ponte-gemini"]
    }
  }
}
```

## Configuração

Tudo por variável de ambiente, tudo opcional.

**`ponte-local`**

| Variável | Padrão | Para quê |
|---|---|---|
| `PONTE_LOCAL_URL` | `http://127.0.0.1:8080` | Endereço do servidor (com ou sem `/v1` no fim). |
| `PONTE_LOCAL_MODELO` | primeiro de `/v1/models` | Qual modelo usar. |
| `PONTE_LOCAL_API_KEY` | vazio | Se o servidor pedir chave. |
| `PONTE_LOCAL_TEMPERATURA` | `0.7` | Temperatura. |
| `PONTE_LOCAL_SEM_PENSAR` | vazio | `1` desliga o raciocínio de modelos como Qwen3 (`enable_thinking=false`). |
| `PONTE_LOCAL_TEMPO` | `900` | Tempo limite por chamada, em segundos. |
| `PONTE_LOCAL_PARTE` | `60000` | Caracteres por parte ao resumir (~15k tokens). Diminua se o contexto do modelo for pequeno. |
| `PONTE_LOCAL_LIMITE` | `3000000` | Tamanho máximo do arquivo a resumir, em caracteres. |

**`ponte-gemini`**

| Variável | Padrão | Para quê |
|---|---|---|
| `AGY_PATH` | procura no PATH | Caminho do executável `agy`. Se definido e errado, dá erro em vez de usar outro `agy`. |
| `PONTE_GEMINI_MODELO` | `hydrogen` | Modelo de `gemini_perguntar` e `gemini_pesquisar`. |
| `PONTE_GEMINI_MODELO_FORTE` | `carbon` | Modelo de `gemini_analisar`. |
| `PONTE_GEMINI_MODO` | `plan` | Modo do `agy`. **Os outros modos podem gravar arquivos.** |
| `PONTE_GEMINI_TEMPO` | `900` | Tempo limite por chamada, em segundos. |
| `PONTE_GEMINI_TMP` | pasta temporária do sistema | Onde criar as pastas temporárias de cada chamada. |

## Decisões de projeto

- **O Gemini nunca edita.** Ele roda com `--mode plan --sandbox` e um enquadramento que pede
  resposta direta. Quem aplica mudanças é o agente principal, depois de conferir. Nome de
  modelo e de modo só aceitam letras, números, ponto e hífen (nada que comece com `-`), e os
  testes garantem que `--dangerously-skip-permissions` não entra no comando.
- **Pergunta não enxerga seus arquivos.** `gemini_perguntar` e `gemini_pesquisar` rodam numa
  pasta temporária vazia, não na pasta de onde o servidor foi aberto. `gemini_analisar` só libera
  a pasta pedida e, se algum arquivo de `arquivos` estiver fora dela, a pasta desse arquivo.
- **Pedidos longos vão por arquivo.** A linha de comando do Windows tem limite de tamanho; acima
  de 12 mil caracteres o pedido é gravado num arquivo dentro da pasta temporária da chamada,
  que é apagada logo depois.
- **Apelido + esforço viram slug.** `carbon` + `high` vira `gemini-4-carbon-high`. Se o esforço
  pedido não existir para o modelo, usa o mais alto abaixo dele (ou o mais baixo disponível, se
  não houver nenhum abaixo), em vez de falhar.
- **Resumo em rodadas.** Cada parte do arquivo é resumida separada; depois os resumos são
  juntados em grupos que cabem numa parte, até sobrar um só. Nenhuma chamada manda ao modelo mais
  que `PONTE_LOCAL_PARTE` caracteres de conteúdo, então funciona com contexto de 32k a 64k tokens.
- **Raciocínio fica de fora.** O bloco `<think>` que modelos como Qwen3 põem antes da resposta é
  removido. Se o modelo gastar todos os tokens pensando, a ferramenta avisa em vez de devolver o
  raciocínio pela metade.
- **Erros viram texto, não exceção.** Servidor desligado, sem login ou modelo recusado voltam como
  uma mensagem que diz o que fazer. O agente consegue seguir sem o ajudante.

## Limites

- O modelo local é menor que o agente principal. Confira no arquivo original o que for crítico
  (números de linha, nomes, valores).
- O `ponte-gemini` depende do formato JSON do `agy` (`--output-format json`). Testado no agy 1.5;
  versões futuras podem mudar o envelope.
- Nenhum dos dois servidores limita quais arquivos podem ser lidos: eles leem o que o agente pedir,
  com as permissões do seu usuário.
- **Injeção de prompt.** Se você mandar o Gemini analisar um repositório de terceiros, um texto
  escondido nele pode tentar instruí-lo. O modo `plan` impede edição e comandos, mas a busca na web
  continua ligada, então não analise código desconhecido numa pasta que tenha segredos (`.env`).
- O texto que você manda vai direto para o `agy`. Comandos de barra (`/algo`) no começo de uma
  linha podem ser interpretados por ele; a opção que desligaria isso também desliga o modo `plan`.
- Se `PONTE_LOCAL_URL` apontar para outra máquina, o conteúdo dos arquivos resumidos vai pela rede
  até ela. Use `https` fora da sua rede local.
- No Windows, se o tempo limite estourar, só o processo `agy` é encerrado; processos que ele tenha
  aberto podem continuar até terminar sozinhos.

## Desenvolvimento

```bash
uv sync
uv run pytest
uv run ruff check .
```

Os testes não precisam de GPU, rede nem `agy`: o servidor local é simulado com
`httpx.MockTransport` e o `agy` é trocado por uma função que devolve saídas prontas.

## Licença

MIT
