.. module:: xdg.IconPack

Offline Icon Packs
==================

`XDG Icon Theme specification <http://standards.freedesktop.org/icon-theme-spec/icon-theme-spec-latest.html>`_

The icon pack planner selects the smallest set of files which satisfies a list
of target icons.  It follows the requested theme and its ``Inherits`` chain,
applies the ``Fixed``, ``Threshold`` and ``Scalable`` directory rules together
with the caller's scale and tolerance, and accounts for each file only once
when several targets use it.

.. autofunction:: select_icon_pack

.. autoclass:: IconTarget
   :members:

.. autoclass:: IconPack
   :members: select

.. autoclass:: PackResult
   :members: as_dict, satisfied, unmet, gaps, missing, unreadable,
             no_size_match, theme_errors

Targets can be supplied as ``IconTarget`` objects or dictionaries::

    from xdg.IconPack import select_icon_pack

    result = select_icon_pack(
        [
            {"name": "folder", "size": 24, "scale": 1, "tolerance": 1},
            {"name": "document-save", "size": 32, "scale": 2, "tolerance": 0},
        ],
        theme="Adwaita",
        file_sizes={"png": 2048, "svg": 900},
    )

    for entry in result.selected:
        print(entry["path"], entry["volume"], entry["target_names"])

``file_sizes`` may give exact file paths, extensions or a callable.  Paths are
matched by resolved file, so identically named icons from different themes keep
separate volumes.  When a file is not listed, its real size on disk is used.
``size`` and ``tolerance`` are nominal units; a target can instead be described
with ``pixel_size``, in which case the nominal size is ``pixel_size / scale``.

The result separates three failure kinds:

- ``missing``: no icon file of that name exists in the searched theme chain.
- ``unreadable``: a directory entry or file exists but cannot be read.
- ``no_size_match``: files of that name exist, but no directory satisfies the
  size, scale and tolerance request.

When some targets are impossible, the remaining targets are still packed and
``unmet`` (also available as ``gaps``) reports the shortfall for each one.

The same planner is available from the command line::

    python -m xdg.IconPack request.json --pretty

A request is a JSON object with ``theme``, ``targets``, ``file_sizes`` and
optional ``search_paths`` and ``extensions``.  Use ``--fail-on-gaps`` when the
command's exit status should signal an incomplete pack.
