# plugins/

Drop provider modules here — one folder per provider:

```
plugins/
  my_plugs/
    adapter.py          # defines ADAPTER = <your ProviderAdapter subclass>
    fake_device.py      # a simulator of your provider, for tests
    test_my_plugs.py    # the SDK conformance kit + your own tests (unique name per module)
```

The hub loads every `plugins/<name>/adapter.py` at start-up (folders starting
with `_` or `.` are skipped). No core file changes. A module that fails to load
is reported in Settings and `/api/system` and never stops the hub.

Start with `python -m hnb.cli new-provider my_plugs --name "My plugs"` and read
[`ADDING_A_PROVIDER.md`](../ADDING_A_PROVIDER.md). After adding or changing a
plugin on an installed hub, re-run `sudo ./install.sh` to rebuild the image.

Plugins run inside the hub with its privileges — install only code you trust.
