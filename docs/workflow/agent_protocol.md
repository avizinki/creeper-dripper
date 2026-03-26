# Agent communication protocol (USER · CLAUDE · CURSOR · CHATGPT)

Shared protocol so copy-pasted messages stay unambiguous across operator, reasoning, execution, and auxiliary chat.

**ChatGPT is a first-class participant** — never treat it as implicit or nameless.

---

## Required message format (pasted / shared messages)

Every pasted or shared message **MUST** begin with these lines **in order**:

```
[TO:<target>]
[FROM:<sender>]
[AGENT:<active agent>]
[TASK:<id or none>]
[MODE:<analysis | implementation | validation | instruction>]
```

### Allowed values for TO / FROM / AGENT

| Value | Role |
|-------|------|
| `USER` | Operator; source of authority |
| `CLAUDE` | Reasoning / implementation in Anthropic Claude (no repo execution authority) |
| `CURSOR` | Execution / validation in Cursor (this IDE agent) |
| `CHATGPT` | OpenAI ChatGPT (or equivalent) — planning, drafting, review; **not** repo truth |

- **`[TO:…]`** — who should **act on** or **read** this message next.
- **`[FROM:…]`** — who **authored** this copy of the message.
- **`[AGENT:…]`** — which participant’s **role** is active for this turn (often matches `FROM`, not always).

`TASK` is a task id (e.g. `T-003`, `BATCH-20260326-night`) or the literal `none`.

### Modes

| Mode | Typical use |
|------|-------------|
| `analysis` | Investigate, explain, no implementation |
| `implementation` | Code/docs changes (usually Claude or Cursor) |
| `validation` | Run gates, tests, report results (usually Cursor) |
| `instruction` | Directives, overrides, process |

---

## Examples

**User instructing Claude**

```
[TO:CLAUDE]
[FROM:USER]
[AGENT:USER]
[TASK:none]
[MODE:instruction]
```

**ChatGPT instructing Cursor**

```
[TO:CURSOR]
[FROM:CHATGPT]
[AGENT:CHATGPT]
[TASK:none]
[MODE:instruction]
```

**Claude reporting to user**

```
[TO:USER]
[FROM:CLAUDE]
[AGENT:CLAUDE]
[TASK:T-005]
[MODE:implementation]
```

**Cursor validating for ChatGPT**

```
[TO:CHATGPT]
[FROM:CURSOR]
[AGENT:CURSOR]
[TASK:T-005]
[MODE:validation]
```

---

## Protocol rules

1. **Every pasted/shared message MUST begin with:** `[TO:…]`, `[FROM:…]`, `[AGENT:…]`, `[TASK:…]`, `[MODE:…]` (in the order above).

2. **Every reply MUST end with:**  
   `From: <agent>`  
   (single line; `<agent>` is one of `USER` | `CLAUDE` | `CURSOR` | `CHATGPT`.)

3. **Every agent must explicitly recognize both:**
   - **sender** (who wrote the message being answered)
   - **target** (who the reply is for)

   Example lines at the start of a reply:

   - `Recognized sender: USER`
   - `Intended recipient: CLAUDE`

4. **CHATGPT** is listed explicitly in TO/FROM/AGENT wherever that tool is involved — do not omit.

5. **If `TO` or `FROM` is missing or ambiguous:** refuse execution (where applicable), do not bind tasks, and **request the corrected header format**.

---

## Rules by participant (summary)

| Participant | Constraints |
|-------------|-------------|
| **CLAUDE** | Must not execute shell/repo commands; must not commit. Recognize sender + target. |
| **CURSOR** | Executes only when `MODE=validation` or `MODE=instruction` with explicit scope; recognize sender + target. |
| **CHATGPT** | Does not hold repo truth; outputs are drafts until validated in-repo by USER/CURSOR/CLAUDE per workflow. |
| **USER** | Authority of last resort; can override prior instructions. |

---

## Workflow JSON (not chat)

Chat messages use **TO/FROM** headers as above.

Workflow handoff files (e.g. `docs/workflow/claude_task_result.json`) should include:

| Field | Meaning |
|-------|---------|
| `agent` | Who prepared the payload (e.g. `CLAUDE`) |
| `prepared_for` | Optional but recommended: who should run validation next (e.g. `CURSOR`) |

Example:

```json
{
  "agent": "CLAUDE",
  "prepared_for": "CURSOR",
  "task_id": "T-005",
  "status": "ready_for_cursor_validation"
}
```

`docs/workflow/claude_task_result.json` remains the **contract** for task validation when `status=ready_for_cursor_validation`. The chat header `[TASK:…]` SHOULD align with `task_id` when both exist.

---

## Enforcement

See **`.cursor/rules/claude-task-validation.mdc`**. Runtime enforcement is rule-based; operators and agents must comply when pasting across systems.
