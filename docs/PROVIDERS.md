# Hermes Providers

## Policy

Aegis Alpha uses Hermes as the AI runtime. Hermes needs at least one inference provider before it can use Aegis Alpha MCP tools or skills.

Current policy:

- Primary provider: OpenRouter.
- Fallback provider: DeepSeek direct with `deepseek-v4-pro`.
- Do not call DeepSeek through OpenRouter when the direct DeepSeek provider is available.

## Files

Full reproducible Hermes config template:

```text
.hermes/config/config.example.yaml
```

Install or replace local Hermes config from the project template:

```bash
scripts/install_hermes_project_config.sh --replace
```

This is the recommended path. It includes provider policy, fallback provider, and the Aegis Alpha MCP server. It also resolves the MCP command path for the current checkout.

Advanced provider-only config example:

```text
.hermes/config/providers.deepseek-openrouter.example.yaml
```

Hermes env example:

```text
.hermes/env.example
```

Advanced provider-only installer:

```bash
scripts/install_hermes_provider_config.sh
```

By default this copies supported provider keys from project `.env.local` into `~/.hermes/.env` without printing values:

```text
DEEPSEEK_API_KEY
OPENROUTER_API_KEY
```

Disable copying when needed:

```bash
scripts/install_hermes_provider_config.sh --no-sync-from-local-env
```

Check provider state:

```bash
scripts/check_hermes_provider.sh
```

## Manual Setup

Copy provider keys into Hermes env:

```bash
mkdir -p ~/.hermes
cp .hermes/env.example ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

Then edit:

```text
DEEPSEEK_API_KEY=your-deepseek-key
OPENROUTER_API_KEY=your-openrouter-key
```

`OPENROUTER_API_KEY` is optional. Use it for non-DeepSeek model contrast or fallback.

## Hermes Config

Recommended default:

```yaml
model:
  provider: "openrouter"
  default: "openai/gpt-5.4"
  base_url: "https://openrouter.ai/api/v1"
  api_mode: "chat_completions"
```

Fallback:

```yaml
fallback_providers:
  - provider: "deepseek"
    model: "deepseek-v4-pro"
```

## References

- Hermes AI Providers: https://hermes-agent.nousresearch.com/docs/integrations/providers
- Hermes Fallback Providers: https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers/
