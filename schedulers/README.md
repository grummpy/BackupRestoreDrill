# Schedulers

Nothing in this folder runs on its own. Monthly scheduling stays off until you install it:

```bash
python -m backuprestoredrill schedule install linux
python -m backuprestoredrill schedule install macos
python -m backuprestoredrill schedule install windows
```

Each installer registers a run on day 15 at 09:00 that executes `python -m backuprestoredrill drill --all`. `schedule uninstall` removes it. The launchers do not install a schedule.
