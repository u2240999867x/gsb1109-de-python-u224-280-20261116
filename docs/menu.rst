.. module:: xdg.Menu

Desktop Menu
============

`XDG Desktop Menu specification <http://standards.freedesktop.org/menu-spec/menu-spec-1.0.html>`_

.. autofunction:: parse

.. autoclass:: Menu
   :members: getName, getGenericName, getIcon, getComment, getPath, getEntries, getMenu, getMenuEntry, getExcludedEntries, getExclusionReason, getExcludedSubmenus, getAppearances, getEntryAppearances

   .. attribute:: ExcludedEntries

       A list of :class:`ExclusionReason` records for entries which this menu
       considered but did not show. The records make decisions from
       ``<Include>`` / ``<Exclude>`` rules, the layout and visibility checks
       queryable after parsing.

   .. attribute:: ExcludedSubmenus

       The subset of :attr:`ExcludedEntries` which refers to submenus.

   .. attribute:: MergeFiles

       Menu files which were merged into this menu, in the order they were
       processed.

   .. attribute:: MissingMergeFiles

       Merge files which were referenced but could not be found. A missing
       merge file is ignored; the rest of the menu tree is still parsed.

   .. attribute:: MergeCycles

       Merge files skipped because merging them would have created a cycle.

.. autoclass:: ExclusionReason
   :members:

   .. attribute:: Reason

       One of ``Excluded``, ``NotIncluded``, ``NotInLayout``, ``Allocated``,
       ``MissingMergeFile``, ``NoDisplay``, ``Hidden``, ``Deleted``, ``Empty``,
       ``NotShowIn`` or ``NoExec``.

   .. attribute:: Rule

       The :class:`Rule` which caused the exclusion, if any. For an
       ``<Exclude>`` rule, :attr:`Detail` contains the rule's XML source.

.. autoclass:: MenuEntry
   :members: save, getDir, getType

   .. attribute:: DesktopEntry

       The :class:`xdg.DesktopEntry.DesktopEntry` instance holding the data for this
       entry.

   .. attribute:: Parents

       The menus in which this entry is allocated. An application may appear
       in more than one menu; every allocation is recorded here.

   .. attribute:: Appearances

       The menus in which this entry is actually visible. This is a subset of
       :attr:`Parents`; entries hidden by the layout, ``NoDisplay``,
       ``Hidden``, ``Deleted``, ``NotShowIn`` or a missing executable are
       excluded. :meth:`Menu.getEntryAppearances` and
       :meth:`Menu.getAppearances` also accept ``visible=False`` to report
       allocations rather than visible appearances.
