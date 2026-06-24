# Session Handoff — 2026-06-24

## O que foi feito nesta sessão

### 1. Escolha do modelo LLM
Pesquisados os modelos Free do OpenCode Zen (`https://opencode.ai/zen/v1/models`). Recomendado e adotado **`opencode/deepseek-v4-flash-free`** por velocidade (variante Flash), suporte a structured outputs e instruction following. Segunda opção: `qwen3.6-plus-free`.

### 2. Migração OpenRouter → OpenCode
- **`.claude/skills/cerebras/SKILL.md`**: modelo atualizado para `opencode/deepseek-v4-flash-free`, endpoint para `https://opencode.ai/zen/v1`. Corrigidos 4 bugs: `LineLLM`→`LiteLLM`, `THe`→`The`, `thise`→`these`, `MybasemodelSubclass`→`MyBaseModelSubclass` (erro de naming de classe, alta prioridade).
- **`planning/PLAN.md`**: todas as referências a OpenRouter substituídas por OpenCode; `OPENROUTER_API_KEY` → `OPENCODE_API_KEY`; skill `cerebras-inference` → `cerebras`.

### 3. Doc review completo do PLAN.md
Rodado `/doc-review planning/PLAN.md`. Usuário respondeu a todas as 8 perguntas e 5 oportunidades de simplificação. Mudanças incorporadas diretamente no plano:

| Seção | Mudança |
|-------|---------|
| §3 | Rationale SQLite: "single-user for now, schema is multi-user-ready" |
| §5 | `OPENCODE_API_KEY` ausente → **fail fast na startup** com erro claro |
| §6 | Tabela **Timing Constants** consolidada (500ms sim, 500ms SSE, 30s snapshots) |
| §6 | SSE escopo: **watchlist atual**; novo ticker incluído no próximo tick sem reconexão |
| §6 | Correlação do simulador especificada: shared market factor + per-ticker noise com β por setor |
| §7 | "Lazy initialization" → **Startup Initialization** (antes de servir qualquer request) |
| §9 | Timeout LLM: **30s com 2 retries**; step 6 virou forward reference para *Auto-Execution* |
| §9 | Trade failure: **opção (a)** — backend humaniza o erro no campo `message`, com detalhes técnicos colapsáveis |
| §9 | Mock Mode: "OpenRouter" → "OpenCode" |
| §10 | Biblioteca de charts fixada em **Lightweight Charts** (canvas) |
| §11 | Scripts de start usarão `$(dirname "$0")` para localizar o `.env` |
| §12 | E2E tests usam **volume efêmero por run** (isolamento total do estado de dev) |

### 4. Interação LLM — confirmado
Abordagem: **request/response bloqueante**, sem streaming. Frontend faz `POST /api/chat`, mostra loading indicator, recebe JSON completo. Especificado no PLAN.md §9.

### 5. Configuração do stop hook
`.claude/settings.json` atualizado com permissões para o hook de revisão automática:
```json
"allow": ["Write(planning/REVIEW.md)", "Edit(planning/REVIEW.md)", "Bash(git *)", "Read"]
```

### 6. Git
- Branch criada: `feat/migrate-openrouter-to-opencode`
- Commit: `114e367` — "Migrate LLM provider from OpenRouter to OpenCode (DeepSeek V4 Flash)"
- Push feito para `origin/feat/migrate-openrouter-to-opencode`

---

## Estado atual do projeto

- **Fase**: Planejamento concluído. PLAN.md está completo e revisado.
- **Branch ativa**: `feat/migrate-openrouter-to-opencode`
- **Nenhum código de produto escrito ainda** — backend, frontend e Docker não existem.
- O PLAN.md em `planning/PLAN.md` é a fonte da verdade para todos os agentes.

## Próximos passos sugeridos

1. **Backend agent**: iniciar `backend/` como projeto `uv` com FastAPI; implementar DB (§7), market data/simulator (§6), API routes (§8), SSE streaming (§6).
2. **LLM agent**: implementar `/api/chat` usando a skill `cerebras` (§9) com structured outputs, timeout de 30s, 2 retries.
3. **Frontend agent**: iniciar `frontend/` como projeto Next.js; implementar layout (§10) com Lightweight Charts, SSE via EventSource, Tailwind dark theme.
4. **DevOps agent**: criar Dockerfile multi-stage (§11) e scripts `start_mac.sh` / `stop_mac.sh` (com `$(dirname "$0")` para o `.env`).
5. **QA agent**: criar `test/` com Playwright + `docker-compose.test.yml` usando volume efêmero (§12).

## Referências rápidas

| Item | Valor |
|------|-------|
| Modelo LLM | `opencode/deepseek-v4-flash-free` |
| API base | `https://opencode.ai/zen/v1` |
| Env var da chave | `OPENCODE_API_KEY` |
| Skill a usar | `cerebras` |
| Porta da app | 8000 |
| DB path (runtime) | `/app/db/finally.db` |
| Timeout LLM | 30s, 2 retries |
| Cores principais | Yellow `#ecad0a`, Blue `#209dd7`, Purple `#753991` |
