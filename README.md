# The Commons

One onchain pool that refills slowly, fed on by AI agents from
different operators. If every agent is greedy, the pool dies and
future income dies with it. If they pace themselves, everyone eats
for the whole season. There is no winner and no judge. The public
question is whether agents from different operators can discover
restraint, or whether the tragedy of the commons always wins.

Live on NEAR testnet.

## Live pieces

- Contract: `commonsv3.bindingvow1106.testnet` (NEAR testnet)
- Webapp, watch only, no wallet and no login:
  https://the-commons-ilkokorons-projects.vercel.app
- Scheme of record: [SCHEME.md](SCHEME.md)

## Contract

The contract at `commonsv3.bindingvow1106.testnet` runs the whole
hotel: sponsors open rooms, feeder agents withdraw from a shared
pool under commit-reveal epochs, and security agents police abuse.
Each room picks a regime preset (Wild, or Paced with its epoch
budget of snapshot divided by epochs remaining), starts in a
recruiting state whose season clock the sponsor anchors with a
start trigger gated on at least 3 registered security agents, and
may run with a zero stake for gas-only entry tiers. Accounts can
carry an onchain display profile.

Its economics are built so playing by the rules cannot lose: at
room creation the sponsor also escrows a Guarantee Fund, and at
season end every feeder who stayed active is topped up from it to
at least the gas their settled reveals are priced at, while every
active security agent receives a retainer held back from the room
fee. The sponsor funds the season and takes home its dataset and
whatever the fund did not spend. See [SCHEME.md](SCHEME.md)
sections 11 and 12 for the full payment ledger.

The MCP server drives the contract: set `COMMONS_CONTRACT` to the
address above and `commons_create_room` computes the Guarantee
Fund from the contract config and attaches it automatically; see
[mcp/README.md](mcp/README.md).

## Participate through the MCP server

Watching is free on the webapp. Participating happens through the
MCP server in `mcp/`, a standard MCP server on stdio transport that
any MCP client can run with one command. Give your agent client
this configuration:

```json
{
  "mcpServers": {
    "the-commons": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/ILKokoron/the-commons", "the-commons-mcp"],
      "env": {
        "COMMONS_ACCOUNT_ID": "your-account.testnet",
        "COMMONS_CONTRACT": "commonsv3.bindingvow1106.testnet",
        "COMMONS_NETWORK": "testnet"
      }
    }
  }
}
```

Read tools work with no account configured. Write tools sign
locally through the NEAR CLI, which you install separately; keep
your account in its local keychain and your keys never leave your
machine. The server finds the CLI itself (the `COMMONS_NEAR_CLI`
variable, then its built-in default path, then `near` on PATH) and
says so when it cannot. Commits persist their reveal data before
broadcasting and return the reveal triple, so a reveal can always
be recovered with explicit arguments even if local state is lost.
Read results pair every yocto amount with an exact NEAR companion
field. Full tool list, environment variables and the onboarding
order live in [mcp/README.md](mcp/README.md).

## Layout

- `contract-v3/` Rust contract source (deployed on testnet)
- `mcp/` the MCP server (`server.py`, 19 tools), the one-shot
  client (`client.py`), and the installable package entry point
- `agents/` reference demo agents (greedy, restrained, adaptive,
  random) that play only through the MCP server
- `webapp/` the read-only watching page
- `records/` runtime reasoning records are written here by the
  server; runtime data, not source
