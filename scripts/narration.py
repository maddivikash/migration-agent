"""Voice-over script for the demo. Each scene: (id, narration text). The recorder plays the UI for
at least the duration of the clip, so timings follow the voice, not the other way round."""
SCENES = [
    ("intro", "This is Migration Agent. A client is moving from three legacy HR systems into a new platform. "
              "Their exports have different column names, mixed date formats, duplicates and missing fields. "
              "The agent's job is to do the migration on its own, and stop only for what it is genuinely unsure about."),
    ("run", "I click Run agent. It reads all three files, works out how each column maps onto the target schema, "
            "and explains every decision with a confidence and a reason. Dates are read as day-month or month-day "
            "based on evidence in the column itself. Exact duplicate rows are dropped. "
            "One hundred and fifteen rows become forty-eight people, with eight hundred fixes applied autonomously."),
    ("queue", "Out of all that, only seven things need a human. Each card has the raw values, the record context, "
              "and the agent's candidate answers, so I can resolve it in one glance."),
    ("q1", "Personal Email could be the work email or the manager's email. The client doesn't migrate personal emails, "
           "so I tell it not to migrate the column. The agent remembers this for next time."),
    ("q2", "Two sources disagree on a hire date. The HR system is the system of record, so I pick that value."),
    ("q3", "February thirty-first is not a real date, and the retry also failed. I type the corrected value with a note for the audit trail."),
    ("q4", "A single-token name can't be split safely. I enter the first and last name."),
    ("q5", "Department code BD is unknown. I map it to Sales. This is remembered for every future run and every future client on the same legacy tool."),
    ("q6", "The agent spotted a probable duplicate: same name and date of birth, but a typo in the email. I confirm they are the same person and it merges them."),
    ("q7", "A terminated employee with no termination date. The client confirms she is still active."),
    ("mappings", "Every mapping the agent made on its own is visible here, with confidence and reasoning, and any of them can be overridden from the dropdown."),
    ("push", "Now I push to the target. Managers go first so referential checks pass. Some records fail with a transient rate limit. "
             "A retry succeeds, because the upsert is idempotent."),
    ("rollback", "Rollback removes a record from the target in one click, and it can be re-queued."),
    ("audit", "Every record carries its full history: before, after, why, and who. Human decisions are highlighted. "
              "Every call to the target is logged with its status. The whole trail exports as CSV or JSON."),
    ("outro", "Run it again, and the questions the consultant already answered are gone. The agent gets quieter with use. "
              "That is the delta on top of the AI."),
]
