# Secret scanning and rotation

The repository is public. Treat any real credential committed to any Git
revision as exposed, even if a later commit deletes it. Rotate it with its
provider, update deployment secret storage, and verify the old credential no
longer works. Do not paste secret values into issues, logs, reports, or chat.
This checklist does not rotate credentials or rewrite Git history.

## Rotation checklist

- [ ] `POSTGRES_PASSWORD`: rotate the database role password and update
  `DATABASE_URL` in every deployment.
- [ ] `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`: rotate the MinIO user/key pair
  and update the matching client configuration.
- [ ] `ENGINE_API_TOKEN`: issue a new random token, update API and GUI process
  environments together, then revoke the old token. Keep
  `ENGINE_API_AUTH_BYPASS=0`.
- [ ] `HELIUS_API_KEY`, `ALCHEMY_API_KEY`, and `ETHERSCAN_API_KEY`: rotate at
  each provider and update the corresponding deployment environment.
- [ ] `QUICKNODE_EVM_RPC_URL`: if it contains a provider credential, revoke the
  endpoint/key and replace the URL in deployment configuration.
- [ ] `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`: regenerate the app
  credentials and update the crawler environment.
- [ ] `TELEGRAM_API_HASH`: regenerate the Telegram application credential;
  review and replace the associated `TELEGRAM_API_ID` configuration if the
  application itself was exposed. Revoke and recreate any affected Telegram
  session file.
- [ ] `NTFY_TOPIC`: if the topic name is used as a private bearer-like channel,
  create a new private topic and update every publisher and subscriber.

Also inspect `DATABASE_URL` and any provider URL for embedded credentials.
The literal `serpent` and `minioadmin` values in `.env.example` are documented
development placeholders only; replace them in every real deployment.

## Gitleaks policy

`.gitleaks.toml` extends Gitleaks' default rules. Its narrow allowlists match
only the literal placeholder values `serpent` and `minioadmin` on
`.env.example`, plus the fixed fake webhook signing fixture in
`tests/test_api_auth_webhooks.py`. The allowlists require both the expected
path and exact value; they do not exempt either file wholesale.

The initial full-history scan examined all 75 reachable commits (about 3.06 MB)
and reported two `generic-api-key` matches in the webhook signature tests.
They are fixed test fixtures, not provider credentials; their exact value is
not reproduced here. The documented exact-value allowlist makes the full scan
clean while preserving detection of other values in those files.

The pre-commit hook scans staged changes with redacted output. Install Gitleaks
and the repository hooks with:

```bash
pre-commit install --install-hooks
```

CI checks out the full history (`fetch-depth: 0`) and runs Gitleaks on pushes
to `main` and pull requests targeting `main`. To scan a complete local clone
manually:

```bash
gitleaks git --redact --config .gitleaks.toml --log-opts='--all' .
```

If `git rev-parse --is-shallow-repository` reports `true`, fetch the missing
history with `git fetch --unshallow` before scanning. Findings are redacted by
default in all documented commands.
