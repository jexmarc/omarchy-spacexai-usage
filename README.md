# SpaceXAI usage

Omarchy **service** plugin. It does not add a bar icon.

It writes Grok and Cursor usage records into
`~/.local/state/omarchy/agents/usage/` using the same JSON contract as the
stock Claude, Codex, and Fireworks collectors. The **Agents** panel
(`omarchy.agents`) already watches that directory, so the extra tabs appear
beside those subscriptions.

- **Grok** — SuperGrok weekly pool and Grok Bot weekly allowance
- **Cursor** — Cursor Models, Other Models, On Demand, Extra usage credits

Leave Agents on the bar. Install with:

```bash
omarchy plugin add https://github.com/jexmarc/spacexai-usage.git --enable
```

Or from a local checkout:

```bash
omarchy plugin enable spacexai-usage
```

Each tab shows remaining quota from that product's authenticated usage stats
(the same numbers as Settings → Usage). Local session token counts are not
estimated.

## Credentials

Login tokens, cookies, and API keys are **not part of this repo**. They stay
in your home directory (`~/.grok/`, `~/.config/Grok Bot/`, `~/.cursor/`) and
are gitignored. Never commit those files.

The collector reads credentials already on this machine and only sends them
to the product's own usage API:

| Tab | Credential | Endpoint |
|---|---|---|
| Grok | `~/.grok/auth.json` (Grok CLI login) | `cli-chat-proxy.grok.com` |
| Grok Bot / Cursor | Grok Bot `sand-secrets.json` or `~/.cursor/auth.json` | `api2.cursor.sh` |

Tokens are never written into the Agents usage JSON, never printed, and never
sent anywhere else. Usage files are created mode `0600`.
