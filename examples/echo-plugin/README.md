# External executor example

This is a deterministic echo, **not an AI provider**. It verifies installation, discovery, tasks, continuation and portability without credentials. Install it into the same Python environment as Hub:

```sh
python -m pip install .
```

From the Hub repository instead, use `python -m pip install ./examples/echo-plugin`.

Add to your private Hub `config.json` (merge the `agents` object with your existing configuration):

```json
{"agents":{
  "example-writer":{"kind":"plugin","plugin":"echo","options":{"prefix":"Example: "}},
  "example-command":{"kind":"command","args":["-m","agent_hub_echo","{request_file}"],
    "resume_args":["-m","agent_hub_echo","{request_file}"]}
}}
```

Run `python -m agent_hub check-config` to validate local configuration before starting a daemon, then `python -m agent_hub status`. Submit to either executor with `mode: "delegate"` and an existing workspace on that device. The command bridge defaults to Hub's own Python interpreter; no repository path or operating-system-specific executable suffix is required. Both modules must be installed, not imported by adding the source tree to PYTHONPATH.

To implement an actual tool, copy this package under a new distribution/module/entry-point name, replace `Echo.run` with the provider integration, and accurately implement cancellation, deadlines, results and permissions. Keep tokens in private device environment variables or the provider's credential store. Persist plugin state only in `context.data_dir`. See the Hub's `docs/executor-plugins.md` for the v1 contract and limits.
