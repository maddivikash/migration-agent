# Demo script (≈3 minutes)

Use this as the storyboard for the demo recording. Start with a fresh state: `rm -rf .state && ./run.sh`.

1. **Run** — open <http://localhost:8000>, click **Run agent**. On the *Live feed*, point at:
   the three files being read; column mappings appearing with confidence and reason
   (`'Emp ID' → employee_id (100%)`); `'DOB' read as dd/mm/yyyy - 21 values have a first component > 12`;
   `dropped 3 exact duplicate row(s)`; `Merged 112 rows into 48 unique people`;
   `Validated 48 records; N need a human decision`.
2. **Queue** — open *Needs you* (badge shows 7). Walk through the cards from the top:
   - *Personal Email* could be `email` or `manager_email` → choose **Don't migrate this column**.
   - *hire_date differs between sources* for one person: HRIS and payroll agree, CRM differs → click the HRIS value.
   - *'31/02/2020' is not a real calendar date; retry also failed* → type `2020-02-29`, add note "confirmed with client", Apply.
   - *Split the name 'Madonna'* → type first/last, Apply.
   - *What does department value 'BD' mean?* → choose **Sales**, note "Business Development sits under Sales".
   - *Is X the same person as Y?* side-by-side with the differing email highlighted → **Same person - merge**.
   - *A terminated employee must have a termination_date* → **Actually still active**.
   After each click the agent re-runs instantly; the *Resolved* panel fills; *held* drops to 0.
3. **Mappings** — show one file: every column, confidence bar, "agent" vs "you" tag, and the
   dropdown that overrides a mapping (change `Bonus` to `salary` and back to show the re-run).
4. **Push** — click **Push ready records**. Feed shows managers pushed first, a few `FAIL ... rate limited`
   (503, transient) and `DEFER` lines for reports whose manager is held. *Records* tab: filter `failed`,
   click **Retry failed** → all succeed. Show a 422 (`manager_email ... does not exist`) if present and
   **Retry failed without manager**.
5. **Rollback** — on a pushed record click **Roll back**; header's *in target* count drops by one; the
   record shows `rolled_back` and can be **Re-queued**.
6. **Audit** — open a record drawer: every change with before/after/why/actor (human rows in blue) and
   the target calls for that record. Then the *Audit trail* tab and **Changes CSV** download.
7. **Memory** — click **Run agent** again: `BD → Sales` and the `Personal Email` drop are now applied
   from mapping memory, and the queue is shorter.
