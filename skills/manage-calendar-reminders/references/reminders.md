# ios-reminders

```text
ios-reminders now
ios-reminders status
ios-reminders lists
ios-reminders list --list FULL_LIST_ID
ios-reminders list --list FULL_LIST_ID --include-completed
ios-reminders get --id Reminder/FULL_UUID
ios-reminders create --input REQUEST.json
ios-reminders complete --id Reminder/FULL_UUID --request-id stable-message-id/complete-1
ios-reminders delete --id Reminder/FULL_UUID --request-id stable-message-id/delete-1 --confirm
ios-reminders verify --id Reminder/FULL_UUID --input EXPECTED.json
```

Create JSON:

```json
{
  "request_id": "stable-message-id/reminder-1",
  "list": "FULL_LIST_ID_OR_UNIQUE_NAME",
  "title": "提交实验报告",
  "due": "2026-10-23T20:00:00+08:00",
  "notes": "附上结果图",
  "priority": "high",
  "notify": true
}
```

Priority is `none/high/medium/low` or `0/1/5/9`, with **1 = high, 9 = low**. A time-only hint is insufficient; resolve a full date and timezone. No due date is allowed when no notification is requested. Date-only due requires `all_day: true`, e.g. `due: "2026-10-23"`. This does not imply a timed alarm.

`notify: true` requires one-time owner calibration using a phone-created reminder with a timed due date and one Date notification:

```text
ios-reminders calibrate-alert --source-id Reminder/FULL_UUID
```

Calibration reads the source and learns a supported Date-trigger schema; it does not modify the source. Only trigger schemas whose fields can be safely regenerated are accepted. A schema mismatch fails before writing. The create operation submits the Reminder, Alarm and AlarmTrigger as an atomic CloudKit change, then independently reads back the reminder, alarm link, trigger type and trigger timestamp.

Server verification cannot observe iPhone notification permission, Focus mode, connectivity, account sync, or actual notification delivery. Say "已写入并验证通知记录" when that field is verified; do not claim an alert actually rang.

`list` reads the live service rather than a local reminders cache. A full ID is required for all item mutations; ID-prefix matching is deliberately not supported. The CLI supports creation, completion and deletion. Editing/removing notification times, recurrence and shared-list permission management are not implemented; use the native app for those operations rather than inventing unsupported commands.

`verify` accepts expected fields including `title`, `due`, `all_day`, `list`, `notes`, `priority`, `completed`. Include `notify: true` to also check the linked notification record.
