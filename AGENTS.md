# AGENTS.md instructions for catalog-classifier

## Project Scope

- This repository is the catalog image classification tool.
- Use the main `README.md` as the source of truth for the project goal, architecture, and implementation sequence.
- The README describes the target system; work on it in small steps instead of implementing large chunks at once.

## Working Roles

- Planner: product owner or architect. Responsible for scope, assumptions, and the README plan.
- Code implementor: engineer or architect. Responsible for implementing the approved plan.
- If the active role is not stated, ask the user which role they want before proceeding.

## Planning Rule

- README files are the primary planning surface for notebook work and reusable backend or frontend code.
- Put the plan, scope, assumptions, expected behavior, edge cases, non-goals, and validation notes in the relevant README before implementation begins.
- Write README content so a separate implementation agent can execute the work without guessing intent.
- Keep README files current as the plan changes.

## Ticketing Rule

- Track implementation work in the `tickets/` folder at the repository root.
- Keep one ticket per small task or vertical slice.
- Name tickets with a stable numeric prefix and a short slug, for example `tickets/0001-upload-handshake.md`.
- Each ticket should state the objective, scope, assumptions, acceptance criteria, dependencies, and validation notes.
- Update the ticket before implementation if the scope changes.
- Use tickets to break the roadmap into reviewable steps, but keep the README as the source of truth for the overall plan.

## Implementation Rule

- Do not implement code until the user's intended outcome is crystal clear.
- Do not implement code when there is ambiguity in behavior, data assumptions, scope, or expected output.
- Before any code change, write a short summary of the intended change and ask the user to approve it.
- If new ambiguity appears during implementation, stop and clarify before continuing.
- Keep each implementation step small and focused.
- For any non-notebook architecture change or reusable code change, add or update tests.

## Project References

- Core project documentation lives in `README.md`.
- Use the project skill for deeper context: `/Users/hoangdeveloper/.codex/skills/catalog-classifier/SKILL.md`
- Use the skill for guidance, but do not treat it as the only source of truth.

## Language Rule

- Avoid acronyms. When an acronym is necessary because it is a common industry term, spell out the full term first and then include the acronym in parentheses.

## Working Style

- Prefer reusable code in shared modules over ad hoc implementation.
- Keep implementation steps small and isolated.
- Favor precision over recall where the README says false merges are dangerous.
- Keep review and approval flows explicit.
