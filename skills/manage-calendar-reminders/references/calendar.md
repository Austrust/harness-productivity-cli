# mailcal

All commands return UTF-8 JSON on stdout, except normal `--help`/`--version`. Login prompts go to stderr. Global options: `--profile NAME`, `--state-dir ROOT` (before the command). Do not change state-dir for retries.

```text
mailcal now
mailcal status
mailcal calendars
mailcal list --calendar FULL_ID --from 2026-10-23T00:00:00+08:00 --to 2026-10-24T00:00:00+08:00
mailcal get --calendar FULL_ID --id FULL_EVENT_ID
mailcal create --input REQUEST.json
mailcal update --input UPDATE.json
mailcal delete --input DELETE.json
mailcal verify --calendar FULL_ID --id FULL_EVENT_ID --input EXPECTED.json
```

Create JSON:

```json
{
  "request_id": "stable-message-id/event-1",
  "calendar": "FULL_ID_OR_UNIQUE_NAME",
  "title": "组会",
  "start": "2026-10-23T15:00:00+08:00",
  "end": "2026-10-23T16:00:00+08:00",
  "notes": "讨论实验结果",
  "location": "会议室",
  "alarms": [10]
}
```

`end` may be omitted; timed duration then defaults to 60 minutes. Alternatively provide positive integer `duration` in minutes. `alarms` is minutes **before** start; `[0]` means at start and `[]` means no notification. Outlook supports at most one alarm. CalDAV supports multiple DISPLAY alarms. All-day example: `start: "2026-10-23", end: "2026-10-25", all_day: true` covers the 23rd and 24th.

Update requires `request_id`, `calendar`, `id`, and at least one changed field. Moving `start` without `end` preserves duration. Only requested properties are changed; attendee and unrelated provider properties are retained. Changes to recurring events are rejected. Delete requires `request_id`, `calendar`, `id`, `confirm: true`.

`verify` input contains only expected business fields such as `title`, `start`, `end`, `all_day`, `notes`, `location`, `alarms`. It performs no write. Graph create results may have a `resource` of `request:UUID`; that locator can be used with `get`/`verify` for recovery even when the initial write response was lost. The returned `item.id` is the provider's actual event ID.

Provider setup is an owner action: iCloud/Basic-auth CalDAV uses a server URL, username and application password; Outlook/Microsoft 365 uses the owner's registered public-client app ID and device login. This CLI does not send email, add attendees, or implement SMTP/IMAP calendar synchronization.
