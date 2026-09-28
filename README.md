# pytest-powercut

Test that your files survive a power cut.

Your program writes a state file. The power goes out mid-write — a battery dies,
a car engine stops, someone pulls the plug on a Raspberry Pi. What does the next
start-up read back? `pytest-powercut` answers that in a unit test, without
touching real hardware.

It records every write, `fsync` and `rename` your code makes, then rebuilds the
disk states a real file system could leave behind at every point where the power
could have been cut, and runs your check against each one.

```console
pip install pytest-powercut
```

## Example

```python
def test_state_file_survives(powercut):
    def workload(root):
        save_state(root / "state.json", b'{"v": 1}')
        powercut.saved("v1")            # from here on, v1 must never be lost
        save_state(root / "state.json", b'{"v": 2}')
        powercut.saved("v2")

    def check(disk, saved):
        p = disk / "state.json"
        got = p.read_bytes() if p.exists() else None
        if "v2" in saved:
            assert got == b'{"v": 2}', f"v2 was saved, but the file holds {got!r}"
        elif "v1" in saved:
            assert got in (b'{"v": 1}', b'{"v": 2}')

    powercut.run(workload, check)
```

Your code under test is not modified. Only writes below the temporary root the
plugin hands to `workload` are intercepted; everything else goes to the real
file system.

If a crash state breaks your check, the test fails with the state that broke it:

```
電断のあとに壊れた状態が 93 件ありました（試した状態 122 件）。

  - v1 was saved, but the file holds None
      「v1」を約束 の直後に電断
      ディレクトリの変更を 0/2 だけ残す。
```

## What it models

After a power cut a file system may lose anything that was not made durable:

- a write that was never `fsync`ed may be gone, kept, or kept only in part;
- the file may keep its new size while its new bytes never reached the disk,
  which reads back as zeros (seen on ext4 on a Raspberry Pi after real power cuts);
- a create, rename or remove is not durable until the **directory** is `fsync`ed,
  and undurable directory changes are lost from the end backwards.

## What it found

Eight ways of writing a state file, each run through every cut point and every
disk state the model allows:

| How the file is written | states tried | broken |
|---|---|---|
| `open()` + `write()` | 33 | 24 |
| `Path.write_bytes()` | 33 | 24 |
| overwrite in place + `fsync` | 24 | 10 |
| temp file + rename, no `fsync` of the file | 85 | 54 |
| temp file + `fsync` + rename, no `fsync` of the directory | 41 | 14 |
| temp file + `fsync` + rename + `fsync` of the directory | 29 | **0** |
| [`atomicwrites`](https://pypi.org/project/atomicwrites/) | 29 | **0** |

And with the real Home Assistant 2026.2.3 installed:

| Home Assistant | states tried | broken | `fsync` calls |
|---|---|---|---|
| `homeassistant.util.file.write_utf8_file` (the default; `.storage` uses it) | 122 | **93** | **0** |
| `homeassistant.util.file.write_utf8_file_atomic` | 29 | **0** | 4 |

90 of those 93 states appear **after** Home Assistant has returned from saving.
The file can come back missing, empty, truncated mid-JSON, or all zero bytes.

These counts are states in the model, not probabilities: they say "this outcome
is possible because nothing was made durable", not "this happens 76% of the time".

## Limits

- Only Python-level file operations are intercepted: `open`, `os.open`,
  `os.replace`/`rename`, `os.remove`, `os.fsync`, `os.fdopen`, `os.stat`,
  `os.write`, `os.fchmod` and `fcntl(F_FULLFSYNC)` on macOS. Writes made inside C
  extensions — `sqlite3`, `numpy.save`, `mmap` — are invisible to it.
- The model is deliberately pessimistic: it allows anything a POSIX file system
  is permitted to do, not what one particular file system happens to do today.
- One directory, no sub-directories, in this first release.

## License

MIT
