# Codex Hummingbot Setup

## Runtime

- Hummingbot API repo: `/Users/dubski/hummingbot-api`
- API URL: `http://localhost:8000`
- MCP container API URL: `http://host.docker.internal:8000`
- Local dev credentials: `admin` / `admin`
- Docker CLI used by Codex: `/Applications/Docker.app/Contents/Resources/bin/docker`

## Codex MCP

`~/.codex/config.toml` contains:

```toml
[mcp_servers.hummingbot]
command = "/Applications/Docker.app/Contents/Resources/bin/docker"
args = [
  "run", "--rm", "-i",
  "-e", "HUMMINGBOT_API_URL=http://host.docker.internal:8000",
  "-e", "HUMMINGBOT_USERNAME=admin",
  "-e", "HUMMINGBOT_PASSWORD=admin",
  "-v", "hummingbot_mcp:/root/.hummingbot_mcp",
  "hummingbot/hummingbot-mcp:latest"
]
```

Restart Codex to load the MCP server.

## Operations

Start or update the API stack:

```bash
cd /Users/dubski/hummingbot-api
PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" make deploy
```

Stop the API stack:

```bash
cd /Users/dubski/hummingbot-api
PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" make stop
```

Verify API health:

```bash
curl -u admin:admin http://localhost:8000/
```

## Port Notes

EMQX host management was remapped from `8081:8081` to `18081:8081` because port `8081` was already used by a local Node process.

## Initial Agent Guardrails

- Treat this as paper/sandbox-first infrastructure.
- Do not add live exchange keys or wallets in the first phase.
- Require explicit user confirmation before any order, executor creation, bot start, cancel-all, leverage, or position-mode action.
- First allowed pairs: `BTC-USDT`, `ETH-USDT`, `SOL-USDT`.
- First max notional: `$100` per sandbox order.
- First max active orders: `5`.
- Always show pre-trade summary and post-trade receipt.
