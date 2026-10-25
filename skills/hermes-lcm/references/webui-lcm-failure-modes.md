# Signal 7 — webui sessions never autocompress: Hermes plugin discovery never ran at webui boot

Symptom: WebUI sessions log "Context engine 'lcm' not found — falling back to built-in compressor" in agent.log (logger `run_agent`), turn lines show platform=webui, and context % climbs past the configured autocompress threshold (e.g. 45%) with no LCM sweep — while Telegram/gateway sessions on the same install load LCM fine. Looks like "85% vs 45% systems competing"; actually the webui agent silently ran the built-in compressor (gate 0.85) because the LCM engine was never selected.

Root cause (hermes-webui @ ~/hermes-webui, fixed 2026-10-02 on this host):
- server.py boot called only api.plugins.load_plugins(), which scans DASHBOARD plugin manifests — never Hermes plugins.
- Hermes plugin discovery is reached only lazily inside agent tool-loading (agent_init ~1115), which runs AFTER _select_context_engine() has already given up → built-in fallback for every web agent.
- The webui venv is fully capable (editable hermes-agent install; fastembed + sqlite_vec present): running discover_plugins() in a webui-venv REPL registered engine 'lcm' + hooks cleanly (61 found / 54 enabled). Ordering was the ONLY failure.
- The webui systemd unit also lacked the profile's LCM/MNEMOSYNE settings (start.sh sources only the repo-local dotfile; bootstrap.py explicitly skips the profile one).

Fix applied (re-apply after hermes-webui updates!):
1. ~/.config/systemd/user/hermes-webui.service.d/env.conf:
   [Service] + EnvironmentFile=-$HOME/.hermes/.env
2. server.py boot patch, inserted right after the api.plugins.load_plugins try-block:

    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        print('[ok] Hermes plugin discovery complete (context engines + hooks registered)', flush=True)
    except Exception as e:
        print(f'[!!] WARNING: Hermes plugin discovery failed: {e}', flush=True)

Restart: systemctl --user restart hermes-webui  (drops in-flight web sessions — warn the user first).

Verify:
- systemctl --user show hermes-webui -p EnvironmentFiles lists the profile file
- webui stdout (journal) shows the [ok] discovery line; NO new "Context engine ... not found" lines for platform=webui turns
- grow a new web session past the threshold → threshold_full_sweep entries appear in agent.log
- "LCM tools are available through context-engine schemas (Path B)" in logs is EXPECTED on this host, not a fault.
