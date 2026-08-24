# Contributing

## Development

Create a Python virtual environment, install `.[dev]`, install the frontend
dependencies with `npm ci`, and run the checks from the README before opening a
pull request.

Do not commit production configuration, subscription URLs, generated files,
tokens, private keys, `.env` files or deployment state.

## Pull requests

- Explain the user-visible or operational change.
- Add or update tests for behavior changes.
- Keep deployment examples generic and reproducible.
- Run `pytest` and `npm run build` locally.

## Commit hygiene

Use task-independent commit messages. Do not include hostnames, credentials,
client names or copied production logs in commits, issues or pull requests.
