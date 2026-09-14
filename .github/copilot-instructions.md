# Public repository safety

Treat this repository, its Git history, issues, pull requests, releases, and
published build artifacts as public-facing.

- Never commit credentials, tokens, cookies, private keys, connection strings,
  authentication material, or unredacted secrets.
- Do not add real personal or confidential data to tracked files, fixtures,
  documentation, commit messages, branch names, issues, or pull requests. This
  includes personal email addresses other than an approved GitHub noreply
  address, local usernames and home paths, private service URLs, customer or
  internal project names, real session IDs, transcripts, and logs.
- Use clearly synthetic examples such as `example.invalid`, `/home/user`,
  `C:\Users\user`, and fabricated IDs.
- Keep generated files, caches, runtime state, screenshots containing private
  data, and local configuration out of Git.
- Runtime session metadata may be processed locally for the documented product
  behavior, but it must not be transmitted, committed, or added to diagnostics
  without explicit user approval and appropriate redaction.
- Use the configured GitHub noreply address for commits.
- Before committing or pushing, inspect the complete diff and run the
  repository's secret scanner. For public-release checks, scan the full reachable
  history as well.
- If completing a task requires sensitive data, keep it outside the repository
  and ask before placing any form of it in a public-facing artifact.
