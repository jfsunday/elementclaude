Closes #1

Automated via auto-code.

## Plan
### Goal
When a plan is approved, let the user pick the follow-up mode (manual/default or auto) instead of silently staying stuck in `plan`, and make `!clear` reset the room's mode back to the configured default.

### Files to touch
- `app/agent/permissions.py` — `handle_exit_plan_mode()` currently only offers ✅ approve / ❌ reject. Extend the plan-approval prompt/reactions to three outcomes: approve+manual (default mode), approve+auto (auto mode), reject. Also `handle_reaction_event()`/`handle_text_answer()` for `pending.kind == "plan"` need to recognize the new choice and carry the chosen mode back to the caller.
- `app/agent/session.py` — `handle_prompt()` (or wherever `can_use_tool`/`handle_exit_plan_mode` result is consumed) needs to persist the chosen mode via `upsert_room(room_id, mode=...)` once approval resolves, so the DB and the in-memory `RoomSession.mode` both leave `plan`. `clear_room()` should also reset `mode=settings.default_mode` when clearing.
- `app/commands/builtin.py` — `cmd_clear()` currently just calls `clear_room()`; confirm the reset-mode message reflects the new default mode reported back to the user ("🧹 session cleared, mode → default").
- `app/rooms/state.py` — no logic change expected, just confirms `upsert_room` already supports `mode=` (it does).

### Approach
1. In `permissions.py::handle_exit_plan_mode`, change the approval prompt to three reactions: ✅ (approve, switch to `default`/manual mode), 🚀 (approve, switch to `auto` mode), ❌ (reject). Keep free-text feedback path for "iterate" working as today.
2. Update `Pending`/decision payload for `kind == "plan"` to carry a `next_mode` field (`"default"` or `"auto"`) alongside `decision`.
3. Update `handle_reaction_event` and `handle_text_answer` for the `plan` kind to map the new reaction/keywords to `{"decision": "allow", "next_mode": "default"|"auto", ...}`.
4. In `handle_exit_plan_mode`, after receiving an `allow` decision, call `upsert_room(room_id, mode=next_mode)` directly (permissions.py already touches the DB via `app.rooms.state.audit`, so importing `upsert_room` there is consistent) before returning `PermissionResultAllow`. This ensures the DB is correct immediately.
5. In `session.py`, after `can_use_tool` resolves the ExitPlanMode approval and mode changed underneath us, make sure `_get_session`'s next pass picks up `room.mode` and calls `sess.client.set_permission_mode(...)` so the *current* SDK client also matches — otherwise the live client stays in plan mode until the process reconnects. Since `_get_session` is only invoked at the top of `handle_prompt` (once per user prompt, before the run), and the mode change happens mid-run (inside `can_use_tool` during the same run), also directly call `sess.client.set_permission_mode(sdk_mode(next_mode))` and update `sess.mode` right after the DB write in step 4 (needs access to the in-memory `RoomSession` — add a small helper in `session.py`, e.g. `set_room_mode(room_id, mode)`, that `permissions.py` calls instead of writing to `upsert_room` directly, to keep both DB and live SDK client in sync in one place).
6. In `session.py::clear_room()`, add `mode=settings.default_mode` to the `upsert_room(room_id, claude_session_id=None, ...)` call so `!clear` always resets mode.
7. Update `cmd_clear` reply text to mention the mode reset (e.g. "🧹 session cleared → mode `default`").
8. Update the plan-approval message text in `handle_exit_plan_mode` ("✅ approve plan · ❌ reject") to explain the three options clearly for Element users, and add the third reaction emoji.

### Tests / verification
- No test suite exists in this repo (per CLAUDE.md); this is a webhook/Matrix-driven bot with no unit tests.
- Manual verification: run `./run-host.sh`, from a whitelisted Matrix DM: `!mode plan`, prompt Claude to do something requiring a plan, approve with the "manual" reaction and confirm `!status` shows `default`; repeat approving with "auto" reaction and confirm `!status` shows `auto`; confirm a follow-up prompt actually runs without further approval prompts in auto mode, and with per-tool approval prompts in default mode.
- `!clear` then `!status` should show mode reset to `settings.default_mode` regardless of what it was before.
- Never touch/restart the systemd service — user restarts manually after pulling (per CLAUDE.md and issue body).

### Out of scope
- Not changing the meaning/mapping of the four existing modes (`default`/`acceptEdits`/`plan`/`auto`) in `app/agent/modes.py`.
- Not adding a fourth "deny and reset mode" behavior — reject just denies the plan and leaves mode as `plan` (user can re-approve or `!mode` manually), since the issue only mentions "yes+manual", "yes+auto", "deny".
- Not touching `AskUserQuestion` handling or regular per-tool `_approval_flow` (✅/❌/🔁) — issue is specifically about the plan-approval flow and `!clear`.
- Not adding persistence/migration for existing rooms currently stuck in `plan` mode from before this fix — user can run `!mode default` once to unstick them.
- No systemd service changes, per explicit instruction in the issue and CLAUDE.md.

## Review passes
### Pass 1
#### Verdict — one sentence
The implementation faithfully fulfills both parts of issue #1 (three-way plan-approval reactions with mode switch, and `!clear` resetting mode) with clean, consistent code and no test suite regressions to run.

#### What was requested vs what was built — bullet mapping
- "approve plan → pick manual (default) or auto mode, or deny" → `handle_exit_plan_mode()` now posts ✅ (approve, manual/default) · 🚀 (approve, auto) · ❌ (reject), and `handle_reaction_event`/`handle_text_answer` for `kind == "plan"` carry a `next_mode` (`"default"` or `"auto"`) back through the resolved future.
- "mode must actually change after approval" → new `session.py::set_room_mode()` writes `upsert_room(room_id, mode=...)` **and** updates the live in-memory `RoomSession.mode` + calls `sess.client.set_permission_mode(sdk_mode(mode))` on the already-connected SDK client, so the room leaves `plan` mode both in the DB and in the currently running session — not just on next reconnect. Called from `handle_exit_plan_mode` right after an `allow` decision, via a deferred import (avoids circular import with `session.py` importing `permissions.py` at module level).
- "`!clear` never resets mode" → `clear_room()` now passes `mode=settings.default_mode` to `upsert_room`, and `cmd_clear` reply text was updated to `"🧹 session cleared → mode \`{settings.default_mode}\`"` to reflect it.
- "Never touch the systemd service" → no service/unit-file changes in the diff; confirmed.

#### Bugs / gaps — list, empty if none
(none blocking)

- Minor/theoretical: `set_room_mode()` is invoked *while* the run is mid-stream (from inside `can_use_tool` during `sess.client.receive_response()`), unlike the pre-existing `_get_session` mode-sync logic which only runs before a query starts. If `sess.client.set_permission_mode()` raises, the except branch calls `await _disconnect(sess)` (which awaits `sess.client.disconnect()`) on the very client object that's currently iterating `receive_response()` in the enclosing `_run_prompt` loop — this could throw/interfere with the in-flight generator. This is an exception-path-only edge case (the SDK's `set_permission_mode` is designed to be called on a connected client, so it should not normally fail) and not something the issue asked to guard against, so not blocking, just worth being aware of if odd behavior is ever reported right after picking 🚀 on a plan.

#### Style / conventions — list, empty if none
- Follows repo conventions: `from __future__ import annotations`, async-first, mode changes routed through `app/rooms/state.py::upsert_room`, `app/agent/modes.py::sdk_mode()` kept as the single source of truth for SDK mode mapping (no hardcoded SDK strings introduced).
- Deferred import of `set_room_mode` inside `handle_exit_plan_mode` to sidestep the `session.py` ↔ `permissions.py` circular import — consistent with how the codebase already handles similar cases (e.g. `_load_hooks_for_room`'s local imports).
- `py_compile` passes on all three touched files; no test suite exists in this repo (per CLAUDE.md), so none was run.

#### Suggested fixes — actionable list, only if verdict is NEEDS_FIX
N/A — verdict is OK.

