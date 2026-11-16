.. module:: xdg.MimeApps

MIME Applications Associations
==============================

`XDG MIME Applications Associations specification <http://specifications.freedesktop.org/mime-apps-spec/>`_

This module reads the desktop entry inventory, the shared MIME inheritance
database and the existing ``mimeapps.list`` files, then works out the smallest
user-level change that gives a set of MIME types the requested default
applications.

Only applications that are installed, visible, executable and that declare
support for the type (directly or through MIME inheritance) are considered.
Effective defaults for MIME types that were not requested are never changed,
including changes that would arrive indirectly through inheritance.  When the
requests cannot all be satisfied, every conflicting request is reported and
the original file is left untouched.

Inspecting the current state
----------------------------

.. autofunction:: describe

.. autoclass:: MimeGraph
   :members: canonical, ancestors, descendants

.. autoclass:: DesktopInventory
   :members: scan, from_entries, get, selectable, all_mime_types, as_list

.. autoclass:: AssociationFile

.. autoclass:: AssociationSet
   :members: defaults, added, keys

.. autofunction:: load_association_files

.. autoclass:: Resolver
   :members: resolve, resolve_all

Planning and applying changes
-----------------------------

.. autofunction:: plan_associations

.. autoclass:: Plan
   :members: ok, as_dict

.. autofunction:: apply_plan

.. autofunction:: verify_plan

.. autofunction:: update_associations

.. autofunction:: render_user_file

Command line
------------

``python -m xdg.MimeApps scenario.json`` writes the user file and prints the
inventory, MIME inheritance, existing associations, the planned changes and
the selection read back from disk.

``python -m xdg.MimeApps scenario.json --dry-run`` prints the same report
without writing.

``python -m xdg.MimeApps scenario.json --describe`` only reports the current
state: inventory, inheritance and effective defaults.

A scenario is a JSON object with these keys:

``data_dirs``, ``config_home``, ``config_dirs``, ``data_home``
    XDG search paths.  ``data_dirs`` are data directories; their
    ``applications`` subdirectory is scanned for desktop entries.

``desktop_env``
    Value of ``$XDG_CURRENT_DESKTOP``, used for ``OnlyShowIn``/``NotShowIn``.

``user_file``
    File to create or update.  Defaults to ``$XDG_CONFIG_HOME/mimeapps.list``.

``targets``
    List of ``{"mime": "...", "app": "..."}`` requests.

``inventory``
    Optional explicit desktop entry list, instead of scanning the disk.  Each
    entry needs ``id`` plus either ``path`` or the relevant fields, for
    example ``mime_types``, ``exec``, ``executable``, ``hidden``,
    ``no_display``.

``inheritance``
    Optional ``{child: [parent, ...]}`` mapping merged with the
    ``mime/subclasses`` files found in ``data_dirs``.

``aliases``
    Optional MIME alias mapping.

``associations``
    Optional ordered association layers, each with ``path`` or explicit
    ``defaults``/``added``/``removed`` maps.  Mark the layer to edit with
    ``"user": true``.
