# Tests

Hardware-free unit tests for the host code: the BMCU wire protocol, the G-code source
planner, state-file loading, and the Snapmaker U1 stock-feeder hand-back.

```sh
python3 -m pip install greenlet
python3 -m unittest discover -s tests -v
```

They import `klippy/extras/bmcu_core` directly, with stand-ins for Klipper's C helpers
(see `_support.py`), so no printer, Klipper checkout or BMCU is needed. CI runs them on
every push and pull request (`.github/workflows/tests.yml`).
