# Writing an Ingestion Adapter

Every memory source is an adapter over the same write path. There is no
adapter framework to learn: an adapter is any program that calls
`remember` through the local API (or the Python client) with three fields
set honestly:

- `surface`: who wrote it (`"calendar"`, `"browser"`, `"mail"`, ...)
- `source_ref`: where it came from (a URL, file path, message id)
- `type`: `episodic` for events, `semantic` for stable facts

The pipeline does the rest on every write, no exceptions: stage-0
redaction, extraction, salience gating, conflict resolution, journaling.
An adapter never needs its own dedup or secret handling.

## The whole adapter contract, in code

```python
from engram.client import Client
from engram.config import Config
from engram.models import MemoryType

client = Client(Config.load(), client_name="calendar").connect(spawn=True)
for event in read_todays_calendar():          # your source here
    client.remember(
        f"{event.title} with {event.attendees} on {event.date}",
        type=MemoryType.EPISODIC,
        scope="work",
        source_ref=event.ical_uid,
    )
```

Register the adapter once (`engram clients allow calendar --scopes work`),
or narrow it further with a capability token and method grants:

```bash
engram clients allow calendar --scopes work --token --methods remember
```

That last line is the least-privilege shape for ingestion: the adapter can
write into `work` and can do nothing else — no recall, no forget, no
export.

## Shipped and planned sources

| Source | Status |
|---|---|
| Markdown files / Obsidian vaults | shipped (`engram seed <dir>`) |
| CLAUDE.md and agent memory files | shipped (`engram seed`) |
| ChatGPT / Claude saved memories | shipped (paste to a file, `engram seed`) |
| Calendar, browser history | adapter contract above; not shipped |
| Messages, email | same contract; gated on dogfooding the redaction pipeline at volume |
| Voice (local Whisper), images (CLIP multivector) | needs multimodal vectors in the shard schema; the journal already stores `source_ref` for re-embedding |
