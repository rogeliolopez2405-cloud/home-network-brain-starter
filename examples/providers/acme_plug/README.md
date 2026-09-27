# Example provider — Acme plugs

A complete, working provider module used as the template for new providers.

| File | What it shows |
|---|---|
| `adapter.py` | The whole contract: identity, settings vs. credentials, transport with timeouts, error mapping, capability mapping, read-back verification, health |
| `fake_device.py` | A simulator of the provider (here: the Acme hub). Every provider module ships one, so tests never need a live account |
| `test_conformance.py` | Runs the SDK conformance kit against the adapter + a provider-specific test |

Try it:

```bash
python -m pytest examples/providers/acme_plug          # 12 passed: 10 conformance checks + 2 own tests
python -m hnb.cli new-provider my_plugs --name "My plugs"   # copy this template to plugins/my_plugs/
```

Walk-through: [`ADDING_A_PROVIDER.md`](../../../ADDING_A_PROVIDER.md).
