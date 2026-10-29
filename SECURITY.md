# Security

Usage Lens runs on your own machine. It reads Claude Code's local logs, serves
the dashboard on `127.0.0.1` only, and makes one network request: a daily check
of Anthropic's public pricing page (`--offline` turns it off). See
[Privacy](docs/privacy.md) for exactly what it reads and what it never reads.

## Reporting a problem

Please don't open a public issue for a security problem. Email
**madeeh.urrehman@modaxo.com** with what you found, how to reproduce it, and the
version you ran (`git rev-parse --short HEAD`, or the version shown in the
dashboard footer). You'll get a reply within five working days.

Fixes are made on the latest release only.
