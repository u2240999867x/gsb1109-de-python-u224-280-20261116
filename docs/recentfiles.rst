.. module:: xdg.RecentFiles

Recently used files
===================

`XDG recent file storage specification <http://standards.freedesktop.org/recent-file-spec/recent-file-spec-latest.html>`_

.. autoclass:: RecentFiles
   :members: parse, write, getFiles, getItems, query, addFile, deleteFile

.. autoclass:: AggregatedRecentFiles
   :members: add_source, remove_source, sources, getItems, query

.. autoclass:: RecentItem
   :members:

.. autoclass:: RecentMerge
   :members:

.. autofunction:: uri_key

.. class:: RecentFile

   .. attribute:: URI

      The URI of the file; typically starts with ``file:///``.

   .. attribute:: MimeType

      A Mime type, such as 'text/plain'.

   .. attribute:: Timestamp

      Unix timestamp of when the file was added to the list.

   .. attribute:: Private

      Boolean indicating whether the entry is private, meaning that it will
      only be shown to a caller explicitly authorized for one of its groups.

   .. attribute:: Groups

      A list of groups this entry belongs to.

Visibility and aggregation
--------------------------

Each parsed ``RecentItem`` is an independent piece of evidence tagged with
the configuration (source) it came from.  When several configurations are
combined in an :class:`AggregatedRecentFiles`:

* public occurrences are visible to every caller;
* private occurrences are only visible when the query passes
  ``include_private=True`` *and* names one of the item's groups, so a
  public occurrence of a URI never releases a private occurrence of the
  same URI;
* MIME type and group filters are conjunctive, and filtering, sorting and
  deduplication only use occurrences visible to the current caller;
* equivalent URIs (see :func:`uri_key`) are merged: the newest visible
  timestamp wins and the merged groups are the union of the visible
  occurrences' groups;
* :meth:`RecentMerge.getItems` and :meth:`RecentMerge.getSources` trace a
  merged record back to the actual :class:`RecentItem` occurrences and
  configurations backing it;
* :meth:`AggregatedRecentFiles.remove_source` withdraws exactly the
  evidence supplied by one configuration.
