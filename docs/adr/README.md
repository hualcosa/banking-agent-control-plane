# ADRs

Uma decisão entra aqui quando (a) alguém pode discordar dela com bons argumentos e (b) existe evidência — um teste, um número, um diff — de que ela foi tomada e não apenas escrita.

| # | Decisão | Evidência |
|---|---|---|
| [0001](0001-ownership-por-canal.md) | Ownership: sessão para consentir, cliente para o resto | testes de isolamento em `test_control_plane.py`, `test_voice.py`, célula `borrowed_token` |
| [0002](0002-storage-seam.md) | Um `Store`, duas implementações, síncrono | `git diff` vazio nos 33 testes antigos; 8 testes de integração |
| [0003](0003-confirmacao-ttl-e-digest.md) | Confirmação expira; a ação é selada com HMAC | células `expired_yes` e `mutated_action`, mutation testing |

## Ainda sem ADR, e por quê

| Decisão | Bloqueada por |
|---|---|
| Residência de dado em `sa-east-1` (T20) | Precisa de latência medida contra Bedrock — marco 4 |
| Observabilidade deployada: AgentCore × Langfuse × LangSmith (T28) | Precisa dos três rodando com o mesmo tráfego |
| Control plane próprio × AgentCore Policy (T6 do roadmap) | Precisa do Gateway + Cedar em `LOG_ONLY` com CREATE_PIX espelhado |
| Modelo para extrair a ação tipada (T26) | Precisa do golden set congelado e de três modelos reais |

Nenhuma delas é escrevível hoje sem inventar um número, e um ADR que aponta para um número inventado é pior que nenhum ADR.
