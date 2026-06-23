---
name: codex-sqlite-log-guard
description: Diagnose sustained high-frequency writes to Codex logs_2.sqlite, quantify TRACE and DEBUG activity, generate a risk-aware remediation plan, install or remove a reversible SQLite trigger that drops only TRACE and DEBUG rows, and run transactional and live self-checks. Use when Codex SQLite logs, WAL growth, disk writes, TRACE logging, logs_2.sqlite, or the codex_drop_trace_debug_logs trigger are being investigated or repaired.
---

# Codex SQLite Log Guard

Use `scripts/codex_log_guard.py` for deterministic inspection and changes.

## Safety contract

- Default to `inspect` or `plan`; both are read-only.
- Do not install or remove a trigger unless the user explicitly requests the change.
- Never suppress `INFO`, `WARN`, or `ERROR`.
- Require `PRAGMA quick_check = ok`, the expected `logs` schema, and a successful online backup before changing schema.
- Refuse unknown or conflicting triggers instead of replacing them.
- Do not delete, truncate, vacuum, or upload the database or its contents.
- Treat the trigger as a workaround. Prefer an official logging control when one is verified for the installed Codex version.

## Workflow

1. Locate the database, defaulting to `~/.codex/logs_2.sqlite`.
2. Inspect integrity, schema, trigger state, level distribution, WAL files, and a short live sample:

   ```powershell
   python scripts/codex_log_guard.py inspect --sample-seconds 12
   ```

3. Generate a recommendation without modifying anything:

   ```powershell
   python scripts/codex_log_guard.py plan --sample-seconds 12
   ```

4. Explain the consequences before installing:
   - TRACE and DEBUG rows will be permanently absent after installation.
   - INFO, WARN, and ERROR remain available.
   - SQLite still evaluates the trigger and retained levels still write to WAL.
   - Existing database size is unchanged.
   - Future Codex migrations may remove or conflict with the trigger.

5. After explicit approval, install and verify:

   ```powershell
   python scripts/codex_log_guard.py install --yes --sample-seconds 10
   ```

6. Check status or rerun self-tests:

   ```powershell
   python scripts/codex_log_guard.py self-test --sample-seconds 10
   ```

7. Remove only this skill's exact trigger after explicit approval:

   ```powershell
   python scripts/codex_log_guard.py uninstall --yes
   ```

## Interpretation

- Use inserted-row counts and level distribution as the primary evidence.
- A stable WAL file size does not prove there are no writes; WAL pages may be reused in place. Check modification times as supporting evidence.
- AUTOINCREMENT IDs can have gaps after ignored inserts. Do not treat ID gaps as data corruption.
- A live sample with no Codex activity is inconclusive. Trigger normal Codex activity and sample again.
- If schema validation fails, stop. Do not adapt the trigger by guessing.

Read [references/safety-and-rollback.md](references/safety-and-rollback.md) when explaining risks, recovery, or version compatibility.
