# Safety and rollback

## Trigger behavior

The installed trigger runs before inserts into `logs` and uses `RAISE(IGNORE)` only when
`upper(NEW.level)` is `TRACE` or `DEBUG`. SQLite abandons that row without returning a
constraint error. Other levels continue through the normal insert path.

Expected side effects:

- TRACE and DEBUG diagnostic history is lost after activation.
- INFO, WARN, and ERROR remain available.
- Ignored AUTOINCREMENT attempts may leave gaps in IDs.
- The trigger reduces row and index writes but does not remove SQL call or trigger-evaluation cost.
- Retained logs, checkpoints, and unrelated Codex databases can still write to disk.

## Backup

Use SQLite's online backup API. Do not copy only the main database while WAL mode is active.
The script writes timestamped backups under `~/.codex/backups/` by default and verifies the
backup with `PRAGMA quick_check`.

## Rollback

The normal rollback is:

```sql
DROP TRIGGER codex_drop_trace_debug_logs;
```

Use the script instead of issuing SQL manually because it verifies that the trigger definition
matches the expected definition:

```powershell
python scripts/codex_log_guard.py uninstall --yes
```

Restoring the whole backup is a last resort. Close all Codex processes first and preserve the
current database before replacing files. Do not restore an old log database merely to remove
the trigger.

## Stop conditions

Do not modify the database if:

- `PRAGMA quick_check` is not `ok`;
- required columns are absent;
- another trigger targets `logs`;
- the named trigger exists with different SQL;
- the online backup fails;
- the database appears to contain non-log application data in the target table.
