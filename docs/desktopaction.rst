.. module:: xdg.DesktopAction

Desktop Actions
===============

`Desktop Actions <http://standards.freedesktop.org/desktop-entry-spec/latest/ar01s11.html>`_
are the extra entries an Application desktop file advertises through its
``Actions=`` key and its ``[Desktop Action <id>]`` groups.

Only the desktop file that wins the XDG data directory lookup is read, so a
user override fully replaces a system file of the same desktop file id.
Action ids are scoped to the application that declares them.

.. autoclass:: DesktopAction

   .. automethod:: getId
   .. automethod:: getGroup
   .. automethod:: getName
   .. automethod:: getExec
   .. automethod:: getFileName
   .. automethod:: getSource

.. autoclass:: DesktopApplication

   .. automethod:: discover
   .. automethod:: from_file
   .. automethod:: getId
   .. automethod:: getDesktopEntry
   .. automethod:: getFileName
   .. automethod:: getSource
   .. automethod:: getShadowedFiles
   .. automethod:: getName
   .. automethod:: getActions
   .. automethod:: getAction
   .. automethod:: getErrors
   .. automethod:: getUnreferencedActionGroups

.. autofunction:: discover_desktop_applications
