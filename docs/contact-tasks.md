# Contact tasks

The MCP server provides `create_contact_task`, `list_contact_tasks` and `manage_contact_task`. A host can create a reminder with its conversation basis, inspect the current revision and delivery state, and pause, resume, reschedule, cancel or confirm it. The Python `ContactTasks` interface uses the same implementation. `schedule_contact` remains available for scheduling an existing memory record.

## Configure a policy

Choose a private database, scope and policy ID. Configure the policy through the console or HTTP API before creating tasks. This example creates suggestions without enabling delivery:

```sh
eventmem api PUT /v1/contact/policies --json '{"id":"companion-reminders","scope":{"project":"personal","persona":"companion","collection":"default","world":"real"},"timezone":"Asia/Singapore"}'
```

An unknown policy, a different scope or a disallowed trigger is rejected before creating a source. New tasks store `reminder` records, so the policy must allow that kind. A policy may remain disabled while tasks are prepared and reviewed. Task tools do not enable policies or configure delivery credentials.

Actual delivery requires a running `eventmem serve` worker and a configured host callback. The host owns its recipient selection, authentication and channel behavior. A WeChat host supplies its own owner-bound callback; MemoryPalace does not contain WeChat login credentials or a transport. See [contact callbacks](operations.md#contact-callbacks).

## Create and inspect a task

Call `create_contact_task` with this shape. Replace the example time with the intended time, including its UTC offset:

```json
{
  "scope": {"project":"personal","persona":"companion","collection":"default","world":"real"},
  "task": {
    "command_id": "appointment-reminder-unique-id",
    "policy_id": "companion-reminders",
    "title": "Appointment reminder",
    "text": "The appointment starts at nine.",
    "basis": "The user requested a reminder for the appointment.",
    "due_at": "2099-01-01T09:00:00+08:00",
    "trigger": "reminder",
    "recurrence": "none",
    "authority": "model"
  }
}
```

`text` is the message sent at the due time, bounded to 6,000 characters. `basis` records why the task exists. `authority` describes the origin of that text: `model` for assistant composition, `explicit` for the user's own supplied text, or `operation` for an observed operation. User authorization to create a reminder does not make model-authored wording a user quotation.

Use a command ID unique within the database and reuse it with the same request when retrying. Reusing it with changed text, provenance or scheduling fields produces a conflict. Interrupted creation can resume without duplicating its source or schedule. A repeat creation request returns the task's current state and revision, including later cancellation or rescheduling.

Supported recurrence values are `none`, `daily`, `weekly` and `yearly`; recurrence follows the policy timezone. Trigger choices are `reminder`, `commitment`, `anniversary`, `checkin` and `greeting`, subject to the policy's allowed triggers. Use separate policies when user-requested reminders and autonomous check-ins need different quiet hours, rates or confirmation settings.

Call `list_contact_tasks` with the same `scope`, a `policy_ids` array and an optional `limit`. It returns up to 100 tasks, newest due time first, with bounded text, source IDs, the current revision and the latest delivery states. Deleted source records are excluded. This bounded list does not replace the console or paginated HTTP contact history.

## Change and deliver a task

Call `manage_contact_task` with `scope`, `policy_ids`, `task_id`, `expected_revision` and `action`. Read the current revision before changing it. A revision conflict requires another read rather than overwriting a newer change.

| Action | Effect |
|---|---|
| `pause` | Pause the schedule and cancel pending deliveries |
| `resume` | Resume the schedule using its current due time |
| `snooze` | Set a new timezone-aware `due_at` and schedule it |
| `cancel` | Cancel the schedule and pending deliveries |
| `confirm` | Approve a suggestion; retain all policy and channel checks |

`pause`, `resume`, `snooze` and `cancel` also return `deliveries` and `reconciliation_required`. Each pending delivery they stopped is listed with an `outcome`: `canceled` when nothing was sent, or `possibly_sent` when its request was already on the network or an earlier attempt ended without a definite refusal. Such a delivery stops retrying, stays `uncertain` until its receipt or an acknowledgment decides it, and sets `reconciliation_required`. A cancellation never reports a message as withdrawn unless it was. One rule decides this for every path that stops a schedule; [active contact](architecture.md#active-contact) describes how a delivery is claimed, dispatched, sent and settled.

Only `snooze` accepts `due_at`. A `scheduled` receipt confirms storage, not sending. A delivery marked `suggested` awaits policy or confirmation requirements. A `sent` delivery records a successful callback; the callback must acknowledge only after its channel confirms the effect. Because the request is out while no transaction is held, that acknowledgment can arrive before its own receipt does: a delivery whose request is on the network accepts it, and the late receipt then finds no row to change. Failed or uncertain delivery remains visible for reconciliation. Follow the [callback idempotency contract](operations.md#contact-callbacks) to prevent repeated external effects.

Scope and policy selection provide query isolation within the single-user database. They are not access control for unrelated memory tools. Host-specific wrappers can fix the scope and policy IDs and expose their own task aliases:

```python
from eventmem.core import Engine
from eventmem.core.contact_tasks import ContactTaskInput, ContactTasks
from eventmem.core.models import Scope

tasks = ContactTasks(
    Engine("/private/memorypalace"),
    Scope(project="personal", persona="companion"),
    ["companion-reminders"],
)
result = tasks.create(ContactTaskInput(
    command_id="appointment-reminder-unique-id",
    policy_id="companion-reminders",
    title="Appointment reminder",
    text="The appointment starts at nine.",
    basis="The user requested a reminder for the appointment.",
    due_at="2099-01-01T09:00:00+08:00",
))
```

These tools manage MemoryPalace contact tasks. Host application automations remain under the host's own scheduling tools.
