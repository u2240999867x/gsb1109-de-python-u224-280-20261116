# LnkParse3

Windows Shortcut file (LNK) parser

[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=for-the-badge)](http://makeapullrequest.com)
[![PyPI license](https://img.shields.io/pypi/l/LnkParse3.svg?style=for-the-badge)](https://github.com/Matmaus/LnkParse3/blob/master/LICENSE)
[![PyPi Version](https://img.shields.io/pypi/v/LnkParse3.svg?style=for-the-badge)](https://pypi.python.org/pypi/LnkParse3/)
[![PyPi Python Versions](https://img.shields.io/pypi/pyversions/LnkParse3.svg?style=for-the-badge)](https://pypi.python.org/pypi/LnkParse3/)
[![GitHub last commit](https://img.shields.io/github/last-commit/Matmaus/LnkParse3.svg?style=for-the-badge)](https://github.com/Matmaus/LnkParse3/commits/master)

LnkParse3 is a minimalistic python package for **forensics** of a binary file with [LNK](https://fileinfo.com/extension/lnk) extension aka [Microsoft Shell Link](https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-shllink/16cb4ca1-9339-4d0c-a68d-bf1d6cc0f943) aka Windows shortcut. It is aimed to dig up as much data as possible and to process even **malformed files**. It is not able to create or modify files.

## Features

- easy to use
- CLI tool & package
- JSON output

# Background

This is a fork of `lnkfile` available [here](https://github.com/silascutler/LnkParse).

Improvements:

- much more extracted data
- many bug fixes
- made to not fail on malformed files

NOTE: `master` branch history was rewritten and has different commits metadata than the upstream `master`.

# Installation

```
pip install LnkParse3
```

# Usage

Can be used as a package or as a command line tool. It accepts several arguments, including setting the output format to JSON or a more human-readable form. For all parameters, see the program description below.

```
usage: lnkparse [-h] [-t] [-j] [-c CP] [-a] FILE

Windows Shortcut file (LNK) parser

positional arguments:
  FILE                  absolute or relative path to the file

optional arguments:
  -h, --help            show this help message and exit
  -t, --target          print target only
  -j, --json            print output in JSON
  -c CP, --codepage CP  set codepage of ASCII strings
  -a, --all             print all extracted data (i.e. offsets and sizes)
```

## CLI tool

```console
$ lnkparse tests/samples/microsoft_example
Windows Shortcut Information:
   Guid: 00021401-0000-0000-C000-000000000046
   Link flags: HasTargetIDList | HasLinkInfo | HasRelativePath | HasWorkingDir | IsUnicode | EnableTargetMetadata - (524443)
   File flags: FILE_ATTRIBUTE_ARCHIVE - (32)
   Creation time: 2008-09-12 20:27:17.101000+00:00
   Accessed time: 2008-09-12 20:27:17.101000+00:00
   Modified time: 2008-09-12 20:27:17.101000+00:00
   File size: 0
   Icon index: 0
   Windowstyle: SW_SHOWNORMAL
   Hotkey: UNSET - UNSET {0x0000}

   ...more data...

   EXTRA:
      DISTRIBUTED LINK TRACKER BLOCK:
         Size: 96
         Length: 88
         Version: 0
         Machine identifier: chris-xps
         Droid volume identifier: 94C77840-FA47-46C7-B356-5C2DC6B6D115
         Droid file identifier: 7BCD46EC-7F22-11DD-9499-00137216874A
         Birth droid volume identifier: 94C77840-FA47-46C7-B356-5C2DC6B6D115
         Birth droid file identifier: 7BCD46EC-7F22-11DD-9499-00137216874A
```

## Python package

```python
>>> import LnkParse3
>>> with open('tests/samples/microsoft_example', 'rb') as indata:
>>> 	lnk = LnkParse3.lnk_file(indata)
>>> 	lnk.print_json()
{
	"data": {
        "relative_path": ".\\a.txt",
        "working_directory": "C:\\test"
    },
    "extra": {
        "DISTRIBUTED_LINK_TRACKER_BLOCK": {
            "birth_droid_file_identifier": "7BCD46EC-7F22-11DD-9499-00137216874A",
            "birth_droid_volume_identifier": "94C77840-FA47-46C7-B356-5C2DC6B6D115",
            "droid_file_identifier": "7BCD46EC-7F22-11DD-9499-00137216874A",
            "droid_volume_identifier": "94C77840-FA47-46C7-B356-5C2DC6B6D115",
            "length": 88,
            "machine_identifier": "chris-xps",
            "size": 96,
            "version": 0
        }
    },
	...more data...
}
```

# Merging catalogs from several machines

`LnkParse3.reconcile` answers the question "which volumes and targets on
these machines are the same object?" without trusting coincidence. A volume
label, a drive letter or an identical path is evidence belonging to the
machine that declared it; none of those alone establishes cross-machine
identity. Identity comes only from the declarations you supply:

- `candidate_volume_mappings`: volume pairs that *may* be one entity.
- `verified_objects`: shortcut sets already confirmed to be one object.
- `distinct_volumes` / `distinct_targets`: pairs known to be different.

The reconciler enumerates the maximal, jointly feasible identity assignments,
keeps every one that is valid, and reports only relations present in **all**
of them. Each machine keeps its own volume declaration and original path in
the output; no synthetic drive letter is introduced.

```console
$ lnkreconcile catalog.json
$ lnkreconcile catalog.json --compact          # omit per-plan entities
$ lnkreconcile catalog.json --all-feasible     # include non-maximal plans
$ lnkreconcile catalog.json --fail-on-conflict # exit 2 when unsatisfiable
```

The repository ships `catalog.example.json`, the sample described below:

```json
{
  "machines": [
    {
      "id": "laptop",
      "volumes": [
        {"id": "os", "label": "Windows", "drive": "C:", "declared": {"note": "disk0p2"}}
      ],
      "shortcuts": [
        {"id": "laptop-report", "volume": "os", "path": "C:\\Users\\ana\\report.docx"}
      ]
    },
    {
      "id": "desktop",
      "volumes": [
        {"id": "sys", "label": "Windows", "drive": "C:"},
        {"id": "data", "label": "Backup", "drive": "D:"}
      ],
      "shortcuts": [
        {"id": "desktop-report", "volume": "data", "path": "D:\\restore\\report.docx"}
      ]
    }
  ],
  "candidate_volume_mappings": [
    {"id": "maybe-os", "a": "laptop:os", "b": "desktop:sys"},
    {"id": "maybe-data", "a": "laptop:os", "b": "desktop:data"}
  ],
  "verified_objects": [
    {"id": "same-report", "members": ["laptop-report", "desktop-report"]}
  ]
}
```

The verified object forces `laptop:os` and `desktop:data` to be one entity.
The remaining `maybe-os` candidate would merge `desktop:sys` into that same
entity, which then holds two volumes of the `desktop` machine; mutual
exclusion rejects that plan. The only plan left reports the volume relation
`laptop:os` = `desktop:data` and the target relation for `same-report`, each
member keeping its own machine, volume, label, drive and original path.

`distinct_volumes` and `distinct_targets` eliminate matching plans. A
same-machine pair can never be one entity, so a candidate between two volumes
of one machine is reported as a warning and never used. Verified objects are
transitive: if two of them force two distinct volumes of the same machine
together, that component is unsatisfiable and reports the conflicting records
and constraint ids. Independent components are still solved and reported.

Programmatic use:

```python
from LnkParse3.reconcile import Catalog, reconcile

catalog = Catalog.from_dict(data)          # or Catalog.from_lnk_files(...)
report = reconcile(catalog)
for relation in report.confirmed_target_relations:
    print(relation.descriptor())
if not report.complete:
    for group in report.unsatisfiable_groups:
        for conflict in group.conflicts:
            print(conflict.kind, conflict.message)
```

`Catalog.from_lnk_files([(machine, shortcut_id, path), ...])` builds the
volumes and shortcuts from LNK files; the label, drive, serial and DROID
identifiers are copied into `declared` as that machine's evidence, never used
as cross-machine identity. Add the supplier's declarations through the
`candidate_volume_mappings`, `verified_objects`, `distinct_volumes` and
`distinct_targets` keyword arguments.

# Extracted data

List of data in LNK structure and their current status of implementation.
For more information about each data, see [Microsoft LNK documentation](https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-shllink/16cb4ca1-9339-4d0c-a68d-bf1d6cc0f943) and [Shell item format specification](https://github.com/libyal/libfwsi/blob/main/documentation/Windows%20Shell%20Item%20format.asciidoc).

- [x] **ShellLinkHeader** [`lnk_header.py`]
- [ ] **LinkTargetIDList** [`lnk_targets.py`]
  - [ ] **RootFolder** [`root_folder.py`] (incomplete)
  - [ ] **CommonPlacesFolder** [`common_places_folder.py`]
  - [ ] **CompressedFolder** [`compressed_folder.py`]
  - [ ] **ControlPanel** [`control_panel.py`]
  - [ ] **Internet** [`internet.py`]
  - [ ] **MyComputer** [`my_computer.py`]
  - [x] **NetworkLocation** [`network_location.py`]
  - [ ] **Printers** [`printers.py`]
  - [ ] **ShellFSFolder** [`shell_fs_folder.py`] (incomplete)
  - [ ] **UsersFilesFolder** [`users_files_folder.py`]
- [x] **LinkInfo** [`lnk_info.py`]
  - [x] **Local** [`local.py`]
  - [x] **Network** [`network.py`]
- [x] **StringData** [`string_data.py`]
- [ ] **ExtraData** [`extra_data.py`]
  - [x] **ConsoleDataBlock** [`console.py`]
  - [x] **ConsoleFEDataBlock** [`code_page.py`]
  - [x] **DarwinDataBlock** [`darwin.py`]
  - [x] **EnvironmentVariableDataBlock** [`environment.py`]
  - [x] **IconEnvironmentDataBlock** [`icon.py`]
  - [x] **KnownFolderDataBlock** [`known_folder.py`]
  - [ ] **PropertyStoreDataBlock** [`metadata.py`] (incomplete)
  - [x] **ShimDataBlock** [`shim_layer.py`]
  - [x] **SpecialFolderDataBlock** [`special_folder.py`]
  - [x] **TrackerDataBlock** [`distributed_tracker.py`]
  - [x] **VistaAndAboveIDListDataBlock** [`shell_item.py`]
  - [x] *Unknown (undefined) block* [`unknown.py`]
  - [x] **TerminalBlock** [`terminal.py`]

# Contributing

Any contribution is welcome. There are still several uncovered parts of LNK Structure. Just fork the project and open a new PR.

## Tests

To run tests without installing any dependencies, just run:

```sh
python -m unittest discover tests
```

If you want to use `pytest`, install it via `pip` and run:

```sh
pytest tests
```

Also, to see code coverage in HTML output, run:

```sh
pytest --cov=LnkParse3 tests --cov-fail-under=80 --cov-report=html --no-cov-on-fail
```

## Code

Make sure to run [`ruff`](https://docs.astral.sh/ruff/) auto-formatter and linter before opening a PR. It will keep the code in good shape.

Also, it would be nice to try to make meaningful commit messages and atomic commits.

# Authors and acknowledgment

Many thanks to the project's founder [@silascutler](https://github.com/silascutler) as well as to [@ernix](https://github.com/ernix) for such a good job refactoring and improving the code.

# Related projects

Here is a list of other available LNK parsers:

- [pylnk3](https://github.com/strayge/pylnk) - console application and package in Python 3
- [lnk-parse](https://github.com/lcorbasson/lnk-parse) - console application in Perl
- [pylnker](https://github.com/HarmJ0y/pylnker) - console application and package in Python 2, based on lnk-parse
- [liblnk](https://github.com/libyal/liblnk) - robust C library with Python 2/3 bindings

# License

Distributed under the MIT License. See [LICENSE](https://github.com/Matmaus/LnkParse3/blob/master/LICENSE) for more information.

# Contact

[matusjas.work@gmail.com](mailto:matusjas.work@gmail.com)

Source - [https://github.com/Matmaus/LnkParse3](https://github.com/Matmaus/LnkParse3)
