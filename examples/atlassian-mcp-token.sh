#!/usr/bin/env bash
#
# Print the OAuth access token that Copilot CLI already holds for the Atlassian
# MCP server, so nezha can query Jira without you minting a second credential.
#
# WHY THIS IS AN EXAMPLE AND NOT A BUILT-IN
#
#   It reads Copilot's private on-disk token store. That layout is undocumented
#   and will change without warning. It is offered as a convenience for a
#   single-user workstation, not as a deployment strategy. For anything that
#   must keep running, mint a dedicated Atlassian API token and use auth=basic.
#
# WHAT IT DOES NOT DO
#
#   Refresh. The store holds a refreshToken, but refreshing means posting to
#   Atlassian's token endpoint with Copilot's client credentials and writing the
#   result back into Copilot's store -- racing the process that owns it. Instead
#   this script fails loudly once the token expires; re-run any Copilot command
#   that touches Jira and Copilot will refresh it for you.
#
# USAGE
#
#   tracker:
#     kind: jira
#     provider:
#       auth: bearer
#       cloud_id: <uuid>
#       site_url: https://your-org.atlassian.net
#       token_command: examples/atlassian-mcp-token.sh
#
#   The matching cloud_id comes from:
#     curl -H "Authorization: Bearer $(examples/atlassian-mcp-token.sh)" \
#          https://api.atlassian.com/oauth/token/accessible-resources
#
set -euo pipefail

SERVER_URL="${ATLASSIAN_MCP_URL:-https://mcp.atlassian.com/v1/mcp/authv2}"
STORE="${COPILOT_HOME:-$HOME/.copilot}/mcp-oauth-config"

[ -d "$STORE" ] || { echo "no Copilot OAuth store at $STORE" >&2; exit 1; }

exec python3 - "$STORE" "$SERVER_URL" <<'PY'
import glob, json, os, sys, time

store, server_url = sys.argv[1], sys.argv[2]

# Entries are named <hash>.json / <hash>.tokens.json. The hash is not a plain
# sha256 of the URL, so match on the serverUrl recorded inside the companion
# file rather than trying to recompute it.
best = None
for meta_path in glob.glob(os.path.join(store, "*.json")):
    if meta_path.endswith(".tokens.json"):
        continue
    try:
        meta = json.load(open(meta_path))
    except (ValueError, OSError):
        continue
    if meta.get("serverUrl") != server_url:
        continue
    tokens_path = meta_path[:-len(".json")] + ".tokens.json"
    try:
        tokens = json.load(open(tokens_path))
    except (ValueError, OSError):
        continue
    if not tokens.get("accessToken"):
        continue
    if best is None or tokens.get("expiresAt", 0) > best.get("expiresAt", 0):
        best = tokens

if best is None:
    sys.exit("no Copilot OAuth token found for %s -- authenticate the MCP "
             "server in Copilot first" % server_url)

expires_at = best.get("expiresAt")
if expires_at and expires_at <= time.time():
    sys.exit("Copilot's Atlassian token expired at %s -- run any Copilot "
             "command that uses Jira to refresh it"
             % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)))

sys.stdout.write(best["accessToken"])
PY
