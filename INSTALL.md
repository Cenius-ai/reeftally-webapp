# Installing and running ReefTally

The one package manager this project uses is `pip` for the *optional* console-script
install. ReefTally itself has **no third-party dependencies**: it is a single Python file that
imports only the standard library, so there is nothing to download and nothing to build.

## 1. Prerequisite

* **Python 3.8 or newer** (Python 3.11 is what the project is developed and tested on).
  Check with:

  ```bash
  python3 --version
  ```

  Python is a system prerequisite. `install.sh` verifies it and stops with a clear message if
  it is missing — it never installs it, and never calls `sudo` or a system package manager.

## 2. Set up the project (one command, then it exits)

```bash
bash install.sh
```

What it does, in order:

1. verifies `python3` exists and is at least 3.8;
2. checks `requirements.txt` — the file declares no third-party packages, so nothing is fetched
   (setup therefore works with no network at all);
3. byte-compiles and **imports** `reeftally.py`, so a broken tree fails here instead of at first use;
4. runs the developer smoke suite (`python3 -m unittest test_reeftally`);
5. tries the optional `pip install --no-build-isolation -e .` so the `reeftally` console script is
   on `PATH`. Running `python3 reeftally.py ...` works with or without this step, so an offline
   machine is not a setup failure (`REEFTALLY_SKIP_SCRIPT=1 bash install.sh` skips it deliberately).

It never starts a server and never prompts. Re-running it is safe.

## 3. Run the tool

No installation is required — the tool is one file:

```bash
python3 reeftally.py --help
python3 reeftally.py report examples/hornsby-reef.json
```

If step 5 above succeeded, the short form also works:

```bash
reeftally --help
reeftally report examples/hornsby-reef.json
```

`python3 -m reeftally --help` works as well when the module is importable (project root on
`sys.path`, or after the editable install).

## 4. See it work end to end

```bash
bash demo.sh
```

Creates a survey in a temporary directory, records quadrats, names species, exports CSV, removes the
last quadrat, shows a validation failure with its exit status, prints the committed example survey,
and cleans up after itself. Fully non-interactive.

## 5. Run the tests

```bash
python3 -m unittest test_reeftally
```

`test_reeftally.py` is a developer artifact — the shipped tool does not need it. pytest collects the
same suite (`pytest -q`) if it happens to be installed, but no runner package is required.

## Where state lives

Every command takes the path of one JSON project file. There is no database, no config directory, no
dotfile, no cache and no lock file; the only state ReefTally reads or writes is the project file you
name on the command line (plus the `.tmp` sibling that is renamed over it during an atomic write).
Deleting the project file is a complete uninstall.
