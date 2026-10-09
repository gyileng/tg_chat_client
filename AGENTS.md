# Repository Guidelines

## Project Structure & Module Organization

This project is a standalone Windows client for manually chatting through multiple Telegram accounts. It is currently in requirements and technical research; the application framework, connection layer, and packaging approach remain undecided.

- `README.md`: project summary, confirmed scope, and entry points.
- `docs/RESEARCH.md`: requirements, proposed interactions, research references, and open decisions. Read it before making changes.
- `../tg_bot`: a neighboring reference project, not an established runtime dependency.

There are no source, test, or asset directories yet. Document their locations when implementation begins.

## Build, Test, and Development Commands

No build, application launch, test, formatter, or lint commands are configured. Do not assume a Python or JavaScript toolchain.

Prefix shell commands with `rtk`, following `/Users/baiweispace/.codex/RTK.md`. Useful inspection commands include:

- `rtk ls`: inspect the current directory.
- `rtk rg --files`: list project files.
- `rtk read README.md docs/RESEARCH.md`: review project context.
- `rtk proxy <command>`: run commands without RTK filtering.

Add verified setup and development commands once the technology stack is selected.

## Coding Style & Naming Conventions

Keep Markdown concise, with descriptive headings, relative links, and explicit distinctions between confirmed requirements and proposals. Existing project documentation is in Chinese; maintain that language when editing it. No code indentation or naming standard has been selected; establish language-specific conventions alongside the first implementation.

## Testing Guidelines

No testing framework or coverage threshold exists yet. For documentation changes, check links and consistency with confirmed scope. Future prototype checks should cover account and group identity, draft preservation, one message per manual trigger, account switching during sends, and understandable failure feedback. Keep local send counts separate from group-recognized counts. Document automated test naming and execution when tests are introduced.

## Commit & Pull Request Guidelines

This directory has no Git metadata, so there is no commit history to derive conventions from. Use concise, imperative commit subjects. Pull requests should explain the change, relevant requirement or decision, and validation performed; include screenshots for UI changes and linked issues when available.

## Security & Product Boundaries

Keep credentials, sessions, account details, and real chat content out of version control and research documents. Use independent sessions and storage from `tg_bot`. Every send requires a user action; automatic loops, scheduled sending, and language-model generation are outside the confirmed first-version scope.
