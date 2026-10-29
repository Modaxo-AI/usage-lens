# Contributing

Issues and pull requests are welcome.

## Before you start

- **Standard library only.** The tool runs with nothing to install; please keep
  it that way.
- **Python 3.9 or newer**, on macOS, Windows and Linux.
- **Never read prompts, replies, tool output or code.** The parser reads usage
  metadata only, and a test plants a sentinel string to prove it. See
  [Privacy](docs/privacy.md) before touching the parser or the export.
- For anything larger than a fix, open an issue first so we can agree on the
  approach.

## Making a change

1. Add or update a test with the change.
2. Run the suite:

   ```bash
   python3 -m unittest discover -s tests -t .
   ```

3. Update the docs in `docs/` if behaviour or a flag changed, and add a line
   under **Unreleased** in [CHANGELOG.md](CHANGELOG.md).

## Sign your commits

Every commit needs a `Signed-off-by` line, certifying the
[Developer Certificate of Origin](https://developercertificate.org/): that you
wrote the change, or otherwise have the right to submit it under this project's
licence. `git commit -s` adds it for you:

```
Signed-off-by: Your Name <you@example.com>
```

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
